"""
Asistif Gözlük - Mesafe Kalibrasyonu
====================================

Kameradan mesafe (derinlik yapay zekâsı) tutarlı bir yanlılıkla hata yapıyor:
geniş açılı lens ve modelin eğitildiği kameralardan farklı olması yüzünden.
Bu araç gerçek mesafelerle modelin tahminini karşılaştırıp bir düzeltme eğrisi
çıkarır ve kalibrasyon.json'a yazar. Program açılışta bu eğriyi okur.

Nasıl yapılır (~10 dakika):
  1. Programı durdurun (kamera boş olmalı):  systemctl --user stop asistif-gozluk
  2. Gözlüğü takan kişi sabit dursun. Önüne metreyle 0,5 / 1 / 1,5 / 2 / 3 / 4 m
     işaretleyin (bant ya da kâğıtla).
  3. Çalıştırın:  python3 kalibrasyon.py
  4. Her mesafede ikinci bir kişi işaretin üstünde, kameraya dönük ayakta dursun,
     Enter'a basın. Araç birkaç kare alıp kaydeder.
  5. Sonunda hata tablosu yazılır, kalibrasyon.json ve kalibrasyon_olcumleri.csv kaydedilir.
  6. Programı başlatın:  systemctl --user start asistif-gozluk

  python3 kalibrasyon.py --mesafeler 0.5 1 1.5 2 2.5 3 4   # farklı mesafeler
  python3 kalibrasyon.py --sil                             # kalibrasyonu kaldır

Hata tablosu tez için: "kalibrasyonsuz" ve "kalibrasyonlu" sütunları. Kalibrasyonlu
hata dürüst olsun diye her nokta, o nokta dışarıda bırakılarak çıkarılan eğriyle
hesaplanır (birini-dışarıda-bırak doğrulaması).
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time

import numpy as np

import mesafe
from goru import NesneTanima

BURASI = os.path.dirname(os.path.abspath(__file__))
CSV_DOSYASI = os.path.join(BURASI, "kalibrasyon_olcumleri.csv")


def kamera_ac():
    from picamera2 import Picamera2
    kamera = Picamera2()
    kamera.configure(kamera.create_preview_configuration(main={"format": "RGB888", "size": (1280, 960)}))
    kamera.start()
    time.sleep(2)
    return kamera


def kisi_tahmini(olcer, yolo, kare):
    """Karedeki (ortaya en yakın) insanın üzerindeki ham model tahmini; insan yoksa orta bölge."""
    h, w = kare.shape[:2]
    yolo.isle(kare)
    harita, (kx0, ky0, kx1, ky1) = olcer.ham_derinlik(kare)
    sx, sy = mesafe.GIRIS_W / (kx1 - kx0), mesafe.GIRIS_H / (ky1 - ky0)
    insanlar = [t for t in yolo.son if t[1] == "person"]
    if insanlar:
        _, _, _, (x1, y1, x2, y2) = min(insanlar, key=lambda t: abs((t[3][0] + t[3][2]) / 2 - w / 2))
        a1 = int((x1 - kx0 + (x2 - x1) * 0.30) * sx)
        a2 = int((x1 - kx0 + (x2 - x1) * 0.70) * sx)
        b1 = int((y1 - ky0 + (y2 - y1) * 0.25) * sy)
        b2 = int((y1 - ky0 + (y2 - y1) * 0.75) * sy)
        a1, a2 = max(0, a1), min(mesafe.GIRIS_W, max(a1 + 1, a2))
        b1, b2 = max(0, b1), min(mesafe.GIRIS_H, max(b1 + 1, b2))
        return float(np.median(harita[b1:b2, a1:a2])), "insan"
    H, W = harita.shape
    return float(np.median(harita[int(H * 0.35):int(H * 0.65), int(W * 0.4):int(W * 0.6)])), "orta bölge"


def egri_kur(noktalar):
    """[(tahmin, gerçek)] → tahmine göre sıralı, gerçek mesafesi tekdüze artan noktalar."""
    n = sorted(noktalar)
    # Tahmin sırası gerçek sırayla çelişirse (model yakında ters davranıyor) komşuları ortala
    degisti = True
    while degisti and len(n) > 1:
        degisti = False
        for i in range(len(n) - 1):
            if n[i + 1][1] < n[i][1]:
                ort_t, ort_g = (n[i][0] + n[i + 1][0]) / 2, (n[i][1] + n[i + 1][1]) / 2
                n[i:i + 2] = [(ort_t, ort_g)]
                degisti = True
                break
    return n


def egri_tahmin(egri, d):
    t = np.array([p[0] for p in egri])
    g = np.array([p[1] for p in egri])
    if d < t[0]:
        return float(d * g[0] / t[0])
    if d > t[-1]:
        return float(d * g[-1] / t[-1])
    return float(np.interp(d, t, g))


def main():
    p = argparse.ArgumentParser(description="Kameradan mesafe kalibrasyonu")
    p.add_argument("--mesafeler", type=float, nargs="+", default=[0.5, 1.0, 1.5, 2.0, 3.0, 4.0])
    p.add_argument("--kare", type=int, default=6, help="her mesafede alınacak kare sayısı")
    p.add_argument("--sil", action="store_true", help="kalibrasyon.json'u sil")
    a = p.parse_args()

    if a.sil:
        if os.path.exists(mesafe.KALIBRASYON_DOSYASI):
            os.remove(mesafe.KALIBRASYON_DOSYASI)
            print("Kalibrasyon kaldırıldı. Programı yeniden başlatın.")
        return

    durum = subprocess.run(["systemctl", "--user", "is-active", "asistif-gozluk"],
                           capture_output=True, text=True).stdout.strip()
    if durum == "active":
        print("Program çalışıyor ve kamerayı kullanıyor. Önce durdurun:\n  systemctl --user stop asistif-gozluk")
        sys.exit(1)

    print("Kamera ve modeller hazırlanıyor...")
    kamera = kamera_ac()
    olcer = mesafe.KameraMesafe(kare_al=lambda: None, mod_al=lambda: "ev")
    olcer.egri = None  # ham model tahmini ölçülecek
    model_modu = "sokak" if mesafe.DERINLIK_MODELI in ("dis", "dış") else "ev"
    olcer._model_yukle(model_modu)
    yolo = NesneTanima(kare_al=lambda: None)
    yolo.yukle()
    print(f"Kırpma: {mesafe.DERINLIK_KIRPMA} | model: {mesafe.MODELLER[model_modu]}\n")

    olcumler = []
    for hedef in a.mesafeler:
        input(f"→ Kişi kameranın tam karşısında {hedef:g} m'de, ayakta dursun. Hazır olunca Enter...")
        degerler, kaynaklar = [], set()
        for _ in range(a.kare):
            kare = kamera.capture_array()
            if float(kare.mean()) < mesafe.KARANLIK_ESIGI:
                print("   ⚠️  Görüntü çok karanlık, ışığı açın")
                continue
            d, kaynak = kisi_tahmini(olcer, yolo, kare)
            degerler.append(d)
            kaynaklar.add(kaynak)
            time.sleep(0.3)
        if not degerler:
            print("   Ölçüm alınamadı, bu mesafe atlandı")
            continue
        tahmin = float(np.median(degerler))
        yayilim = float(np.std(degerler))
        olcumler.append((hedef, tahmin, yayilim, "/".join(sorted(kaynaklar))))
        print(f"   model {tahmin:.2f} m (±{yayilim:.2f}, {'/'.join(sorted(kaynaklar))}) → gerçek {hedef:g} m\n")
    kamera.stop()

    if len(olcumler) < 3:
        print("En az 3 mesafe gerekli, kalibrasyon kaydedilmedi.")
        return

    noktalar = [(t, g) for g, t, _, _ in olcumler]
    egri = egri_kur(noktalar)
    print("\n  Gerçek | Model  | Kalibrasyonsuz hata | Kalibrasyonlu hata (birini dışarıda bırak)")
    satirlar, ham_hatalar, kal_hatalar = [], [], []
    for i, (gercek, tahmin, yayilim, kaynak) in enumerate(olcumler):
        digerleri = egri_kur([n for j, n in enumerate(noktalar) if j != i])
        duzeltilmis = egri_tahmin(digerleri, tahmin) if len(digerleri) >= 2 else tahmin
        ham_h = (tahmin - gercek) / gercek * 100
        kal_h = (duzeltilmis - gercek) / gercek * 100
        ham_hatalar.append(abs(ham_h))
        kal_hatalar.append(abs(kal_h))
        print(f"  {gercek:5.2f} m | {tahmin:5.2f} m | {ham_h:+6.0f} % | {duzeltilmis:5.2f} m  {kal_h:+6.0f} %")
        satirlar.append([gercek, round(tahmin, 3), round(yayilim, 3), kaynak, round(ham_h, 1),
                         round(duzeltilmis, 3), round(kal_h, 1)])
    print(f"\n  Ortalama mutlak hata: kalibrasyonsuz %{np.mean(ham_hatalar):.0f} → kalibrasyonlu %{np.mean(kal_hatalar):.0f}")

    with open(mesafe.KALIBRASYON_DOSYASI, "w") as f:
        json.dump({"tarih": time.strftime("%Y-%m-%d %H:%M"), "kirpma": mesafe.DERINLIK_KIRPMA,
                   "model": mesafe.MODELLER[model_modu], "noktalar": [list(n) for n in egri]}, f, indent=1)
    yeni = not os.path.exists(CSV_DOSYASI)
    with open(CSV_DOSYASI, "a", newline="") as f:
        w = csv.writer(f)
        if yeni:
            w.writerow(["tarih", "gercek_m", "model_m", "yayilim_m", "kaynak", "kalibrasyonsuz_hata_yuzde",
                        "kalibrasyonlu_m", "kalibrasyonlu_hata_yuzde"])
        for s in satirlar:
            w.writerow([time.strftime("%Y-%m-%d %H:%M")] + s)
    print(f"\n✓ {mesafe.KALIBRASYON_DOSYASI} kaydedildi. Programı başlatın: systemctl --user start asistif-gozluk")


if __name__ == "__main__":
    main()
