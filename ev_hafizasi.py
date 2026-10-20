"""
Asistif Gözlük - Değişimi Fark Eden Ev Hafızası + Çıkış Kontrolü
================================================================

Görme engelli biri evinde ezberle yürür; asıl tehlike bilinmeyen şeyler değil,
YERİ DEĞİŞEN şeylerdir (koridora bırakılmış sandalye, açık kalmış dolap kapağı).

Ev gözcüsü (Gemini Live) kullanıcı dolaşırken her karede bölgeyi ve gördüklerini
METİN olarak bildirir (bolge_gozlemi). GÖRÜNTÜ SAKLANMAZ. Bu modül:

  1. Her bölgenin "normal hâlini" kendiliğinden öğrenir: bir nesne o bölgede en az
     NORMAL_EN_AZ farklı gözlemde (ve en az iki ayrı ziyarette) görülmüşse normaldir.
  2. Öğrenme bitmiş bir bölgede (ÖĞRENME_EN_AZ gözlem, en az iki ziyaret) normalde olmayan bir nesne
     YÜRÜME YOLUNDA ya da YERDE iki ardışık gözlemde görülürse söylenir:
     "Koridorda, her zamankinden farklı olarak sandalye var."
     Yolda olmayan değişiklikler söylenmez, sorulunca anlatılır.
  3. Cihaz durumlarını (ocak, musluk, pencere, kapı, ışık) en son hâliyle saklar:
     "çıkıyorum" denince eşya hafızasıyla birlikte özetlenir.

Dosya: ev_hafizasi.json (git'e gönderilmez)
"""

import difflib
import json
import os
import re
import threading
import time

DOSYA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ev_hafizasi.json")

OGRENME_EN_AZ = 6        # bölge bu kadar gözlemden önce "değişti" demez
NORMAL_EN_AZ = 3         # nesne bu kadar gözlemde görüldüyse o bölgenin normalidir
ZIYARET_ARASI_S = 600    # 10 dk'dan uzun aradan sonraki gözlem yeni ziyaret sayılır
TEKRAR_S = 1800          # aynı değişiklik yarım saat içinde tekrar söylenmez
GOZLEM_SINIRI = 40       # bölge başına saklanan son gözlem sayısı
BENZERLIK = 0.75

# Bildirilmeye değer cihaz durumları (çıkış kontrolü)
CIHAZLAR = ["ocak", "fırın", "musluk", "pencere", "kapı", "ışık", "ütü", "klima", "televizyon"]
RISKLI_DURUM = {"ocak": "açık", "fırın": "açık", "musluk": "açık", "ütü": "açık", "pencere": "açık"}


def _normal(metin):
    m = (metin or "").lower().strip().translate(str.maketrans("çğıöşüâî", "cgiosuai"))
    m = re.sub(r"[^a-z0-9 ]+", " ", m)
    m = re.sub(r"\b(bir|iki|uc|kucuk|buyuk|eski|yeni)\b", " ", m)
    return re.sub(r"\s+", " ", m).strip()


def _ayni(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= BENZERLIK


def bulunma_hali(ad):
    """'mutfak' → 'mutfakta', 'koridor' → 'koridorda', 'yatak odası' → 'yatak odasında'."""
    ad = (ad or "").strip()
    unluler = [h for h in ad.lower() if h in "aıoueiöü"]
    ek = "a" if not unluler or unluler[-1] in "aıou" else "e"
    if re.search(r"(sı|si|su|sü)$", ad.lower()):
        return ad + "n" + "d" + ek  # tamlama: odası → odasında
    return ad + ("t" if ad[-1:].lower() in "çfhkpsşt" else "d") + ek


def gecen_sure(saniye):
    if saniye < 60:
        return "az önce"
    dk = int(saniye // 60)
    if dk < 60:
        return f"{dk} dakika önce"
    sa = dk // 60
    return f"{sa} saat önce" if sa < 24 else f"{sa // 24} gün önce"


class EvHafizasi:
    """
    gozlem(bolge, nesneler, durumlar) → söylenecek cümle ya da None
      nesneler: [{"ad": "sandalye", "yolda": True}]  yolda: yürüme yolunda ya da yerde
      durumlar: [{"cihaz": "ocak", "durum": "açık"}]
    """

    def __init__(self, dosya=DOSYA, saat=time.time):
        self.dosya = dosya
        self.saat = saat
        self._kilit = threading.Lock()
        self.veri = self._yukle()
        self._aday = {}           # (bölge, nesne) → ilk görülme (iki ardışık gözlem şartı için)
        self._soylenen = {}       # (bölge, nesne) → son söylenme

    def _yukle(self):
        try:
            with open(self.dosya) as f:
                v = json.load(f)
            v.setdefault("bolgeler", {})
            v.setdefault("durumlar", {})
            return v
        except Exception:
            return {"bolgeler": {}, "durumlar": {}}

    def _yaz(self):
        try:
            gecici = self.dosya + ".tmp"
            with open(gecici, "w") as f:
                json.dump(self.veri, f, ensure_ascii=False, indent=1)
            os.replace(gecici, self.dosya)
        except Exception as e:
            print(f"⚠️  Ev hafızası kaydedilemedi: {e}")

    def _bolge_adi(self, bolge):
        """Modelin bölge adını mevcut bir bölgeyle eşle ('salon' / 'oturma odası' gibi küçük farklar)."""
        n = _normal(bolge)
        for ad in self.veri["bolgeler"]:
            if _ayni(_normal(ad), n):
                return ad
        return (bolge or "").strip().lower()

    @staticmethod
    def _sayim(gozlemler):
        """{nesne: (görüldüğü gözlem sayısı, ziyaret sayısı)}"""
        sayim, ziyaretler, son_t, ziyaret = {}, {}, None, 0
        for g in gozlemler:
            if son_t is None or g["t"] - son_t > ZIYARET_ARASI_S:
                ziyaret += 1
            son_t = g["t"]
            for n in set(g["n"]):
                eslesen = next((k for k in sayim if _ayni(k, n)), n)
                sayim[eslesen] = sayim.get(eslesen, 0) + 1
                ziyaretler.setdefault(eslesen, set()).add(ziyaret)
        return {k: (v, len(ziyaretler[k])) for k, v in sayim.items()}

    @staticmethod
    def _ogrenildi(b):
        """Yeterli gözlem ve en az iki ayrı ziyaret: tek oturuşta görüleni "normal" sanmasın."""
        g = b["gozlemler"]
        ziyaret = 1 + sum(1 for x, y in zip(g, g[1:]) if y["t"] - x["t"] > ZIYARET_ARASI_S)
        return len(g) >= OGRENME_EN_AZ and ziyaret >= 2

    def normaller(self, bolge):
        b = self.veri["bolgeler"].get(self._bolge_adi(bolge))
        if not b:
            return []
        return [k for k, (adet, ziyaret) in self._sayim(b["gozlemler"]).items()
                if adet >= NORMAL_EN_AZ and ziyaret >= 2]

    def gozlem(self, bolge, nesneler, durumlar=()):
        simdi = self.saat()
        bolge = self._bolge_adi(bolge)
        if not bolge:
            return None
        nesneler = [n for n in (nesneler or []) if isinstance(n, dict) and n.get("ad")]
        with self._kilit:
            for d in durumlar or []:
                cihaz, durum = _normal(d.get("cihaz", "")), (d.get("durum") or "").strip().lower()
                cihaz = next((c for c in CIHAZLAR if _normal(c) in cihaz), None)
                if cihaz and durum:
                    self.veri["durumlar"][cihaz] = {"durum": durum, "bolge": bolge, "zaman": simdi}
            b = self.veri["bolgeler"].setdefault(bolge, {"gozlemler": []})
            normal = None
            if self._ogrenildi(b):
                normal = [k for k, (adet, ziyaret) in self._sayim(b["gozlemler"]).items()
                          if adet >= NORMAL_EN_AZ and ziyaret >= 2]
            b["gozlemler"].append({"t": simdi, "n": [_normal(n["ad"]) for n in nesneler]})
            b["gozlemler"] = b["gozlemler"][-GOZLEM_SINIRI:]
            self._yaz()
        if normal is None:
            return None  # bu bölge hâlâ öğreniliyor
        # Yolda/yerde olup normalde olmayan nesneler: iki ardışık gözlemde görülürse söyle
        yeni_adaylar, soylenecek = {}, []
        for n in nesneler:
            ad = _normal(n["ad"])
            if not n.get("yolda") or any(_ayni(ad, k) for k in normal):
                continue
            anahtar = (bolge, ad)
            yeni_adaylar[anahtar] = self._aday.get(anahtar, simdi)
            if anahtar in self._aday and simdi - self._soylenen.get(anahtar, -1e9) > TEKRAR_S:
                self._soylenen[anahtar] = simdi
                soylenecek.append(n["ad"].strip().lower())
        self._aday = {k: v for k, v in self._aday.items() if k[0] != bolge}
        self._aday.update(yeni_adaylar)
        if not soylenecek:
            return None
        return f"{bulunma_hali(bolge).capitalize()}, her zamankinden farklı olarak {' ve '.join(soylenecek)} var."

    def degisiklikler(self, bolge=None):
        """Sorulunca: bölge(ler)in son gözlemi normal hâlinden nasıl farklı."""
        cevap = []
        for ad, b in self.veri["bolgeler"].items():
            if bolge and ad != self._bolge_adi(bolge):
                continue
            if not self._ogrenildi(b):
                cevap.append(f"{ad}: henüz öğreniliyor ({len(b['gozlemler'])} gözlem)")
                continue
            normal = self.normaller(ad)
            son = b["gozlemler"][-1]
            fazla = [n for n in set(son["n"]) if not any(_ayni(n, k) for k in normal)]
            eksik = [k for k in normal if not any(_ayni(k, n) for n in son["n"])]
            parca = []
            if fazla:
                parca.append("fazladan: " + ", ".join(fazla))
            if eksik:
                parca.append("yerinde görünmeyen: " + ", ".join(eksik))
            cevap.append(f"{ad} ({gecen_sure(self.saat() - son['t'])} bakıldı): " +
                         ("; ".join(parca) if parca else "her zamanki gibi"))
        return cevap or ["Henüz hiçbir bölgeyi öğrenmedim."]

    def durum_ozeti(self):
        simdi = self.saat()
        return [{"cihaz": c, "durum": d["durum"], "nerede": d["bolge"], "ne_zaman": gecen_sure(simdi - d["zaman"]),
                 "dikkat": RISKLI_DURUM.get(c) == d["durum"]}
                for c, d in self.veri["durumlar"].items()]

    def unut(self, bolge=None):
        with self._kilit:
            if bolge:
                self.veri["bolgeler"].pop(self._bolge_adi(bolge), None)
            else:
                self.veri = {"bolgeler": {}, "durumlar": {}}
            self._yaz()
