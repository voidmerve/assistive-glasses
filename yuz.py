"""
Asistif Gözlük - Tanıdık Yüz Tanıma
===================================

Kullanıcı karşısındaki kişiyi sesle tanıtır ("bu Ayşe"), gözlük o kişiyi sonra
görünce bir kez söyler: "Saat 12 yönünde Ayşe var."

GİZLİLİK: Fotoğraf SAKLANMAZ. Her kişi için sadece yüzün 128 sayılık özeti
(SFace yüz vektörü) yuzler.json'a yazılır; bu sayılardan yüz görüntüsü geri
üretilemez. Hiçbir şey internete / Gemini'ye gitmez, her şey Pi'de çalışır.
Sadece izin veren kişiler tanıtılmalı; "Ayşe'yi unut" ile silinir.

Modeller (OpenCV, modeller/ klasöründe, ~38 MB):
  YuNet  (yüz bulma, ~15 ms)  face_detection_yunet_2023mar.onnx
  SFace  (yüz vektörü, ~60 ms) face_recognition_sface_2021dec.onnx
Sadece YOLO karede insan gördüğünde ve YUZ_ARALIK_S'de bir çalışır.

Ayarlar (.env):
  YUZ_TANIMA=1          # 0 = kapalı
  YUZ_ESIK=0.42         # kosinüs benzerliği; altındaysa isim söylenmez ("biri var")
"""

import json
import math
import os
import threading
import time

import cv2
import numpy as np

BURASI = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(BURASI, "modeller", "face_detection_yunet_2023mar.onnx")
SFACE = os.path.join(BURASI, "modeller", "face_recognition_sface_2021dec.onnx")
DOSYA = os.path.join(BURASI, "yuzler.json")

YUZ_ACIK = (os.getenv("YUZ_TANIMA") or "1").strip() != "0"
YUZ_ESIK = float(os.getenv("YUZ_ESIK") or 0.42)
YUZ_ARALIK_S = 1.5
ALGILAMA_GENISLIK = 640       # yüz bulma bu genişlikte yapılır (hız)
DUYURU_S = 300                # aynı kişi 5 dk içinde tekrar söylenmez
EN_FAZLA_ORNEK = 12           # kişi başına saklanan yüz vektörü


class YuzTanima:
    """
    tanidi: f(isim, saat_yonu) → tanıdık biri yeni göründü (DUYURU_S kuralıyla)
    insanlar_al: f() → [(x1, y1, x2, y2)] YOLO insan kutuları (yoksa yüz aranmaz)
    """

    def __init__(self, tanidi=None, insanlar_al=None, dosya=DOSYA):
        self.tanidi = tanidi
        self.insanlar_al = insanlar_al or (lambda: [None])
        self.dosya = dosya
        self.algilayici = self.tanici = None
        self._kilit = threading.Lock()
        self.kisiler = self._yukle()          # isim → [vektör, ...]
        self._son_islem = 0.0
        self._son_duyuru = {}
        self.son_gorulen = []                 # [(isim | None, saat_yonu, benzerlik, zaman)]
        self.sure_ms = 0.0

    # ---------- kayıt ----------
    def _yukle(self):
        try:
            with open(self.dosya) as f:
                return {k: [np.array(v, np.float32) for v in vs] for k, vs in json.load(f).items()}
        except Exception:
            return {}

    def _yaz(self):
        try:
            gecici = self.dosya + ".tmp"
            with open(gecici, "w") as f:
                json.dump({k: [v.round(5).tolist() for v in vs] for k, vs in self.kisiler.items()}, f)
            os.replace(gecici, self.dosya)
        except Exception as e:
            print(f"⚠️  Yüzler kaydedilemedi: {e}")

    # ---------- model ----------
    def yukle(self):
        if not YUZ_ACIK:
            return False
        if self.algilayici is not None:
            return True
        if not (os.path.exists(YUNET) and os.path.exists(SFACE)):
            print("⚠️  Yüz tanıma modelleri yok (modeller/), yüz tanıma kapalı")
            return False
        self.algilayici = cv2.FaceDetectorYN.create(YUNET, "", (320, 320), 0.8, 0.3, 50)
        self.tanici = cv2.FaceRecognizerSF.create(SFACE, "")
        print(f"🙂 Yüz tanıma hazır ({len(self.kisiler)} kişi tanıtılmış)")
        return True

    def _yuzler(self, kare):
        """Karedeki yüzler: [(yüz satırı tam kare ölçeğinde)] (YuNet biçimi: kutu + 5 nokta + güven)."""
        h, w = kare.shape[:2]
        olcek = min(1.0, ALGILAMA_GENISLIK / w)
        kucuk = cv2.resize(kare, (int(w * olcek), int(h * olcek))) if olcek < 1 else kare
        self.algilayici.setInputSize((kucuk.shape[1], kucuk.shape[0]))
        _, yuzler = self.algilayici.detect(kucuk)
        if yuzler is None:
            return []
        sonuc = []
        for y in yuzler:
            y = y.copy()
            y[:14] /= olcek
            sonuc.append(y)
        return sonuc

    def _vektor(self, kare, yuz):
        hizali = self.tanici.alignCrop(kare, yuz)
        v = self.tanici.feature(hizali).flatten().astype(np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    def _en_benzer(self, v):
        en_iyi, en_ad = -1.0, None
        for ad, vs in self.kisiler.items():
            b = max(float(np.dot(v, k)) for k in vs)
            if b > en_iyi:
                en_iyi, en_ad = b, ad
        return en_ad, en_iyi

    # ---------- görü hattından (mesafe.py thread'i) ----------
    def isle(self, kare):
        if self.algilayici is None or time.monotonic() - self._son_islem < YUZ_ARALIK_S:
            return
        if not self.insanlar_al():
            self.son_gorulen = []
            return
        self._son_islem = time.monotonic()
        t0 = time.time()
        h, w = kare.shape[:2]
        odak = (min(w, h) / 2) / math.tan(math.radians(67.0 / 2))
        yatay_fov = math.degrees(2 * math.atan((w / 2) / odak))
        gorulen = []
        with self._kilit:
            for yuz in self._yuzler(kare):
                if yuz[2] < 24:            # çok küçük yüz (uzak): tanıma güvenilmez
                    continue
                ad, benzerlik = self._en_benzer(self._vektor(kare, yuz)) if self.kisiler else (None, 0.0)
                if benzerlik < YUZ_ESIK:
                    ad = None
                aci = ((yuz[0] + yuz[2] / 2) / w - 0.5) * yatay_fov
                saat = int(round(aci / 30.0)) % 12 or 12
                gorulen.append((ad, saat, round(benzerlik, 2), time.time()))
        self.son_gorulen = gorulen
        self.sure_ms = (time.time() - t0) * 1000
        for ad, saat, benzerlik, _ in gorulen:
            if ad and time.monotonic() - self._son_duyuru.get(ad, -1e9) > DUYURU_S:
                self._son_duyuru[ad] = time.monotonic()
                print(f"🙂 Tanındı: {ad} (benzerlik {benzerlik}, saat {saat})")
                if self.tanidi:
                    self.tanidi(ad, saat)

    # ---------- sesli komutlar (Gemini fonksiyonları) ----------
    def tanit(self, isim, kare_al, sure_s=4.0):
        """Karşıdaki kişinin yüzünü isimle kaydeder. kare_al: f() → BGR kare."""
        isim = (isim or "").strip().title()
        if not isim:
            return "İsim anlaşılmadı."
        if self.algilayici is None:
            return "Yüz tanıma kapalı."
        vektorler, bitis = [], time.monotonic() + sure_s
        while time.monotonic() < bitis and len(vektorler) < 8:
            kare = kare_al()
            if kare is not None:
                with self._kilit:
                    yuzler = self._yuzler(kare)
                    if yuzler:
                        en_buyuk = max(yuzler, key=lambda y: y[2] * y[3])
                        if en_buyuk[2] >= 40:
                            vektorler.append(self._vektor(kare, en_buyuk))
            time.sleep(0.3)
        if len(vektorler) < 3:
            return ("Yüzü net göremedim. Kişi bir metre kadar yakında, yüzü kameraya dönük dursun, "
                    "tekrar deneyelim.")
        with self._kilit:
            # Başka bir kişiye çok benziyorsa uyar (yanlış kayıt / ikizler)
            ad, b = self._en_benzer(np.mean(vektorler, axis=0) / np.linalg.norm(np.mean(vektorler, axis=0)))
            onceki = self.kisiler.get(isim, [])
            self.kisiler[isim] = (onceki + vektorler)[-EN_FAZLA_ORNEK:]
            self._yaz()
        uyari = f" Not: {ad} ile çok benziyor, karışabilir." if ad and ad != isim and b > YUZ_ESIK else ""
        return f"{isim} tanıtıldı.{uyari}"

    def unut(self, isim):
        with self._kilit:
            hedef = next((k for k in self.kisiler if k.lower() == (isim or "").strip().lower()), None)
            if hedef is None:
                return f"{isim} adında tanıtılmış kimse yok."
            del self.kisiler[hedef]
            self._yaz()
        return f"{hedef} silindi, artık tanımayacağım."

    def kim_var(self):
        taze = [g for g in self.son_gorulen if time.time() - g[3] < 4]
        if not taze:
            return "Şu an karşında yüzünü görebildiğim kimse yok."
        bilinen = [f"saat {s} yönünde {a}" for a, s, _, _ in taze if a]
        bilinmeyen = sum(1 for a, *_ in taze if not a)
        parca = bilinen + ([f"{bilinmeyen} tanımadığım kişi"] if bilinmeyen else [])
        return ", ".join(parca) + "."

    def liste(self):
        return sorted(self.kisiler)
