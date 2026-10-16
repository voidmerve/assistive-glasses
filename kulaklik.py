"""
Asistif Gözlük - Bluetooth Kulaklık Bekçisi
===========================================

Kulaklık koparsa (testlerde: kulaklık ses akışını kapatma isteğine cevap vermedi,
bağlantı düştü) 5 sn'de bir yeniden bağlanmayı dener. Bağlanınca varsayılan ses
çıkışını ve mikrofonu kulaklığa alır ve haber verir.

Ayarlar (.env):
  KULAKLIK_MAC=20:23:03:25:87:1C   # boşsa bekçi çalışmaz

Not: Pi 4'te Wi-Fi ve Bluetooth aynı çipi ve anteni paylaşır. Kopmalar sık
sürerse en sağlam çözüm ucuz bir USB Bluetooth adaptörüdür.
"""

import os
import subprocess
import threading
import time

KULAKLIK_MAC = (os.getenv("KULAKLIK_MAC") or "").strip()


def _calistir(*komut, zaman_asimi=15):
    try:
        return subprocess.run(komut, capture_output=True, text=True, timeout=zaman_asimi).stdout
    except Exception:
        return ""


class KulaklikBekcisi(threading.Thread):
    """
    degisti: f(bagli: bool) → bağlantı durumu değişince (bağlanınca ses yönü ayarlandıktan sonra)
    """

    def __init__(self, mac=KULAKLIK_MAC, degisti=None, aralik=5.0):
        super().__init__(daemon=True, name="kulaklik")
        self.mac = mac
        self.degisti = degisti
        self.aralik = aralik
        self.bagli = None
        self.kopma_sayisi = 0
        self._son_deneme = 0.0
        self.durdur_olayi = threading.Event()

    @property
    def etkin(self):
        return bool(self.mac)

    def bagli_mi(self):
        return "Connected: yes" in _calistir("bluetoothctl", "info", self.mac, zaman_asimi=5)

    def ses_yonunu_ayarla(self):
        """Varsayılan çıkışı ve mikrofonu kulaklığa al (kopup gelince başka cihaza kalmış olabilir)."""
        parca = self.mac.replace(":", "_")
        for tur, komut in (("sinks", "set-default-sink"), ("sources", "set-default-source")):
            for satir in _calistir("pactl", "list", "short", tur, zaman_asimi=5).splitlines():
                alanlar = satir.split()
                if len(alanlar) > 1 and (parca in alanlar[1] or self.mac in alanlar[1]) and ".monitor" not in alanlar[1]:
                    _calistir("pactl", komut, alanlar[1], zaman_asimi=5)
                    break

    def run(self):
        if not self.etkin:
            return
        while not self.durdur_olayi.is_set():
            try:
                bagli = self.bagli_mi()
                if bagli != self.bagli:
                    eski, self.bagli = self.bagli, bagli
                    if bagli:
                        time.sleep(2)  # ses aygıtları PipeWire'da belirsin
                        self.ses_yonunu_ayarla()
                        print("🎧 Kulaklık bağlı")
                    elif eski is None:
                        print(f"🎧 Kulaklık ({self.mac}) bağlı değil, bağlanmaya çalışılıyor...")
                    else:
                        self.kopma_sayisi += 1
                        print(f"🎧 Kulaklık koptu ({self.kopma_sayisi}. kez), yeniden bağlanılıyor...")
                    if self.degisti and eski is not None:
                        self.degisti(bagli)
                if not bagli and time.time() - self._son_deneme > 8:
                    self._son_deneme = time.time()
                    _calistir("bluetoothctl", "connect", self.mac, zaman_asimi=15)
            except Exception as e:
                print(f"⚠️  Kulaklık bekçisi: {type(e).__name__}: {e}")
            self.durdur_olayi.wait(self.aralik)

    def durdur(self):
        self.durdur_olayi.set()
