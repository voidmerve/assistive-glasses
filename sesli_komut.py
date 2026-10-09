"""
Asistif Gözlük - İnternetsiz Sesli Komut Tanıma
===============================================

SPACE ile alınan kayıt Gemini'ye gitmeden önce burada vosk (Türkçe model) ile
yerelde yazıya çevrilir. Kayıt bir mod komutuysa ("ev modu", "sokak modu" ...)
mod değiştirilir ve kayıt Gemini'ye hiç gönderilmez.

İleride Gemini Live'a geçildiğinde de internet yokken yedek olarak kalır.

Tek başına test:
  python3 sesli_komut.py kayit1.wav kayit2.wav ...
  python3 sesli_komut.py --metin "sokağa çıkıyorum"   # sadece eşleştirmeyi dener
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

KOMUT_EV = "ev"
KOMUT_SOKAK = "sokak"

# Bir kaydın komut sayılması için en fazla kelime sayısı. Daha uzun cümleler
# ("sokak modunda durak nerede") soru kabul edilir ve Gemini'ye gider.
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
_EV_KELIMELERI = ["ev", "eve", "evde", "evdeyim", "evdeyiz", "iceri", "iceriye", "icerdeyim"]
_SOKAK_KELIMELERI = ["sokak", "sokaga", "sokaktayim", "sokakta", "disari", "disariya",
                     "disardayim", "disaridayim"]
_MOD_KELIMELERI = ["mod", "modu", "moduna", "modunu", "mode"]
_FIIL_KELIMELERI = ["gec", "gecis", "gectim", "don", "dondum", "cik", "cikiyorum", "ciktim",
                    "cikalim", "gir", "girdim", "geldim", "ac", "baslat"]


def komut_bul(metin):
    """Yazıya çevrilmiş metinden KOMUT_EV, KOMUT_SOKAK ya da None döner."""
    kelimeler = normallestir(metin).split()
    if not kelimeler or len(kelimeler) > MAKS_KOMUT_KELIME:
        return None

    # "ev" çok kısa, benzerlikle eşleşirse "en", "et" gibi kelimeler de tutar: birebir ara
    ev = any(k in _EV_KELIMELERI if len(k) <= 3 else _benzer(k, _EV_KELIMELERI, 0.8)
             for k in kelimeler)
    sokak = any(_benzer(k, _SOKAK_KELIMELERI) for k in kelimeler)
    mod_ya_da_fiil = any(_benzer(k, _MOD_KELIMELERI + _FIIL_KELIMELERI, 0.8) for k in kelimeler)

    if ev and sokak:
        return None  # belirsiz
    # "sokak" / "dışarı" kelimesi tek başına da yeterli ("sokağa", "dışarıdayım")
    if sokak:
        return KOMUT_SOKAK
    # "ev" tek başına çok genel ("ev nerede"), yanında mod ya da fiil olmalı
    if ev and (mod_ya_da_fiil or len(kelimeler) == 1):
        return KOMUT_EV
    # Model "ev"i sık sık "hey", "il" diye duyuyor. Sadece iki mod var: "sokak"
    # geçmeyen ve "modu"dan önce kısa bir kelime olan komut ev modudur.
    if len(kelimeler) >= 2 and len(kelimeler[0]) <= 3 and kelimeler[1] in ("modu", "moduna", "modunu"):
        return KOMUT_EV
    return None


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
