"""
Asistif Gözlük - "Az önce ne gördük?" Geçmişi + GPS Yer Notları
==============================================================

Geçmiş: gözlüğün söylediği ve gözcünün gördüğü her şey (söylenmeyenler de:
sıklık sınırı yüzünden susulan eczane, dükkân) zaman ve konumla birlikte
SADECE BELLEKTE tutulur, diske yazılmaz, GECMIS_S sonra silinir.
  "Az önce geçtiğimiz eczane neredeydi?" → "6 dakika önce, yaklaşık 300 metre geride."

Yer notları: kullanıcı sokakta "buraya not al: durağın önünde üç basamak var" der,
not telefonun GPS konumuyla yer_notlari.json'a yazılır. O noktaya YAKIN_M kadar
yaklaşınca kendiliğinden söylenir: "Burada not var: durağın önünde üç basamak var."
"""

import json
import math
import os
import threading
import time
from collections import deque

GECMIS_S = 2 * 3600
YER_NOTU_DOSYASI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yer_notlari.json")
YAKIN_M = 20.0            # nota bu kadar yaklaşınca söylenir
UZAKLASMA_M = 60.0        # aynı not, kullanıcı bu kadar uzaklaşıp geri gelince tekrar söylenir
GPS_EN_KOTU_M = 35.0      # GPS doğruluğu bundan kötüyse yer notu söylenmez/kaydedilmez


def mesafe_m(a, b):
    """İki (enlem, boylam) arası metre (haversine)."""
    r = 6371000.0
    f1, f2 = math.radians(a[0]), math.radians(b[0])
    df, dl = f2 - f1, math.radians(b[1] - a[1])
    h = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _sure(saniye):
    if saniye < 60:
        return "az önce"
    dk = int(saniye // 60)
    return f"{dk} dakika önce" if dk < 60 else f"{dk // 60} saat {dk % 60} dakika önce"


class Gecmis:
    def __init__(self, saat=time.time):
        self.saat = saat
        self._kayitlar = deque()
        self._kilit = threading.Lock()

    def ekle(self, tur, metin, konum=None):
        metin = (metin or "").strip()
        if not metin:
            return
        simdi = self.saat()
        with self._kilit:
            # Aynı cümle art arda gelirse (ör. panel + konuşma) bir kez yaz
            if self._kayitlar and self._kayitlar[-1][2] == metin and simdi - self._kayitlar[-1][0] < 5:
                return
            self._kayitlar.append((simdi, tur, metin, konum))
            while self._kayitlar and simdi - self._kayitlar[0][0] > GECMIS_S:
                self._kayitlar.popleft()

    def sorgula(self, dakika=30, simdiki_konum=None, en_fazla=40):
        """Modelin okuyacağı liste: en yeni önce, ne zaman, (varsa) ne kadar geride."""
        simdi = self.saat()
        with self._kilit:
            secilen = [k for k in self._kayitlar if simdi - k[0] <= dakika * 60]
        satirlar = []
        for t, tur, metin, konum in reversed(secilen[-en_fazla:]):
            ek = ""
            if konum and simdiki_konum:
                m = mesafe_m(konum, simdiki_konum)
                ek = f", buraya yaklaşık {int(round(m, -1))} metre" if m >= 20 else ", hemen burada"
            satirlar.append(f"{_sure(simdi - t)} ({tur}{ek}): {metin}")
        return satirlar or [f"Son {dakika:g} dakikada kayıtlı bir şey yok."]


class YerNotlari:
    def __init__(self, dosya=YER_NOTU_DOSYASI, saat=time.time):
        self.dosya = dosya
        self.saat = saat
        self._kilit = threading.Lock()
        self.notlar = self._yukle()
        self._soylenen = {}  # not kimliği → kullanıcı uzaklaşana kadar tekrar söyleme

    def _yukle(self):
        try:
            with open(self.dosya) as f:
                return json.load(f)
        except Exception:
            return []

    def _yaz(self):
        try:
            gecici = self.dosya + ".tmp"
            with open(gecici, "w") as f:
                json.dump(self.notlar, f, ensure_ascii=False, indent=1)
            os.replace(gecici, self.dosya)
        except Exception as e:
            print(f"⚠️  Yer notları kaydedilemedi: {e}")

    def ekle(self, metin, konum, dogruluk=None):
        metin = (metin or "").strip().rstrip(".")
        if not metin:
            return "Not boş, kaydedilmedi."
        if konum is None:
            return "Telefon konumu gelmiyor, not kaydedilemedi. Telefondaki konum paylaşımını açın."
        if dogruluk is not None and dogruluk > GPS_EN_KOTU_M:
            return f"Konum şu an çok belirsiz (±{dogruluk:.0f} m), biraz açık alanda tekrar deneyin."
        with self._kilit:
            kimlik = int(self.saat() * 1000)
            self.notlar.append({"id": kimlik, "metin": metin, "konum": list(konum), "zaman": self.saat()})
            self._soylenen[kimlik] = True  # kaydedilen yerde hemen geri okunmasın
            self._yaz()
        return f"Not kaydedildi: {metin}."

    def yakindakiler(self, konum, yaricap=YAKIN_M):
        return sorted(((mesafe_m(konum, n["konum"]), n) for n in self.notlar
                       if mesafe_m(konum, n["konum"]) <= yaricap), key=lambda x: x[0])

    def sil(self, konum=None, hepsi=False):
        with self._kilit:
            if hepsi:
                adet, self.notlar = len(self.notlar), []
            elif konum is not None:
                yakin = {n["id"] for _, n in self.yakindakiler(konum, yaricap=YAKIN_M * 1.5)}
                adet = len(yakin)
                self.notlar = [n for n in self.notlar if n["id"] not in yakin]
            elif self.notlar:
                adet, self.notlar = 1, self.notlar[:-1]
            else:
                adet = 0
            self._yaz()
        return f"{adet} not silindi."

    def kontrol(self, konum, dogruluk=None):
        """Konum güncellenince çağrılır: yeni yaklaşılan notların cümlesi ya da None."""
        if konum is None or (dogruluk is not None and dogruluk > GPS_EN_KOTU_M):
            return None
        soylenecek = []
        with self._kilit:
            for n in self.notlar:
                m = mesafe_m(konum, n["konum"])
                if m > UZAKLASMA_M:
                    self._soylenen.pop(n["id"], None)
                elif m <= YAKIN_M and not self._soylenen.get(n["id"]):
                    self._soylenen[n["id"]] = True
                    soylenecek.append(n["metin"])
        if not soylenecek:
            return None
        return "Burada not var: " + ". ".join(soylenecek) + "."
