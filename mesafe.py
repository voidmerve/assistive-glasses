"""
Asistif Gözlük - Kameradan Mesafe (derinlik yapay zekâsı)
=========================================================

Lazer sensör yokken (ya da yanında) mesafeyi tek kameradan tahmin eder.

Model: Depth Anything V2 Metric Small (metre cinsinden derinlik verir).
  Ev modunda iç mekân sürümü (Hypersim ile eğitilmiş), sokak modunda dış mekân
  sürümü (Virtual KITTI ile eğitilmiş). Bellekte aynı anda sadece biri durur.

Hız: Eski kod Hugging Face hattını kullanıyordu; hat her kareyi 518 piksele
  büyüttüğü için Pi 4'te kare başına ~5 sn sürüyordu. Burada model doğrudan
  252 × 196 girişle çağrılıyor: ~0,9 sn. DERINLIK_ARALIK_S'de bir çalışır.

Çıktılar (son_olcum):
  tam_on   : görüntünün ortasındaki en yakın şeyin mesafesi (m)
  bolgeler : sol / orta / sağ en yakın mesafeler
  nesneler : YOLO'nun bulduğu her nesne için ad, saat yönü, mesafe
             ("saat 1 yönünde sandalye, 1,8 metre")
  olcek    : kendi kendine kalibrasyon katsayısı

Kendi kendine kalibrasyon: Boyu bilinen nesneler (insan ~1,70 m, araba ~1,50 m,
  sandalye ~0,90 m...) görüntüdeki yüksekliklerinden de mesafe verir
  (iğne deliği kamera: mesafe = odak × gerçek boy / piksel boyu). Bu iki
  tahminin oranı zamanla ortalanır ve derinlik haritası bu oranla düzeltilir.
  Kesilmiş (kare kenarına değen) kutular hesaba katılmaz.

Kameradan engel uyarısı (KAMERA_UYARI=1):
  Yürüme yolunun ortasında ENGEL_UYARI_M'den yakın bir şey varsa açık tarafı
  söyler: "Önünde sandalye var, sağdan geç"; ENGEL_DUR_M'den yakınsa önce bip +
  "Dur". Durduğun yerde de çalışır. Saniyede ~0,8 ölçümle çalıştığı için lazer
  sensör kadar hızlı değildir: yavaş yürüyüş için uygundur.

Ayarlar (.env):
  DERINLIK_ARALIK_S=0.6    # turlar arası mola (YOLO + derinlik bir tur ~1,3 sn)
  GORU_CEKIRDEK=3          # 4 çekirdeğin biri Live, ses ve panel için boş kalsın
  ENGEL_UYARI_M=1.3
  ENGEL_DUR_M=0.7
  KAMERA_DIKEY_FOV=67      # Camera Module 3 Wide dikey görüş açısı (derece)
  KAMERA_UYARI=1
  DERINLIK_MODELI=ic       # ic | dis | mod (mod: ev → iç, sokak → dış). İç mekân modeli yakın mesafede en doğrusu
  DERINLIK_CARPAN=0.75     # gerçek / gösterilen mesafe (1-2-3 m testiyle bulunur)
"""

import math
import os
import threading
import time

import cv2
import numpy as np


def _sayi(ad, varsayilan):
    try:
        return float(os.getenv(ad) or varsayilan)
    except ValueError:
        return varsayilan


DERINLIK_ARALIK_S = _sayi("DERINLIK_ARALIK_S", 0.6)   # turlar arası mola; tur ~1,3 sn → ölçüm ~2 sn'de bir
# Görü hattı en fazla bu kadar çekirdek kullanır: 4'ünü de alınca Live cevapları 5-15 sn gecikiyordu
GORU_CEKIRDEK = int(_sayi("GORU_CEKIRDEK", 3))
ENGEL_UYARI_M = _sayi("ENGEL_UYARI_M", 1.3)           # yürüme yolunda bundan yakın: "Önünde sandalye var"
ENGEL_DUR_M = _sayi("ENGEL_DUR_M", 0.7)               # bundan yakın: bip + "Dur"
ENGEL_TEKRAR_S = _sayi("ENGEL_TEKRAR_S", 8.0)         # engel durdukça hatırlatma aralığı
KAMERA_DIKEY_FOV = _sayi("KAMERA_DIKEY_FOV", 67.0)
KAMERA_UYARI = (os.getenv("KAMERA_UYARI") or "1").strip() not in ("0", "hayir", "false")
# mod: ev → iç mekân, sokak → dış mekân modeli. ic / dis: her zaman o model
# (sunum salonda yapılacaksa sokak modunda da doğru mesafe için DERINLIK_MODELI=ic)
DERINLIK_MODELI = (os.getenv("DERINLIK_MODELI") or "ic").strip().lower()
# Geniş açılı lens (Camera Module 3 Wide) nesneleri küçük gösterir; model eğitildiği kameralara
# göre mesafeyi büyük tahmin eder. Ölçülen gerçek / gösterilen oranı (ör. 3 m'yi 4 m gösteriyorsa 0,75).
DERINLIK_CARPAN = _sayi("DERINLIK_CARPAN", 1.0)

MODELLER = {
    "ev": "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf",
    "sokak": "depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf",
}
GIRIS_W, GIRIS_H = 252, 196  # 14'ün katları (ViT yama boyu)
ORTALAMA = np.array([0.485, 0.456, 0.406], np.float32)
SAPMA = np.array([0.229, 0.224, 0.225], np.float32)

# Boyu yaklaşık bilinen COCO sınıfları (m): ölçek kalibrasyonu için
BILINEN_BOY = {"person": 1.70, "car": 1.50, "bus": 3.00, "truck": 3.00, "chair": 0.90,
               "bicycle": 1.05, "motorcycle": 1.10, "dog": 0.55, "bench": 0.85,
               "fire hydrant": 0.75, "refrigerator": 1.75, "toilet": 0.75}


def saat_yonu(x_oran, yatay_fov):
    """Görüntüdeki yatay konum (0..1) → saat yönü (10..2)."""
    aci = (x_oran - 0.5) * yatay_fov
    saat = round(12 + aci / 30.0)
    return 12 if saat == 0 else (saat - 12 if saat > 12 else saat)


def mesafe_yazisi(m):
    return f"{m:.1f}".replace(".", ",") + " metre"


class KameraMesafe(threading.Thread):
    """
    kare_al: f() → BGR kare (picamera2 RGB888 biçimi BGR sıralıdır)
    mod_al: f() → "ev" | "sokak"
    tespitler_al: f() → [(ad_tr, sinif_en, guven, (x1,y1,x2,y2))] (YOLO, isteğe bağlı)
    uyari: f(seviye, metin) → yaklaşma uyarısı (seviye "dikkat" | "dur")
    """

    def __init__(self, kare_al, mod_al, yolo=None, uyari=None, aralik=DERINLIK_ARALIK_S):
        """yolo: goru.NesneTanima. Bu thread onu her karede derinlikten hemen önce çalıştırır:
        ikisi aynı anda çalışınca 4 çekirdeği paylaşıp birbirini 3-4 kat yavaşlatıyordu."""
        super().__init__(daemon=True, name="gorus")
        self.kare_al, self.mod_al = kare_al, mod_al
        self.yolo = yolo
        self.tespitler_al = (lambda: yolo.son) if yolo is not None else (lambda: [])
        self.uyari = uyari
        self.aralik = aralik
        self.model, self.model_modu = None, None
        self.olcek = 1.0           # kalibrasyon katsayısı (boy tahmini / derinlik tahmini)
        self._olcek_ornek = 0
        self.son_olcum = None
        self.son_harita = None     # renkli derinlik haritası (panel için, BGR)
        self.engel_seviye = 0       # 0 yok, 1 dikkat, 2 dur
        self._son_uyari = 0.0
        self.son_engel = None
        self.durdur_olayi = threading.Event()
        self._kilit = threading.Lock()

    # ---------- model ----------
    def _model_yukle(self, mod):
        print(f"📐 Derinlik modeli yükleniyor ({'iç' if mod == 'ev' else 'dış'} mekân)...")
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModelForDepthEstimation
        t0 = time.time()
        try:
            yol = snapshot_download(MODELLER[mod], local_files_only=True)
        except Exception:
            yol = snapshot_download(MODELLER[mod])  # ilk kez: internetten indir (~95 MB)
        self.model = None  # önce eskisini bırak: bellekte iki model durmasın
        model = AutoModelForDepthEstimation.from_pretrained(yol).eval()
        self.torch = torch
        self.model, self.model_modu = model, mod
        self.olcek, self._olcek_ornek = 1.0, 0  # modeller farklı ölçekte hata yapar
        print(f"📐 Derinlik modeli hazır: {'iç mekân' if mod == 'ev' else 'dış mekân'} "
              f"({time.time() - t0:.1f} s)")

    # ---------- döngü ----------
    def run(self):
        try:
            import torch
            torch.set_num_threads(GORU_CEKIRDEK)
        except Exception:
            pass
        if self.yolo is not None:
            self.yolo.yukle()
        while not self.durdur_olayi.is_set():
            t0 = time.time()
            try:
                mod = self.mod_al()
                if DERINLIK_MODELI in ("ic", "iç"):
                    mod = "ev"
                elif DERINLIK_MODELI in ("dis", "dış"):
                    mod = "sokak"
                if self.model_modu != mod:
                    self._model_yukle(mod)
                kare = self.kare_al()
                if kare is not None:
                    if self.yolo is not None:
                        self.yolo.isle(kare)
                    self._isle(kare)
            except Exception as e:
                print(f"⚠️  Derinlik hatası: {type(e).__name__}: {e}")
                self.durdur_olayi.wait(5)
            self.durdur_olayi.wait(max(0.1, self.aralik - (time.time() - t0)))

    def derinlik_haritasi(self, kare_bgr):
        """BGR kare → metre cinsinden derinlik (GIRIS_H × GIRIS_W)."""
        rgb = cv2.cvtColor(cv2.resize(kare_bgr, (GIRIS_W, GIRIS_H), interpolation=cv2.INTER_AREA),
                           cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = ((rgb - ORTALAMA) / SAPMA).transpose(2, 0, 1)[None]
        with self.torch.inference_mode():
            d = self.model(pixel_values=self.torch.from_numpy(x)).predicted_depth
        d = d.squeeze().numpy()
        if d.shape != (GIRIS_H, GIRIS_W):
            d = cv2.resize(d, (GIRIS_W, GIRIS_H))
        return d

    def _isle(self, kare):
        t0 = time.time()
        h, w = kare.shape[:2]
        ham = self.derinlik_haritasi(kare) * DERINLIK_CARPAN  # sabit lens düzeltmesi
        derinlik = ham * self.olcek                            # + boy kalibrasyonunun ince ayarı
        odak = (h / 2) / math.tan(math.radians(KAMERA_DIKEY_FOV / 2))   # piksel
        yatay_fov = math.degrees(2 * math.atan((w / 2) / odak))

        # Bölgeler: orta yükseklik bandında en yakın %15'lik dilim (tek piksel gürültüsüne dayanıklı)
        bant = derinlik[int(GIRIS_H * 0.30):int(GIRIS_H * 0.75)]
        ucte = GIRIS_W // 3
        bolgeler = {ad: float(np.percentile(bant[:, a:b], 15))
                    for ad, (a, b) in {"sol": (0, ucte), "orta": (ucte, 2 * ucte), "sag": (2 * ucte, GIRIS_W)}.items()}

        # Nesneler: kutunun alt-orta kısmındaki medyan derinlik (arka planı dışarıda bırakır)
        nesneler, oranlar = [], []
        sx, sy = GIRIS_W / w, GIRIS_H / h
        for ad, sinif, guven, (x1, y1, x2, y2) in self.tespitler_al():
            a1, a2 = int(x1 * sx + (x2 - x1) * sx * 0.25), int(x1 * sx + (x2 - x1) * sx * 0.75)
            b1, b2 = int(y1 * sy + (y2 - y1) * sy * 0.35), int(y1 * sy + (y2 - y1) * sy * 0.90)
            parca = ham[max(0, b1):max(b1 + 1, b2), max(0, a1):max(a1 + 1, a2)]
            if parca.size == 0:
                continue
            d_ham = float(np.median(parca))
            mesafe = d_ham * self.olcek
            kaynak = "derinlik"
            # Boy tahmini: kutu kare kenarına değmiyorsa (nesne kesilmemişse)
            ayakta = sinif != "person" or (y2 - y1) > 1.8 * (x2 - x1)  # oturan insan 1,70 m değildir
            if sinif in BILINEN_BOY and ayakta and y1 > 4 and y2 < h - 4 and (y2 - y1) > 20:
                d_boy = odak * BILINEN_BOY[sinif] / (y2 - y1)
                if 0.3 < d_boy < 30:
                    oranlar.append(d_boy / max(d_ham, 0.1))
                    mesafe, kaynak = (mesafe + d_boy) / 2, "derinlik+boy"
            nesneler.append({"ad": ad, "sinif": sinif, "saat": saat_yonu((x1 + x2) / 2 / w, yatay_fov),
                             "mesafe": round(mesafe, 2), "kaynak": kaynak, "guven": round(guven, 2),
                             "kutu": [int(x1), int(y1), int(x2), int(y2)]})
        self._olcek_guncelle(oranlar)
        nesneler.sort(key=lambda n: n["mesafe"])

        # Yürüme yolu: görüntünün alt-orta bandı (gözlük kamerası yerdeki ve göğüs hizasındaki engeli burada görür)
        yol = derinlik[int(GIRIS_H * 0.35):int(GIRIS_H * 0.92)]
        yol_bolgeler = {ad: float(np.percentile(yol[:, a:b], 10))
                        for ad, (a, b) in {"sol": (0, ucte), "orta": (ucte, 2 * ucte), "sag": (2 * ucte, GIRIS_W)}.items()}
        tam_on = min(bolgeler["orta"], yol_bolgeler["orta"])
        olcum = {"zaman": time.time(), "tam_on": round(tam_on, 2),
                 "bolgeler": {k: round(v, 2) for k, v in bolgeler.items()},
                 "yol": {k: round(v, 2) for k, v in yol_bolgeler.items()}, "nesneler": nesneler,
                 "olcek": round(self.olcek, 3), "model": self.model_modu,
                 "sure_ms": round((time.time() - t0) * 1000)}
        renkli = cv2.applyColorMap(
            (255 - np.clip(derinlik / 6.0, 0, 1) * 255).astype(np.uint8), cv2.COLORMAP_TURBO)  # yakın = kırmızı
        with self._kilit:
            self.son_olcum, self.son_harita = olcum, renkli
        self._engel_kontrol(olcum, w)

    def _olcek_guncelle(self, oranlar):
        """Boy tahminiyle derinlik tahmininin oranını yavaşça ortalar (aykırı değerlere dayanıklı)."""
        if not oranlar:
            return
        oran = float(np.median(oranlar))
        if not 0.4 < oran < 2.5:
            return  # büyük olasılıkla yanlış eşleşme
        # oran ham derinliğe göre hesaplandığı için doğrudan hedef ölçektir
        agirlik = 0.2 if self._olcek_ornek < 10 else 0.05
        yeni = math.exp((1 - agirlik) * math.log(self.olcek) + agirlik * math.log(oran))
        self.olcek = min(2.0, max(0.5, yeni))
        self._olcek_ornek += 1

    def _engel_kontrol(self, olcum, genislik):
        """
        Yürüme yolunun ortasında yakın bir şey varsa uyarır; durduğun yerde de çalışır
        (sensör yokken hıza bakılamaz). Açık tarafı söyler: "Önünde sandalye var, sağdan geç".
          ortası < ENGEL_DUR_M   → seviye 2 (bip + "Dur")
          ortası < ENGEL_UYARI_M → seviye 1
          ortası > ENGEL_UYARI_M + 0,3 → temiz (histerezis: eşikte titreyip açılıp kapanmasın)
        Seviye yükselince hemen, aynı seviyede kaldıkça ENGEL_TEKRAR_S'de bir söylenir.
        """
        yol, simdi = olcum["yol"], olcum["zaman"]
        orta = yol["orta"]
        eski = self.engel_seviye
        # Her eşiğin kendi histerezisi: eşikte titreyen ölçüm uyarıyı açıp kapatmasın
        if orta < ENGEL_DUR_M or (eski == 2 and orta < ENGEL_DUR_M + 0.15):
            seviye = 2
        elif orta < ENGEL_UYARI_M or (eski >= 1 and orta < ENGEL_UYARI_M + 0.3):
            seviye = 1
        else:
            seviye = 0
        self.engel_seviye = seviye
        if seviye == 0 or not self.uyari:
            self.son_engel = None
            return
        if seviye <= eski and simdi - self._son_uyari < ENGEL_TEKRAR_S:
            return
        # Engelin adı: ortadaki, mesafesi yol ortasına yakın YOLO nesnesi
        ad, sinif = "engel", None
        for n in olcum["nesneler"]:
            x1, _, x2, _ = n["kutu"]
            if x1 < genislik * 0.67 and x2 > genislik * 0.33 and n["mesafe"] < orta + 0.6:
                ad, sinif = n["ad"], n["sinif"]
                break
        # Açık taraf: diğer iki bölgeden belirgin uzak olan
        sol, sag = yol["sol"], yol["sag"]
        if max(sol, sag) > max(orta + 0.5, ENGEL_UYARI_M):
            taraf = "sol" if sol > sag else "sag"
        else:
            taraf = None
        self._son_uyari = simdi
        self.son_engel = {"seviye": seviye, "ad": ad, "sinif": sinif, "taraf": taraf, "mesafe": round(orta, 2),
                          "tekrar": seviye <= eski}  # aynı engelin hatırlatması mı, yeni/yükselen mi
        self.uyari(self.son_engel)

    # ---------- dışarıya ----------
    def guncel(self, en_eski_s=6.0):
        """Son ölçüm, eskiyse None."""
        with self._kilit:
            o = self.son_olcum
        if o is None or time.time() - o["zaman"] > en_eski_s:
            return None
        return o

    def ozet_metni(self, en_fazla=4):
        """Live'a ve REST'e verilen mesafe bilgisi."""
        o = self.guncel()
        if o is None:
            return ""
        parcalar = [f"tam önde (saat 12) yaklaşık {mesafe_yazisi(o['tam_on'])}"]
        for n in o["nesneler"][:en_fazla]:
            parcalar.append(f"{n['ad']} saat {n['saat']} yönünde, yaklaşık {mesafe_yazisi(n['mesafe'])}")
        return ("Kamera derinlik tahmini (yaklaşık, ±%20): " + "; ".join(parcalar) + ". "
                "Bu mesafeleri 'yaklaşık' diyerek kullan, başka mesafe uydurma.")

    def harita_jpeg(self):
        with self._kilit:
            h = self.son_harita
        if h is None:
            return None
        _, buf = cv2.imencode(".jpg", cv2.resize(h, (320, 248)), [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes()

    def durdur(self):
        self.durdur_olayi.set()
