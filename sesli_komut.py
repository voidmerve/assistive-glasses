"""
Asistif Gözlük - İnternetsiz Sesli Komut Tanıma
===============================================

Uzun basışla alınan kayıt burada vosk (Türkçe model) ile yerelde yazıya
çevrilir. Komutlar internetsiz anlaşılır:
  "ev modu" / "sokak modu", "kısa konuş", "detaylı anlat", "sessiz ol",
  "daha çok anlat" / "daha az anlat", "durum ne", "tekrar et",
  "anahtarım nerede" (cüzdan, telefon, bilgisayar)
Gemini Live bağlıyken ses zaten Live'a canlı akar ve Live bu komutları
fonksiyon çağrısıyla anlar; vosk o zaman yedektir.

İleride Gemini Live'a geçildiğinde de internet yokken yedek olarak kalır.

Tek başına test:
  python3 sesli_komut.py kayit1.wav kayit2.wav ...
  python3 sesli_komut.py --metin "detaylı anlat"   # sadece eşleştirmeyi dener
"""

import os
import re
import sys
import json
import time
import wave
import difflib
import unicodedata
import threading

PROJE_DIZINI = os.path.dirname(os.path.abspath(__file__))
VARSAYILAN_MODEL = os.path.join(PROJE_DIZINI, "vosk_model")

KOMUT_KISA = "kisa"        # kısa konuşma düzeyi
KOMUT_DETAYLI = "detayli"  # detaylı konuşma düzeyi
KOMUT_SESSIZ = "sessiz"    # sadece sorunca konuş
KOMUT_DURUM = "durum"      # internet ve sensör durumu
KOMUT_TEKRAR = "tekrar"    # son söyleneni tekrarla
KOMUT_EV = "ev"            # ev moduna geç
KOMUT_SOKAK = "sokak"      # sokak moduna geç
KOMUT_DAHA_COK = "daha_cok"  # sokak anlatıcısı daha sık konuşsun
KOMUT_DAHA_AZ = "daha_az"    # daha seyrek
KOMUT_NEREDE = "nerede:"     # "nerede:anahtar" gibi döner

# Bir kaydın komut sayılması için en fazla kelime sayısı. Daha uzun cümleler
# ("kısa yoldan nasıl giderim") soru kabul edilir ve Gemini'ye gider.
MAKS_KOMUT_KELIME = 4

_TR_KUCUK = str.maketrans({"I": "ı", "İ": "i"})
_ASCII = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def normallestir(metin):
    """Türkçe küçük harf → ASCII → noktalama temizliği."""
    metin = metin.translate(_TR_KUCUK).lower().translate(_ASCII)
    # vosk "i̇" yazıyor (i + birleşik nokta). Birleşik işaretleri at, kelime bölünmesin.
    metin = "".join(c for c in unicodedata.normalize("NFD", metin)
                    if unicodedata.category(c) != "Mn")
    metin = re.sub(r"[^a-z0-9 ]+", " ", metin)
    return re.sub(r"\s+", " ", metin).strip()


def _benzer(kelime, hedefler, oran=0.75):
    return any(kelime == h or difflib.SequenceMatcher(None, kelime, h).ratio() >= oran
               for h in hedefler)


# Küçük model kelimeleri bazen yanlış yazar, bu yüzden benzerlikle eşleşiyoruz.
# Her komut: (anahtar kelimeler, tek başına yeterli mi). Tek başına yetmeyenler
# yanında _AYAR_FIILLERI'nden biri ister ("kısa" tek başına bir soru olabilir).
_KOMUTLAR = {
    KOMUT_TEKRAR: (["tekrar", "tekrarla", "tekrarlar", "tekrarlarmisin", "yinele"], True),
    KOMUT_DURUM: (["durum", "durumun", "durumu", "durumda"], True),
    KOMUT_SESSIZ: (["sessiz", "sessize", "sessizlik", "sus"], True),
    KOMUT_KISA: (["kisa", "kisalt", "kisaca"], False),
    KOMUT_DETAYLI: (["detayli", "detay", "ayrintili", "ayrinti", "uzun"], False),
}
_AYAR_FIILLERI = ["konus", "konusun", "konusma", "anlat", "soyle", "mod", "modu", "moda",
                  "moduna", "ol", "gec", "yap", "al", "ver"]
_TEKRAR_IFADELERI = [("ne", "dedin"), ("bir", "daha"), ("anlamadim",)]


def _kelime_eslesir(kelime, hedefler):
    # Kısa kelimeler benzerlikle eşleşirse ("sus" ~ "sis") yanlış komut olur: birebir ara
    if len(kelime) <= 3:
        return kelime in hedefler
    return _benzer(kelime, hedefler, 0.8)


# Ev / sokak modu: "ev" çok kısa ve vosk sık yanlış duyuyor ("hey", "il"), bu yüzden ayrı kurallar
_EV_KELIMELERI = ["ev", "eve", "evde", "evdeyim", "evdeyiz", "iceri", "iceriye", "icerdeyim"]
_SOKAK_KELIMELERI = ["sokak", "sokaga", "sokaktayim", "sokakta", "disari", "disariya",
                     "disardayim", "disaridayim"]
_MOD_FIILLERI = ["mod", "modu", "moduna", "modunu", "mode", "gec", "gecis", "gectim", "don", "dondum",
                 "cik", "cikiyorum", "ciktim", "cikalim", "gir", "girdim", "geldim"]
_ESYA_KELIMELERI = {
    "anahtar": ["anahtar", "anahtarim", "anahtarlar", "anahtarlarim", "anahtarlik"],
    "cüzdan": ["cuzdan", "cuzdanim"],
    "telefon": ["telefon", "telefonum"],
    "bilgisayar": ["bilgisayar", "bilgisayarim", "laptop", "laptopum"],
}


def _mod_komutu(kelimeler):
    ev = any(k in _EV_KELIMELERI if len(k) <= 3 else _benzer(k, _EV_KELIMELERI, 0.8) for k in kelimeler)
    sokak = any(_benzer(k, _SOKAK_KELIMELERI) for k in kelimeler)
    fiil = any(_benzer(k, _MOD_FIILLERI, 0.8) for k in kelimeler)
    if ev and sokak:
        return None
    if sokak:
        return KOMUT_SOKAK
    if ev and (fiil or len(kelimeler) == 1):
        return KOMUT_EV
    # "hey modu", "il moduna geç": sadece iki mod var, "sokak" geçmiyorsa ev modudur
    if len(kelimeler) >= 2 and len(kelimeler[0]) <= 3 and kelimeler[1] in ("modu", "moduna", "modunu"):
        return KOMUT_EV
    return None


def komut_bul(metin):
    """Yazıya çevrilmiş metinden KOMUT_* (ya da "nerede:<eşya>") veya None döner."""
    kelimeler = normallestir(metin).split()
    if not kelimeler or len(kelimeler) > MAKS_KOMUT_KELIME:
        return None

    if any(k in ("nerede", "nerde", "neredeydi", "nereye") for k in kelimeler):
        for esya, adlar in _ESYA_KELIMELERI.items():
            if any(_benzer(k, adlar, 0.8) for k in kelimeler):
                return KOMUT_NEREDE + esya
        return None  # "durak nerede" gibi: soru, Gemini'ye gitsin

    if "daha" in kelimeler and not ("bir" in kelimeler):
        if any(k in ("cok", "fazla", "sik") for k in kelimeler):
            return KOMUT_DAHA_COK
        if any(k in ("az", "seyrek") for k in kelimeler):
            return KOMUT_DAHA_AZ

    mod = _mod_komutu(kelimeler)
    if mod is not None:
        return mod

    for ifade in _TEKRAR_IFADELERI:
        if all(k in kelimeler for k in ifade):
            return KOMUT_TEKRAR

    fiil_var = any(_kelime_eslesir(k, _AYAR_FIILLERI) for k in kelimeler)
    bulunan = set()
    for komut, (anahtarlar, tek_basina) in _KOMUTLAR.items():
        if any(_kelime_eslesir(k, anahtarlar) for k in kelimeler):
            if tek_basina or fiil_var or len(kelimeler) == 1:
                bulunan.add(komut)
    if len(bulunan) == 1:
        return bulunan.pop()
    return None  # ya komut yok ya da belirsiz ("kısa değil detaylı")


class KomutTanici:
    """
    Vosk modelini arka planda yükler ve WAV dosyalarını yazıya çevirir.
    Model yüklenemezse sadece uyarı yazar, tani() her zaman (None, "") döner.
    """

    def __init__(self, model_yolu=VARSAYILAN_MODEL):
        self.model_yolu = model_yolu
        self.model = None
        self.hazir = threading.Event()
        self.hata = None
        self._kilit = threading.Lock()

    def yukle_arkaplanda(self):
        threading.Thread(target=self._yukle, daemon=True, name="vosk-yukle").start()
        return self

    def _yukle(self):
        try:
            t0 = time.time()
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)  # vosk'un kendi loglarını sustur
            if not os.path.isdir(self.model_yolu):
                raise FileNotFoundError(f"model klasörü yok: {self.model_yolu}")
            self.model = Model(self.model_yolu)
            print(f"🗣️  Sesli komut modeli hazır ({time.time() - t0:.1f} s)")
        except Exception as e:
            self.hata = e
            print(f"⚠️  Sesli komut modeli yüklenemedi, sadece klavyeyle mod değişir: "
                  f"{type(e).__name__}: {e}")
        finally:
            self.hazir.set()

    def yaziya_cevir(self, wav_yolu):
        from vosk import KaldiRecognizer
        with wave.open(wav_yolu, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise ValueError("WAV mono ve 16 bit olmalı")
            tanici = KaldiRecognizer(self.model, wf.getframerate())
            while True:
                veri = wf.readframes(4000)
                if not veri:
                    break
                tanici.AcceptWaveform(veri)
        return json.loads(tanici.FinalResult()).get("text", "")

    def tani(self, wav_yolu, bekle_s=0.0):
        """
        (komut, metin) döner. Komut yoksa veya model hazır değilse komut None olur.
        bekle_s: model henüz yükleniyorsa en fazla bu kadar bekle.
        """
        if not self.hazir.wait(timeout=bekle_s) or self.model is None:
            return None, ""
        try:
            with self._kilit:  # aynı anda iki tanıma olmasın
                t0 = time.time()
                metin = self.yaziya_cevir(wav_yolu)
            komut = komut_bul(metin)
            print(f"🗣️  Vosk ({time.time() - t0:.1f} s): \"{metin}\" → "
                  f"{'komut: ' + komut if komut else 'komut değil'}")
            return komut, metin
        except Exception as e:
            print(f"⚠️  Sesli komut tanıma hatası: {type(e).__name__}: {e}")
            return None, ""


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--metin":
        metin = " ".join(sys.argv[2:])
        print(f"\"{metin}\" → {komut_bul(metin)}")
        return
    if len(sys.argv) < 2:
        print(__doc__)
        return
    k = KomutTanici().yukle_arkaplanda()
    k.hazir.wait()
    for yol in sys.argv[1:]:
        komut, metin = k.tani(yol)
        print(f"{os.path.basename(yol)}: \"{metin}\" → {komut}")


if __name__ == "__main__":
    main()
