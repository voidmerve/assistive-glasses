"""
Asistif Gözlük - Tek Düğmeli Kontrol
====================================

Gözlüğün sapındaki tek düğme:
  kısa basış  → "Önümde ne var?" (konuşma sürüyorsa önce susturur)
  uzun basış  → basılı tuttuğun sürece dinler, bırakınca soruyu işler
  çift basış  → son söyleneni tekrarlar

Düğme bir GPIO pini ile GND arasına bağlanır (dahili pull-up kullanılır).
.env içinde DUGME_PIN=17 gibi BCM numarası verilir. Boşsa sadece klavye çalışır.

Düğme gelene kadar klavye (SSH terminali, Enter'sız):
  d ya da Enter → kısa basış
  SPACE         → uzun basış başlat / bitir (terminal tuşun bırakıldığını göremez)
  r             → çift basış
  q             → çıkış

Bu modül sadece olay üretir: KISA, UZUN_BASLADI, UZUN_BITTI, CIFT, CIKIS.
Olayların ne yapacağına ana program karar verir.
"""

import os
import threading

KISA = "kisa"
UZUN_BASLADI = "uzun_basladi"
UZUN_BITTI = "uzun_bitti"
CIFT = "cift"
CIKIS = "cikis"

UZUN_BASIS_S = 0.5     # bundan uzun basılı tutmak = uzun basış
CIFT_BASIS_S = 0.35    # iki kısa basış arası bundan kısaysa = çift basış


class Dugme:
    """
    olay_geldi(olay) her olayda düğmenin kendi thread'inden çağrılır,
    bu yüzden hızlı dönmeli (ağır işler ana döngüye bırakılmalı).
    """

    def __init__(self, olay_geldi, pin=None):
        self.olay_geldi = olay_geldi
        self.pin = (pin if pin is not None else os.getenv("DUGME_PIN") or "").strip()
        self.cihaz = None
        self._uzun = False
        self._klavye_uzun = False
        self._bekleyen_kisa = None
        self._kilit = threading.Lock()

    def baslat(self):
        if not self.pin:
            print("🔘 Fiziksel düğme tanımlı değil (DUGME_PIN boş), klavye kullanılıyor")
            return self
        try:
            from gpiozero import Button
            self.cihaz = Button(int(self.pin), pull_up=True, bounce_time=0.03,
                                hold_time=UZUN_BASIS_S)
            self.cihaz.when_held = self._tutuldu
            self.cihaz.when_released = self._birakildi
            print(f"🔘 Düğme GPIO{self.pin} üzerinde hazır")
        except Exception as e:
            print(f"⚠️  Düğme açılamadı (GPIO{self.pin}): {type(e).__name__}: {e}. Klavye kullanılıyor.")
            self.cihaz = None
        return self

    # ---------- fiziksel düğme ----------
    def _tutuldu(self):
        with self._kilit:
            self._uzun = True
            self._bekleyen_kisa_iptal()
        self._gonder(UZUN_BASLADI)

    def _birakildi(self):
        with self._kilit:
            if self._uzun:
                self._uzun = False
                uzun_bitti = True
            else:
                uzun_bitti = False
                if self._bekleyen_kisa is not None:
                    # İkinci kısa basış süre dolmadan geldi: çift basış
                    self._bekleyen_kisa_iptal()
                    cift = True
                else:
                    cift = False
                    self._bekleyen_kisa = threading.Timer(CIFT_BASIS_S, self._kisa_zamani_doldu)
                    self._bekleyen_kisa.daemon = True
                    self._bekleyen_kisa.start()
        if uzun_bitti:
            self._gonder(UZUN_BITTI)
        elif cift:
            self._gonder(CIFT)

    def _kisa_zamani_doldu(self):
        with self._kilit:
            self._bekleyen_kisa = None
        self._gonder(KISA)

    def _bekleyen_kisa_iptal(self):
        if self._bekleyen_kisa is not None:
            self._bekleyen_kisa.cancel()
            self._bekleyen_kisa = None

    # ---------- klavye ----------
    def tus(self, tus):
        """Klavyeden gelen tek tuşu düğme olayına çevirir."""
        if tus in ("d", "D", "\n", "\r"):
            self._gonder(KISA)
        elif tus == " ":
            self._klavye_uzun = not self._klavye_uzun
            self._gonder(UZUN_BASLADI if self._klavye_uzun else UZUN_BITTI)
        elif tus in ("r", "R"):
            self._gonder(CIFT)
        elif tus in ("q", "Q"):
            self._gonder(CIKIS)

    def _gonder(self, olay):
        try:
            self.olay_geldi(olay)
        except Exception as e:
            print(f"⚠️  Düğme olayı işlenemedi ({olay}): {type(e).__name__}: {e}")

    def kapat(self):
        if self.cihaz is not None:
            try:
                self.cihaz.close()
            except Exception:
                pass
