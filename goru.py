"""
Asistif Gözlük - Cihaz Üstü Nesne Tanıma (YOLO)
===============================================

yolov8n ile saniyede ~1 kare işler (Pi 4'te 320 px kare ~0,33 sn). İnternet gerekmez.

Sonuçların iki kullanımı var:
  1. Jüri paneli: canlı görüntünün üstüne Türkçe etiketli kutular çizilir.
  2. Ev modunda eşya hafızası: telefon ve bilgisayar görülünce yeri kaydedilir
     ("masanın üzerinde, sol tarafta"). Anahtar ve cüzdan YOLO'nun sınıflarında
     yok, onları Gemini Live kaydeder.

Ayarlar (.env):
  YOLO=1            # 0 = kapalı (işlemci yükünü azaltmak için)
  YOLO_ARALIK_S=1.0
"""

import os
import threading
import time

import cv2

YOLO_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolov8n.pt")
YOLO_ACIK = (os.getenv("YOLO") or "1").strip() not in ("0", "hayir", "false")
try:
    YOLO_ARALIK_S = float(os.getenv("YOLO_ARALIK_S") or 1.0)
except ValueError:
    YOLO_ARALIK_S = 1.0

TURKCE = {
    "person": "insan", "bicycle": "bisiklet", "car": "araba", "motorcycle": "motosiklet",
    "bus": "otobüs", "truck": "kamyon", "traffic light": "trafik ışığı", "stop sign": "dur işareti",
    "bench": "bank", "dog": "köpek", "cat": "kedi", "backpack": "sırt çantası", "umbrella": "şemsiye",
    "handbag": "çanta", "suitcase": "bavul", "bottle": "şişe", "cup": "bardak", "fork": "çatal",
    "knife": "bıçak", "spoon": "kaşık", "bowl": "kase", "chair": "sandalye", "couch": "kanepe",
    "potted plant": "saksı", "bed": "yatak", "dining table": "masa", "toilet": "klozet",
    "tv": "televizyon", "laptop": "bilgisayar", "mouse": "fare", "remote": "kumanda",
    "keyboard": "klavye", "cell phone": "telefon", "microwave": "mikrodalga", "oven": "fırın",
    "sink": "lavabo", "refrigerator": "buzdolabı", "book": "kitap", "clock": "saat",
    "vase": "vazo", "scissors": "makas", "fire hydrant": "yangın musluğu",
    "parking meter": "parkmetre", "train": "tren", "bird": "kuş", "apple": "elma",
    "banana": "muz", "orange": "portakal", "teddy bear": "oyuncak ayı", "toothbrush": "diş fırçası",
}


class NesneTanima(threading.Thread):
    """
    kare_al: f() → RGB kare (numpy) ya da None
    esya_gordu: f(sinif, kutu, digerleri, genislik) → hafızaya yazmak için (ev modunda)
    """

    def __init__(self, kare_al, esya_gordu=None, aralik=YOLO_ARALIK_S, guven=0.4):
        super().__init__(daemon=True, name="yolo")
        self.kare_al = kare_al
        self.esya_gordu = esya_gordu
        self.aralik = aralik
        self.guven = guven
        self.model = None
        self.son = []          # [(sinif_tr, sinif_en, guven, (x1,y1,x2,y2))] kare boyutunda
        self.son_zaman = 0.0
        self.sure_ms = 0.0
        self.boyut = (640, 480)
        self.durdur_olayi = threading.Event()

    def run(self):
        try:
            from ultralytics import YOLO
            t0 = time.time()
            self.model = YOLO(YOLO_MODEL)
            print(f"👁️  YOLO hazır ({time.time() - t0:.1f} s)")
        except Exception as e:
            print(f"⚠️  YOLO yüklenemedi, panelde kutu olmayacak: {type(e).__name__}: {e}")
            return
        while not self.durdur_olayi.is_set():
            t0 = time.time()
            try:
                kare = self.kare_al()
                if kare is not None:
                    self._isle(kare)
            except Exception as e:
                print(f"⚠️  YOLO hatası: {type(e).__name__}: {e}")
            self.durdur_olayi.wait(max(0.05, self.aralik - (time.time() - t0)))

    def _isle(self, kare):
        t0 = time.time()
        h, w = kare.shape[:2]
        sonuc = self.model(kare, imgsz=320, conf=self.guven, verbose=False)[0]
        tespitler = []
        for kutu in sonuc.boxes:
            sinif = self.model.names[int(kutu.cls)]
            x1, y1, x2, y2 = (float(v) for v in kutu.xyxy[0])
            tespitler.append((TURKCE.get(sinif, sinif), sinif, float(kutu.conf), (x1, y1, x2, y2)))
        self.son, self.son_zaman, self.boyut = tespitler, time.time(), (w, h)
        self.sure_ms = (time.time() - t0) * 1000
        if self.esya_gordu:
            digerleri = [(t[1], t[3]) for t in tespitler]
            for _, sinif, _, kutu in tespitler:
                if sinif in ("cell phone", "laptop"):
                    self.esya_gordu(sinif, kutu, digerleri, w)

    def ciz(self, kare_bgr):
        """Son tespitleri BGR karenin üstüne çizer (panel için). 2 sn'den eskiyse çizmez."""
        if time.time() - self.son_zaman > 2.0:
            return kare_bgr
        for ad, _, guven, (x1, y1, x2, y2) in self.son:
            p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
            cv2.rectangle(kare_bgr, p1, p2, (40, 220, 120), 2)
            etiket = f"{_ascii(ad)} {guven:.0%}"
            (tw, th), _ = cv2.getTextSize(etiket, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(kare_bgr, (p1[0], p1[1] - th - 6), (p1[0] + tw + 4, p1[1]), (40, 220, 120), -1)
            cv2.putText(kare_bgr, etiket, (p1[0] + 2, p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        return kare_bgr

    def ozet(self):
        """Panel için: [{ad, guven}] ve süre."""
        return {"nesneler": [{"ad": t[0], "guven": round(t[2], 2)} for t in self.son],
                "sure_ms": round(self.sure_ms)}

    def durdur(self):
        self.durdur_olayi.set()


def _ascii(metin):
    """OpenCV yazı tipi Türkçe harfleri çizemez."""
    return metin.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))
