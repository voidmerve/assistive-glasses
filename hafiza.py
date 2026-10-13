"""
Asistif Gözlük - Eşya Hafızası
==============================

Gözlük anahtar, cüzdan, telefon ve bilgisayarı her gördüğünde nerede olduğunu
bir cümleyle kaydeder: "masanın üzerinde, sol tarafta". GÖRÜNTÜ SAKLANMAZ,
sadece metin tarifi ve zaman saklanır (hafiza.json, git'e gönderilmez).

Kaynaklar:
  - Gemini Live (ev modunda birkaç saniyede bir kare bakar): dört eşyanın hepsi,
    zengin tarif ("mutfak tezgâhında, ekmek kutusunun yanında")
  - YOLO (cihazda, internetsiz): sadece telefon ve bilgisayar, tarifi kutuların
    konumundan çıkarılır ("masanın üzerinde, sol tarafta")

Sorulunca: "Anahtarını en son 10 dakika önce masanın üzerinde, sol tarafta gördüm."
"""

import json
import os
import threading
import time

DOSYA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hafiza.json")

# Takip edilen eşyalar ve kullanıcının onlara diyebileceği adlar
ESYALAR = {
    "anahtar": ["anahtar", "anahtarım", "anahtarlar", "anahtarlık", "key", "keys"],
    "cüzdan": ["cüzdan", "cüzdanım", "wallet", "para çantası"],
    "telefon": ["telefon", "telefonum", "cep telefonu", "cell phone", "phone", "iphone"],
    "bilgisayar": ["bilgisayar", "bilgisayarım", "dizüstü", "laptop", "notebook"],
}

# Gemini Live gördüyse YOLO'nun daha kaba tarifi bu süre boyunca onun üzerine yazmaz
GEMINI_ONCELIK_S = 120

# YOLO (COCO) sınıflarının Türkçe karşılıkları
YOLO_ESYA = {"cell phone": "telefon", "laptop": "bilgisayar"}
YOLO_YUZEY = {"dining table": "masanın", "bed": "yatağın", "couch": "kanepenin",
              "chair": "sandalyenin", "bench": "bankın", "toilet": "klozetin"}
YOLO_YAKIN = {"tv": "televizyonun", "refrigerator": "buzdolabının", "sink": "lavabonun",
              "oven": "fırının", "microwave": "mikrodalganın", "book": "kitabın",
              "cup": "bardağın", "keyboard": "klavyenin", "potted plant": "saksının"}


def esya_adi(metin):
    """Kullanıcının ya da modelin söylediği addan standart eşya adı, bilinmiyorsa None."""
    m = (metin or "").strip().lower()
    for ad, esler in ESYALAR.items():
        if m == ad or any(e in m for e in esler):
            return ad
    return None


def gecen_sure_metni(saniye):
    if saniye < 60:
        return "az önce"
    dk = int(saniye // 60)
    if dk < 60:
        return f"{dk} dakika önce"
    sa = int(dk // 60)
    if sa < 24:
        return f"{sa} saat önce"
    return f"{int(sa // 24)} gün önce"


class EsyaHafizasi:
    def __init__(self, dosya=DOSYA, saat=time.time, degisti=None):
        self.dosya = dosya
        self.saat = saat
        self.degisti = degisti  # f(esya, kayit): panele haber vermek için
        self._kilit = threading.Lock()
        self.kayitlar = self._yukle()

    def _yukle(self):
        try:
            with open(self.dosya) as f:
                return json.load(f)
        except Exception:
            return {}

    def _kaydet_dosya(self):
        try:
            gecici = self.dosya + ".tmp"
            with open(gecici, "w") as f:
                json.dump(self.kayitlar, f, ensure_ascii=False, indent=1)
            os.replace(gecici, self.dosya)
        except Exception as e:
            print(f"⚠️  Hafıza kaydedilemedi: {e}")

    def kaydet(self, esya, konum, kaynak="gemini"):
        """Eşyanın yerini kaydeder. Kaydedildiyse True."""
        ad = esya_adi(esya)
        konum = (konum or "").strip().rstrip(".")
        if ad is None or not konum:
            return False
        simdi = self.saat()
        with self._kilit:
            eski = self.kayitlar.get(ad)
            # Gemini'nin zengin tarifini kısa süre içinde YOLO'nun kaba tarifi ezmesin
            if (kaynak == "yolo" and eski and eski.get("kaynak") == "gemini"
                    and simdi - eski["zaman"] < GEMINI_ONCELIK_S):
                return False
            yeni = {"konum": konum, "zaman": simdi, "kaynak": kaynak}
            degisti = not eski or eski.get("konum") != konum
            self.kayitlar[ad] = yeni
            self._kaydet_dosya()
        if degisti:
            print(f"🧠 Hafıza: {ad} → {konum} ({kaynak})")
            if self.degisti:
                try:
                    self.degisti(ad, yeni)
                except Exception:
                    pass
        return True

    def sorgula(self, esya):
        """Söylenecek cümle."""
        ad = esya_adi(esya)
        if ad is None:
            return "Bunu takip etmiyorum. Anahtar, cüzdan, telefon ve bilgisayarın yerini hatırlıyorum."
        k = self.kayitlar.get(ad)
        if not k:
            return f"{ad.capitalize()} henüz hiç görmedim."
        ek = {"anahtar": "ını", "cüzdan": "ını", "telefon": "unu", "bilgisayar": "ını"}[ad]
        return f"{ad.capitalize()}{ek} en son {gecen_sure_metni(self.saat() - k['zaman'])} {k['konum']} gördüm."

    def liste(self):
        simdi = self.saat()
        return {ad: {**k, "once": gecen_sure_metni(simdi - k["zaman"])} for ad, k in self.kayitlar.items()}


# ═════════════ YOLO kutularından konum cümlesi ═════════════
def _yatay_yer(cx, genislik):
    oran = cx / genislik
    if oran < 0.36:
        return "sol tarafta"
    if oran > 0.64:
        return "sağ tarafta"
    return "tam önünde"


def yolo_konum_tarifi(esya_kutusu, digerleri, genislik):
    """
    esya_kutusu: (x1, y1, x2, y2). digerleri: [(sinif, (x1, y1, x2, y2)), ...]
    Eşyanın altındaki yüzeyi (masa, yatak, kanepe...) ya da yanındaki belirgin
    nesneyi bulup "masanın üzerinde, sol tarafta" gibi bir cümle kurar.
    """
    x1, y1, x2, y2 = esya_kutusu
    cx, alt = (x1 + x2) / 2, y2
    yer = _yatay_yer(cx, genislik)
    # Üzerinde durduğu yüzey: eşyanın alt-orta noktası yüzey kutusunun içinde
    for sinif, (a1, b1, a2, b2) in digerleri:
        if sinif in YOLO_YUZEY and a1 <= cx <= a2 and b1 - 10 <= alt <= b2:
            return f"{YOLO_YUZEY[sinif]} üzerinde, {yer}"
    # Yanındaki belirgin nesne: en yakın merkez
    en_yakin, en_mesafe = None, 1e9
    for sinif, (a1, b1, a2, b2) in digerleri:
        if sinif in YOLO_YAKIN:
            d = ((cx - (a1 + a2) / 2) ** 2 + ((y1 + y2) / 2 - (b1 + b2) / 2) ** 2) ** 0.5
            if d < en_mesafe:
                en_yakin, en_mesafe = sinif, d
    if en_yakin is not None and en_mesafe < genislik * 0.35:
        return f"{YOLO_YAKIN[en_yakin]} yanında, {yer}"
    return yer
