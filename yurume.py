"""
Asistif Gözlük - Yürürken Kendiliğinden Mesafe Kalibrasyonu
===========================================================

Elle kalibrasyon (kalibrasyon.py) bir kez yapılır; ışık, ortam ya da kamera açısı
değişince kayabilir. Bu modül kullanıcı normal yürürken kalibrasyonu sessizce denetler:

  1. Adım boyu: telefonun adım sayarı (telefon sayfası, DeviceMotion) ile GPS
     karşılaştırılır. Açık havada, iyi GPS'le en az ADIM_GPS_EN_AZ_M yürününce
     adım boyu = yürünen GPS mesafesi / adım sayısı.
  2. Mesafe ölçeği: kullanıcı sabit bir nesneye (sandalye, bank, saksı...) doğru
     yürürken model "nesne d1'den d2'ye yaklaştı" der; gerçekte adım × adım boyu
     kadar yaklaşılmıştır. Oran = yürünen / (d1 − d2).
     Oranların ortancası (en az ORAN_EN_AZ örnek) mesafelere çarpan olarak uygulanır,
     0,75–1,33 arasında sınırlanır (tek bir kötü ölçüm her şeyi bozmasın).

Sınırlar: adım boyu GPS'le öğrenilene kadar ADIM_BOYU_M varsayılır; kullanıcı
dönerken ya da nesne hareket ederken ölçüm alınmaz ama bazı hatalı örnekler
kaçabilir, bu yüzden ortanca ve sınırlama kullanılır.

Dosya: yurume.json (adım boyu ve oranlar, git'e gönderilmez)
"""

import json
import os
import threading
import time
from collections import deque

from gecmis import mesafe_m

DOSYA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yurume.json")
ADIM_BOYU_M = float(os.getenv("ADIM_BOYU_M") or 0.65)
ADIM_GPS_EN_AZ_M = 40.0
GPS_IYI_M = 12.0
ORAN_EN_AZ = 5
CARPAN_SINIR = (0.75, 1.33)
SABIT_NESNELER = {"chair", "couch", "bed", "dining table", "tv", "refrigerator", "potted plant", "bench",
                  "toilet", "sink", "oven", "fire hydrant", "stop sign", "parking meter", "suitcase"}


class YurumeKalibrasyonu:
    def __init__(self, dosya=DOSYA, saat=time.monotonic):
        self.dosya = dosya
        self.saat = saat
        self._kilit = threading.Lock()
        k = self._yukle()
        self.adim_boyu = float(k.get("adim_boyu", ADIM_BOYU_M))
        self.adim_boyu_kaynak = k.get("adim_boyu_kaynak", "varsayılan")
        self.oranlar = deque(k.get("oranlar", []), maxlen=25)
        self._adimlar = deque(maxlen=120)     # (zaman, toplam adım)
        self._gps_bas = None                  # (zaman, konum, adım) adım boyu ölçümünün başı
        self._izlenen = None                  # (zaman, sınıf, merkez_x_oranı, mesafe, adım)

    def _yukle(self):
        try:
            with open(self.dosya) as f:
                return json.load(f)
        except Exception:
            return {}

    def _yaz(self):
        try:
            with open(self.dosya, "w") as f:
                json.dump({"adim_boyu": round(self.adim_boyu, 3), "adim_boyu_kaynak": self.adim_boyu_kaynak,
                           "oranlar": [round(o, 3) for o in self.oranlar]}, f, indent=1)
        except Exception as e:
            print(f"⚠️  yurume.json yazılamadı: {e}")

    # ---------- girdiler ----------
    def adim_geldi(self, toplam):
        with self._kilit:
            if self._adimlar and toplam < self._adimlar[-1][1]:
                self._adimlar.clear()          # telefon sayfası yenilendi: sayaç sıfırlandı
                self._gps_bas = self._izlenen = None
            self._adimlar.append((self.saat(), float(toplam)))

    def _adim_simdi(self):
        if not self._adimlar or self.saat() - self._adimlar[-1][0] > 5:
            return None                         # adım sayarı gelmiyor
        return self._adimlar[-1][1]

    def konum_geldi(self, konum, dogruluk):
        """Adım boyunu GPS'le öğren (sokakta, düz yürürken)."""
        adim = self._adim_simdi()
        if adim is None or konum is None or dogruluk is None or dogruluk > GPS_IYI_M:
            self._gps_bas = None
            return
        simdi = self.saat()
        if self._gps_bas is None or simdi - self._gps_bas[0] > 180:
            self._gps_bas = (simdi, konum, adim)
            return
        yol, adimlar = mesafe_m(self._gps_bas[1], konum), adim - self._gps_bas[2]
        if yol >= ADIM_GPS_EN_AZ_M and adimlar >= 30:
            boy = yol / adimlar               # düz olmayan yürüyüşte kısa çıkar: alt sınır koy
            if 0.4 <= boy <= 0.95:
                with self._kilit:
                    # Yumuşak güncelleme: tek ölçüm adım boyunu birden değiştirmesin
                    self.adim_boyu = boy if self.adim_boyu_kaynak == "varsayılan" else 0.7 * self.adim_boyu + 0.3 * boy
                    self.adim_boyu_kaynak = "gps"
                    self._yaz()
                print(f"🚶 Adım boyu GPS'le güncellendi: {self.adim_boyu:.2f} m ({yol:.0f} m / {adimlar:.0f} adım)")
            self._gps_bas = (simdi, konum, adim)

    def olcum_geldi(self, nesneler, kare_genisligi=None):
        """
        mesafe.py her ölçümde çağırır. nesneler: [{"sinif", "mesafe", "kutu"}] (kalibrasyonlu,
        çarpan UYGULANMAMIŞ mesafe). Tam öndeki sabit nesneye doğru yürürken oran üretir.
        """
        adim = self._adim_simdi()
        if adim is None:
            self._izlenen = None
            return
        w = kare_genisligi or 960
        adaylar = [{**n, "mesafe": n["kal"]} for n in nesneler if n.get("kal") is not None
                   and n["sinif"] in SABIT_NESNELER and 1.0 <= n["kal"] <= 5.0
                   and abs((n["kutu"][0] + n["kutu"][2]) / 2 / w - 0.5) < 0.2]
        simdi = self.saat()
        if not adaylar:
            self._izlenen = None
            return
        n = min(adaylar, key=lambda n: n["mesafe"])
        x = (n["kutu"][0] + n["kutu"][2]) / 2 / w
        iz = self._izlenen
        ayni = iz is not None and iz[1] == n["sinif"] and abs(iz[2] - x) < 0.12 and simdi - iz[0] < 10
        if not ayni:
            self._izlenen = (simdi, n["sinif"], x, n["mesafe"], adim)
            return
        adimlar = adim - iz[4]
        yaklasma = iz[3] - n["mesafe"]
        if adimlar < 3:
            return                             # yeterince yürünmedi, ilk ölçümü koru
        if yaklasma > 0.3:
            oran = adimlar * self.adim_boyu / yaklasma
            if 0.5 <= oran <= 2.0:
                with self._kilit:
                    self.oranlar.append(oran)
                    self._yaz()
                print(f"🚶 Yürüyüş kalibrasyonu: {n['sinif']} {iz[3]:.1f}→{n['mesafe']:.1f} m, "
                      f"{adimlar:.0f} adım × {self.adim_boyu:.2f} → oran {oran:.2f} "
                      f"(çarpan {self.carpan:.2f}, {len(self.oranlar)} örnek)")
        self._izlenen = (simdi, n["sinif"], x, n["mesafe"], adim)

    # ---------- çıktı ----------
    @property
    def carpan(self):
        if len(self.oranlar) < ORAN_EN_AZ:
            return 1.0
        s = sorted(self.oranlar)
        orta = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
        return max(CARPAN_SINIR[0], min(CARPAN_SINIR[1], orta))

    def durum(self):
        return {"adim_boyu_m": round(self.adim_boyu, 2), "adim_boyu_kaynak": self.adim_boyu_kaynak,
                "ornek": len(self.oranlar), "carpan": round(self.carpan, 3),
                "adim_sayar": "geliyor" if self._adim_simdi() is not None else "yok"}
