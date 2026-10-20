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
import math
import time

import cv2
import numpy as np

import mesafe
from goru import NesneTanima

BURASI = os.path.dirname(os.path.abspath(__file__))
CSV_DOSYASI = os.path.join(BURASI, "kalibrasyon_olcumleri.csv")
KARE_KLASORU = os.path.join(BURASI, "kalibrasyon_kareler")


def kamera_ac():
    from picamera2 import Picamera2
    kamera = Picamera2()
    kamera.configure(kamera.create_preview_configuration(main={"format": "RGB888", "size": (1280, 960)}))
    kamera.start()
    time.sleep(2)
    return kamera


def kisi_tahmini(olcer, yolo, kare):
    """Ortaya en yakın insan için (model tahmini, kutu boyutundan tahmin, kutu); insan yoksa None."""
    h, w = kare.shape[:2]
    yolo.isle(kare)
    insanlar = [t for t in yolo.son if t[1] == "person"]
    if not insanlar:
        return None
    harita, (kx0, ky0, kx1, ky1) = olcer.ham_derinlik(kare)
    GH, GW = harita.shape
    sx, sy = GW / (kx1 - kx0), GH / (ky1 - ky0)
    x1, y1, x2, y2 = min(insanlar, key=lambda t: abs((t[3][0] + t[3][2]) / 2 - w / 2))[3]
    a1 = int((x1 - kx0 + (x2 - x1) * 0.30) * sx)
    a2 = int((x1 - kx0 + (x2 - x1) * 0.70) * sx)
    b1 = int((y1 - ky0 + (y2 - y1) * 0.25) * sy)
    b2 = int((y1 - ky0 + (y2 - y1) * 0.75) * sy)
    a1, a2 = max(0, min(GW - 1, a1)), min(GW, max(a1 + 1, a2))
    b1, b2 = max(0, min(GH - 1, b1)), min(GH, max(b1 + 1, b2))
    model_d = float(np.median(harita[b1:b2, a1:a2]))
    # Programdaki geometrik ölçümün aynısı (yakında program buna güvenir)
    odak = (min(w, h) / 2) / math.tan(math.radians(mesafe.KAMERA_DIKEY_FOV / 2))
    geo = None
    if (y2 - y1) > 1.8 * (x2 - x1) and y1 > 4 and y2 < h - 4:
        geo = odak * mesafe.BILINEN_BOY["person"] / (y2 - y1)
    elif x1 > 4 and x2 < w - 4:
        geo = odak * mesafe.BILINEN_GENISLIK["person"] / (x2 - x1)
    return model_d, geo, (int(x1), int(y1), int(x2), int(y2))


def kare_kaydet(kare, kutu, hedef):
    os.makedirs(KARE_KLASORU, exist_ok=True)
    k = kare.copy()
    if kutu:
        cv2.rectangle(k, kutu[:2], kutu[2:], (0, 255, 0), 3)
    x0, y0, x1, y1 = mesafe.KameraMesafe.kirpma_kutusu(k.shape[1], k.shape[0])
    cv2.rectangle(k, (x0, y0), (x1, y1), (255, 0, 0), 2)  # derinlik modelinin gördüğü bölge
    o = 640 / max(k.shape[:2])
    cv2.imwrite(os.path.join(KARE_KLASORU, f"{hedef:g}m.jpg"), cv2.resize(k, (round(k.shape[1] * o), round(k.shape[0] * o))))


def tekdüze_aralik(olcumler):
    """Uzaktan yakına giderken model tahmininin de azaldığı en geniş aralık (yakında model şaşıyor)."""
    sirali = sorted(olcumler, key=lambda o: o[0])
    bas = len(sirali) - 1
    while bas > 0 and sirali[bas - 1][1] < sirali[bas][1]:
        bas -= 1
    return sirali[bas:], sirali[:bas]


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
        while True:
            input(f"→ Kişi kameranın tam karşısında {hedef:g} m'de, ayakta dursun. Hazır olunca Enter...")
            modeller, geolar, kutu, son_kare = [], [], None, None
            for _ in range(a.kare * 3):
                if len(modeller) >= a.kare:
                    break
                kare = mesafe.kare_dondur(kamera.capture_array())
                son_kare = kare
                if float(kare.mean()) < mesafe.KARANLIK_ESIGI:
                    continue
                sonuc = kisi_tahmini(olcer, yolo, kare)
                if sonuc:
                    modeller.append(sonuc[0])
                    if sonuc[1]:
                        geolar.append(sonuc[1])
                    kutu = sonuc[2]
                time.sleep(0.2)
            kare_kaydet(son_kare, kutu, hedef)
            if len(modeller) >= max(2, a.kare // 2):
                break
            parlak = float(son_kare.mean())
            neden = "görüntü karanlık (ışık / lens kapağı?)" if parlak < mesafe.KARANLIK_ESIGI else \
                "kameraya insan görünmüyor (kamera kişiye dönük mü?)"
            print(f"   ⚠️  Ölçüm alınamadı: {neden}. Görüntü: kalibrasyon_kareler/{hedef:g}m.jpg")
            if input("   Tekrar denemek için Enter, bu mesafeyi atlamak için 'a' + Enter: ").strip().lower() == "a":
                modeller = []
                break
        if not modeller:
            continue
        tahmin, yayilim = float(np.median(modeller)), float(np.std(modeller))
        geo = float(np.median(geolar)) if geolar else None
        olcumler.append((hedef, tahmin, yayilim, geo))
        geo_y = f"{geo:.2f} m" if geo else "—"
        print(f"   model {tahmin:.2f} m (±{yayilim:.2f}), kutu boyutu {geo_y} → gerçek {hedef:g} m\n")
    kamera.stop()

    kullanilan, atilan = tekdüze_aralik(olcumler)
    if atilan:
        print(f"\nℹ️  {', '.join(f'{o[0]:g}' for o in atilan)} m: model burada şaşıyor (yakını uzak sanıyor), eğriye "
              "alınmadı. Bu yakınlıkta program kutu boyutuna güvenir (tablodaki 'kutu' sütunu).")
    if len(kullanilan) < 3:
        print("Eğri için tutarlı en az 3 mesafe gerekli, kalibrasyon kaydedilmedi.")
        return

    noktalar = [(t, g) for g, t, _, _ in kullanilan]
    egri = egri_kur(noktalar)
    print("\n  Gerçek | Model (hata)     | Kutu boyutu (hata) | Kalibrasyonlu (hata, birini dışarıda bırak)")
    satirlar, ham_hatalar, kal_hatalar = [], [], []
    for gercek, tahmin, yayilim, geo in olcumler:
        icinde = (tahmin, gercek) in noktalar
        digerleri = egri_kur([n for n in noktalar if n != (tahmin, gercek)])
        duzeltilmis = egri_tahmin(digerleri, tahmin) if icinde and len(digerleri) >= 2 else None
        ham_h = (tahmin - gercek) / gercek * 100
        geo_h = (geo - gercek) / gercek * 100 if geo else None
        kal_h = (duzeltilmis - gercek) / gercek * 100 if duzeltilmis is not None else None
        if kal_h is not None:
            ham_hatalar.append(abs(ham_h))
            kal_hatalar.append(abs(kal_h))
        g_y = f"{geo:5.2f} m {geo_h:+5.0f} %" if geo else "      —        "
        k_y = f"{duzeltilmis:5.2f} m {kal_h:+5.0f} %" if kal_h is not None else "— (eğri dışı)"
        print(f"  {gercek:5.2f} m | {tahmin:5.2f} m {ham_h:+5.0f} % | {g_y}   | {k_y}")
        satirlar.append([gercek, round(tahmin, 3), round(yayilim, 3), None if geo is None else round(geo, 3),
                         None if geo_h is None else round(geo_h, 1), round(ham_h, 1),
                         None if duzeltilmis is None else round(duzeltilmis, 3),
                         None if kal_h is None else round(kal_h, 1)])
    print(f"\n  Eğri aralığında ortalama mutlak hata: kalibrasyonsuz %{np.mean(ham_hatalar):.0f} → "
          f"kalibrasyonlu %{np.mean(kal_hatalar):.0f}")
    print(f"  Her mesafenin görüntüsü: {KARE_KLASORU}/")

    with open(mesafe.KALIBRASYON_DOSYASI, "w") as f:
        json.dump({"tarih": time.strftime("%Y-%m-%d %H:%M"), "kirpma": mesafe.DERINLIK_KIRPMA,
                   "model": mesafe.MODELLER[model_modu], "noktalar": [list(n) for n in egri]}, f, indent=1)
    yeni = not os.path.exists(CSV_DOSYASI)
    with open(CSV_DOSYASI, "a", newline="") as f:
        w = csv.writer(f)
        if yeni:
            w.writerow(["tarih", "gercek_m", "model_m", "yayilim_m", "kutu_m", "kutu_hata_yuzde",
                        "kalibrasyonsuz_hata_yuzde", "kalibrasyonlu_m", "kalibrasyonlu_hata_yuzde"])
        for s in satirlar:
            w.writerow([time.strftime("%Y-%m-%d %H:%M")] + s)
    print(f"\n✓ {mesafe.KALIBRASYON_DOSYASI} kaydedildi. Programı başlatın: systemctl --user start asistif-gozluk")


if __name__ == "__main__":
    main()
