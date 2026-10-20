"""
Asistif Gözlük - Sokak Modu Anlatıcısı (orta yol)
=================================================

Gemini Live sokak modunda birkaç saniyede bir kareye bakar ve gördüğü önemli
şeyleri bildir(tur, metin) fonksiyonuyla bildirir. Neyin ne zaman söyleneceğine
burada karar verilir:

  olay : güvenlik ya da yön bulma için önemli (aşağı basamak, yaya geçidi,
         trafik ışığı rengi, gelen otobüsün numarası, çukur)
         → hemen söylenir; aynı olay 45 sn içinde tekrar edilmez
  engel: yoldaki ya da yakındaki engeller (park etmiş bisiklet, direk, insan grubu)
         → yerlerle aynı sıklık kuralı; aynı engel 2 dk içinde tekrar edilmez
  sosyal: biri el uzatıyor, el sallıyor, konuşulan kişi gitti → hemen söylenir;
         aynı ipucu 20 sn içinde tekrar edilmez
  yer  : yön bulmaya yarayan yerler (eczane, market, durak, tabela)
         → en fazla ARALIK saniyede bir; aynı yer 10 dk içinde tekrar edilmez

Konuşma sıklığı sesle değişir: "daha çok anlat" → 10 sn, "daha az anlat" → 60 sn,
varsayılan 30 sn.
"""

import difflib
import re
import time

SIKLIKLAR = {"az": 60.0, "orta": 30.0, "cok": 10.0}
OLAY_TEKRAR_S = 45.0
ENGEL_TEKRAR_S = 120.0
SOSYAL_TEKRAR_S = 20.0
YER_TEKRAR_S = 600.0
BENZERLIK = 0.6


def _normal(metin):
    m = metin.lower().translate(str.maketrans("çğıöşü", "cgiosu"))
    m = re.sub(r"\d+([.,]\d+)?\s*metre", "", m)        # mesafe değişse de aynı şey sayılsın
    m = re.sub(r"saat\s*\d+\s*(yonunde)?", "", m)        # yön değişse de aynı şey sayılsın
    return re.sub(r"[^a-z0-9 ]+", " ", m).strip()


class Anlatici:
    def __init__(self, siklik="orta", saat=time.monotonic):
        self.siklik = siklik if siklik in SIKLIKLAR else "orta"
        self.saat = saat
        self._son_yer_zamani = -1e9
        self._gecmis = []  # (zaman, tur, normal metin)

    @property
    def aralik(self):
        return SIKLIKLAR[self.siklik]

    def siklik_degistir(self, yon):
        """yon: 'daha_cok' | 'daha_az'. Yeni sıklığı döner."""
        sira = ["az", "orta", "cok"]
        i = sira.index(self.siklik) + (1 if yon == "daha_cok" else -1)
        self.siklik = sira[max(0, min(2, i))]
        return self.siklik

    def _tekrar_mi(self, tur, normal, simdi):
        sure = {"olay": OLAY_TEKRAR_S, "engel": ENGEL_TEKRAR_S, "sosyal": SOSYAL_TEKRAR_S}.get(tur, YER_TEKRAR_S)
        for zaman, t, eski in self._gecmis:
            if simdi - zaman < sure and difflib.SequenceMatcher(None, eski, normal).ratio() >= BENZERLIK:
                return True
        return False

    def karar(self, tur, metin):
        """Söylenecekse True. Söylendiyse geçmişe yazar."""
        metin = (metin or "").strip()
        if not metin:
            return False
        tur = tur if tur in ("olay", "engel", "sosyal") else "yer"
        simdi = self.saat()
        self._gecmis = [g for g in self._gecmis if simdi - g[0] < YER_TEKRAR_S]
        normal = _normal(metin)
        if self._tekrar_mi(tur, normal, simdi):
            return False
        # Engel ve yer bildirimleri aynı sıklık sınırını paylaşır; olay ve sosyal ipuçları hiç beklemez
        hemen = tur in ("olay", "sosyal")
        if not hemen and simdi - self._son_yer_zamani < self.aralik:
            return False
        if not hemen:
            self._son_yer_zamani = simdi
        self._gecmis.append((simdi, tur, normal))
        return True
