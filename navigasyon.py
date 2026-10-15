"""
Asistif Gözlük - Navigasyon
===========================

Konum: telefonun GPS'i. Telefon, panelin /telefon sayfasını açar ve konumu
saniyede bir Pi'ye gönderir (panel.py). Uygulama kurmak gerekmez.

Rota ve adres:
  - GOOGLE_MAPS_API_KEY geçerliyse Google Directions + Geocoding (Türkçe)
  - Değilse OpenStreetMap: Nominatim (adres) + routing.openstreetmap.de (yaya rotası).
    Ücretsiz, anahtar gerekmez; kullanım kuralı gereği saniyede en fazla 1 istek.

Yönlendirme:
  - Dönüşe ~40 m kala: "40 metre sonra sağa dön"
  - Dönüş noktasında (~12 m): "Şimdi sağa dön"
  - Rotadan 35 m'den fazla, art arda 8 sn sapınca: rota yeniden hesaplanır
  - Hedefe 15 m kala: "Hedefe vardın"
  - GPS doğruluğu 50 m'den kötüyse karar verilmez (yanlış "rotadan çıktın" demesin)

Duyurular duyur(metin, yon) geri çağırmasıyla ana programa gider. Ana program
bunları Gemini Live'a verir; model görüntüde dönüş yerini belirleyen bir işaret
(eczane, tabela) görürse cümleye ekler.
"""

import math
import re
import threading
import time

import requests

KULLANICI_AJANI = "asistif-gozluk-tez/1.0 (Raspberry Pi)"
YAKLASMA_M = 40
SIMDI_M = 12
BIRLESTIR_M = 15          # bundan yakın iki dönüş tek cümlede söylenir
EN_AZ_YAKLASMA_M = 25     # bundan kısa mesafede "... metre sonra" denmez, sadece "Şimdi"
VARIS_M = 15
SAPMA_M = 35
SAPMA_SURE_S = 8
KOTU_GPS_M = 50
SAPMA_GPS_M = 25          # sapma kararı için GPS bundan iyi olmalı (iç mekânda GPS 30-60 m oynar)
YENIDEN_HESAP_ARALIK_S = 30


# ═════════════ geometri ═════════════
def mesafe_m(a, b):
    """İki (lat, lon) arası metre (haversine)."""
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _yerel(p, merkez):
    """Küçük alanlarda (lat, lon) → metre cinsinden (x, y)."""
    k = math.cos(math.radians(merkez[0]))
    return ((p[1] - merkez[1]) * 111320 * k, (p[0] - merkez[0]) * 110540)


def cizgiye_mesafe_m(p, cizgi):
    """Noktanın çoklu çizgiye en kısa uzaklığı (m)."""
    if not cizgi:
        return math.inf
    if len(cizgi) == 1:
        return mesafe_m(p, cizgi[0])
    en_az = math.inf
    for a, b in zip(cizgi, cizgi[1:]):
        ax, ay = _yerel(a, p)
        bx, by = _yerel(b, p)
        dx, dy = bx - ax, by - ay
        uzunluk2 = dx * dx + dy * dy
        t = 0 if uzunluk2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / uzunluk2))
        en_az = min(en_az, math.hypot(ax + t * dx, ay + t * dy))
    return en_az


def mesafe_metni(m):
    if m >= 1000:
        km = round(m / 1000, 1)
        return f"{km:.1f}".replace(".", ",").replace(",0", "") + " kilometre"
    if m >= 100:
        return f"{int(round(m / 50) * 50)} metre"
    return f"{int(round(m / 10) * 10)} metre"


def sure_metni(s):
    dk = max(1, round(s / 60))
    return f"{dk} dakika" if dk < 60 else f"{dk // 60} saat {dk % 60} dakika"


# ═════════════ rota yapısı ═════════════
class Adim:
    def __init__(self, metin, konum, yon=None):
        self.metin = metin      # "sağa dön", "Çamlık Caddesi'ne sola dön"
        self.konum = konum      # manevra noktası (lat, lon)
        self.yon = yon          # "sol" | "sag" | None (baston titreşimi için)


def yakin_adimlari_birlestir(adimlar):
    """Birbirine çok yakın dönüşleri birleştirir: "sola dön, ardından hemen sola dön"."""
    sonuc = []
    for a in adimlar:
        if sonuc and len(sonuc) > 1 and mesafe_m(sonuc[-1].konum, a.konum) < BIRLESTIR_M:
            onceki = sonuc[-1]
            sonuc[-1] = Adim(f"{onceki.metin}, ardından hemen {a.metin}", onceki.konum, onceki.yon or a.yon)
        else:
            sonuc.append(a)
    return sonuc


class Rota:
    def __init__(self, hedef_adi, hedef, adimlar, cizgi, toplam_m, sure_s, kaynak):
        self.hedef_adi, self.hedef = hedef_adi, hedef
        adimlar = yakin_adimlari_birlestir(adimlar)
        self.adimlar, self.cizgi = adimlar, cizgi
        self.toplam_m, self.sure_s, self.kaynak = toplam_m, sure_s, kaynak
        # Rota üzerinde biriken mesafe: her köşe ve her dönüş için "rotanın kaçıncı metresi"
        self.km = [0.0]
        for a, b in zip(cizgi, cizgi[1:]):
            self.km.append(self.km[-1] + mesafe_m(a, b))
        self.adim_km, j = [], 0
        for a in adimlar:  # dönüşleri sırayla, geriye bakmadan çizgiye oturt
            en_iyi = min(range(j, len(cizgi)), key=lambda i: mesafe_m(cizgi[i], a.konum))
            self.adim_km.append(self.km[en_iyi])
            j = en_iyi

    def izdusum(self, p, bas_km, pencere_ileri=150, pencere_geri=30):
        """p noktasının rota üzerindeki konumu (m) ve rotaya uzaklığı; sadece bas_km çevresine bakar."""
        en_iyi = (math.inf, bas_km)
        for i in range(len(self.cizgi) - 1):
            if self.km[i + 1] < bas_km - pencere_geri or self.km[i] > bas_km + pencere_ileri:
                continue
            a, b = self.cizgi[i], self.cizgi[i + 1]
            ax, ay = _yerel(a, p)
            bx, by = _yerel(b, p)
            dx, dy = bx - ax, by - ay
            u2 = dx * dx + dy * dy
            t = 0 if u2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / u2))
            d = math.hypot(ax + t * dx, ay + t * dy)
            if d < en_iyi[0]:
                en_iyi = (d, self.km[i] + t * (self.km[i + 1] - self.km[i]))
        return en_iyi[1], en_iyi[0]


# ═════════════ OpenStreetMap ═════════════
_OSM_YON = {"left": ("sola dön", "sol"), "right": ("sağa dön", "sag"),
            "slight left": ("hafif sola dön", "sol"), "slight right": ("hafif sağa dön", "sag"),
            "sharp left": ("keskin sola dön", "sol"), "sharp right": ("keskin sağa dön", "sag"),
            "straight": ("düz devam et", None), "uturn": ("geri dön", None)}


class OSMSaglayici:
    ad = "OpenStreetMap"
    _son_istek = 0.0
    _kilit = threading.Lock()

    def _get(self, url, params):
        with self._kilit:  # Nominatim kuralı: saniyede en fazla 1 istek
            bekle = 1.05 - (time.time() - OSMSaglayici._son_istek)
            if bekle > 0:
                time.sleep(bekle)
            OSMSaglayici._son_istek = time.time()
        for deneme in range(3):  # kısa ağ kopmalarında (ad çözümleme hatası) tekrar dene
            try:
                r = requests.get(url, params=params, headers={"User-Agent": KULLANICI_AJANI}, timeout=12)
                r.raise_for_status()
                return r.json()
            except requests.ConnectionError:
                if deneme == 2:
                    raise
                time.sleep(1.5)

    def adres_bul(self, sorgu, yakin=None):
        p = {"q": sorgu, "format": "json", "limit": 1, "accept-language": "tr", "countrycodes": "tr"}
        if yakin:  # yakındaki sonuçları öne al (zorunlu değil)
            d = 0.25
            p["viewbox"] = f"{yakin[1] - d},{yakin[0] + d},{yakin[1] + d},{yakin[0] - d}"
        sonuc = self._get("https://nominatim.openstreetmap.org/search", p)
        if not sonuc:
            return None
        s = sonuc[0]
        ad = s.get("display_name", sorgu).split(",")[0]
        return (float(s["lat"]), float(s["lon"])), ad

    def ters_adres(self, konum):
        s = self._get("https://nominatim.openstreetmap.org/reverse",
                      {"lat": konum[0], "lon": konum[1], "format": "json", "zoom": 18,
                       "accept-language": "tr"})
        a = s.get("address", {})
        parcalar = [a.get("road"), a.get("neighbourhood") or a.get("suburb") or a.get("quarter"),
                    a.get("town") or a.get("city_district") or a.get("city")]
        parcalar = [p for p in parcalar if p]
        return ", ".join(dict.fromkeys(parcalar)) or s.get("display_name", "")

    def rota(self, baslangic, hedef, hedef_adi):
        url = (f"https://routing.openstreetmap.de/routed-foot/route/v1/foot/"
               f"{baslangic[1]},{baslangic[0]};{hedef[1]},{hedef[0]}")
        s = self._get(url, {"steps": "true", "overview": "full", "geometries": "geojson"})
        if s.get("code") != "Ok" or not s.get("routes"):
            return None
        r = s["routes"][0]
        cizgi = [(lat, lon) for lon, lat in r["geometry"]["coordinates"]]
        adimlar = []
        for st in r["legs"][0]["steps"]:
            m = st["maneuver"]
            konum = (m["location"][1], m["location"][0])
            tur, mod, sokak = m.get("type"), m.get("modifier"), st.get("name") or ""
            if tur == "depart":
                metin = f"{sokak} boyunca yürümeye başla" if sokak else "yürümeye başla"
                yon = None
            elif tur == "arrive":
                continue
            elif tur in ("roundabout", "rotary"):
                metin, yon = f"dönel kavşaktan {m.get('exit', 1)}. çıkıştan çık", None
            else:
                metin, yon = _OSM_YON.get(mod, ("düz devam et", None))
                if tur in ("continue", "new name") and mod in (None, "straight"):
                    if not sokak:
                        continue  # yol adı değişmedi, söylenecek bir şey yok
                    metin = "düz devam et"
                if sokak:
                    metin = f"{metin}, {sokak}"
            adimlar.append(Adim(metin, konum, yon))
        return Rota(hedef_adi, hedef, adimlar, cizgi, r["distance"], r["duration"], self.ad)


# ═════════════ Google ═════════════
def _polyline_coz(kod):
    noktalar, i, lat, lon = [], 0, 0, 0
    while i < len(kod):
        degerler = []
        for _ in range(2):
            sonuc = kaydir = 0
            while True:
                b = ord(kod[i]) - 63
                i += 1
                sonuc |= (b & 0x1f) << kaydir
                kaydir += 5
                if b < 0x20:
                    break
            degerler.append(~(sonuc >> 1) if sonuc & 1 else sonuc >> 1)
        lat, lon = lat + degerler[0], lon + degerler[1]
        noktalar.append((lat / 1e5, lon / 1e5))
    return noktalar


class GoogleSaglayici:
    ad = "Google"

    def __init__(self, anahtar):
        self.anahtar = anahtar
        self.reddedildi = False

    def _get(self, url, params):
        r = requests.get(url, params={**params, "key": self.anahtar, "language": "tr"}, timeout=12)
        r.raise_for_status()
        s = r.json()
        if s.get("status") == "REQUEST_DENIED":
            self.reddedildi = True
            raise PermissionError(s.get("error_message", "Google anahtarı reddedildi"))
        return s

    def adres_bul(self, sorgu, yakin=None):
        p = {"address": sorgu, "region": "tr"}
        s = self._get("https://maps.googleapis.com/maps/api/geocode/json", p)
        if s.get("status") != "OK":
            return None
        r = s["results"][0]
        loc = r["geometry"]["location"]
        return (loc["lat"], loc["lng"]), r.get("formatted_address", sorgu).split(",")[0]

    def ters_adres(self, konum):
        s = self._get("https://maps.googleapis.com/maps/api/geocode/json", {"latlng": f"{konum[0]},{konum[1]}"})
        if s.get("status") != "OK":
            return ""
        return ", ".join(s["results"][0]["formatted_address"].split(",")[:2])

    def rota(self, baslangic, hedef, hedef_adi):
        s = self._get("https://maps.googleapis.com/maps/api/directions/json",
                      {"origin": f"{baslangic[0]},{baslangic[1]}", "destination": f"{hedef[0]},{hedef[1]}",
                       "mode": "walking"})
        if s.get("status") != "OK":
            return None
        leg = s["routes"][0]["legs"][0]
        adimlar, cizgi = [], []
        for i, st in enumerate(leg["steps"]):
            metin = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", st.get("html_instructions", ""))).strip()
            man = st.get("maneuver", "")
            yon = "sol" if "left" in man else "sag" if "right" in man else None
            konum = (st["start_location"]["lat"], st["start_location"]["lng"])
            adimlar.append(Adim(metin[:1].lower() + metin[1:], konum, yon))
            cizgi += _polyline_coz(st["polyline"]["points"])
        return Rota(hedef_adi, hedef, adimlar, cizgi, leg["distance"]["value"], leg["duration"]["value"], self.ad)


# ═════════════ yönetici ═════════════
class Navigasyon:
    """
    duyur: f(metin, yon) → kullanıcıya söyler (yon: "sol" | "sag" | None)
    degisti: f(durum) → panele haber
    yedek_konum: GPS gelmeden önce kullanılacak sabit konum (test)
    """

    def __init__(self, duyur, google_anahtari=None, degisti=None, yedek_konum=None, saat=time.time):
        self.duyur = duyur
        self.degisti = degisti
        self.saat = saat
        self.google = GoogleSaglayici(google_anahtari) if google_anahtari else None
        self.osm = OSMSaglayici()
        self.yedek_konum = yedek_konum
        self.konum, self.dogruluk, self.konum_zamani = None, None, 0.0
        self.rota = None
        self.adim = 0
        self.ilerleme = 0.0  # rota üzerinde yürünen metre
        self._yaklasma_soylendi = False
        self._sapma_basi = None
        self._son_yeniden_hesap = 0.0
        self._son_baslat = (None, 0.0, "")  # (hedef, zaman, özet): aynı istek tekrar gelirse
        self._kilit = threading.RLock()

    # ---------- sağlayıcı ----------
    def _saglayici(self):
        if self.google is not None and not self.google.reddedildi:
            return self.google
        return self.osm

    def _dene(self, islem, *args):
        """Önce Google, reddedilirse OSM."""
        try:
            return getattr(self._saglayici(), islem)(*args)
        except PermissionError as e:
            print(f"⚠️  Google reddetti ({e}), OpenStreetMap kullanılıyor")
            return getattr(self.osm, islem)(*args)

    @property
    def gps_var(self):
        return self.konum is not None and self.saat() - self.konum_zamani < 15

    def guncel_konum(self):
        return self.konum if self.gps_var else self.yedek_konum

    @property
    def aktif(self):
        return self.rota is not None

    # ---------- komutlar (bloklayıcı, ağ kullanır) ----------
    def baslat(self, hedef_metni):
        """Rota hesaplar, söylenecek özeti döner."""
        h, zaman, ozet = self._son_baslat
        if self.aktif and h == hedef_metni and self.saat() - zaman < 30:
            return ozet  # yapay zekâ fonksiyonu art arda iki kez çağırdı: yeniden hesaplama
        bas = self.guncel_konum()
        if bas is None:
            return "Konumunu bilmiyorum. Telefonda konum sayfasını aç."
        try:
            bulunan = self._dene("adres_bul", hedef_metni, bas)
            if not bulunan:
                return f"{hedef_metni} bulunamadı."
            hedef, hedef_adi = bulunan
            rota = self._dene("rota", bas, hedef, hedef_adi)
        except Exception as e:
            print(f"⚠️  Navigasyon hatası: {type(e).__name__}: {e}")
            return "Rota hesaplanamadı, internet bağlantısını kontrol et."
        if rota is None:
            return "Rota bulunamadı."
        with self._kilit:
            self.rota, self.adim, self.ilerleme = rota, 1, 0.0  # 0. adım (yürümeye başla) özette söylendi
            self._yaklasma_soylendi, self._sapma_basi = False, None
        print(f"🗺️  Rota ({rota.kaynak}): {hedef_adi}, {rota.toplam_m:.0f} m, {len(rota.adimlar)} adım")
        self._bildir()
        ilk = rota.adimlar[0].metin if rota.adimlar else "yürümeye başla"
        not_ = "" if self.gps_var else " Telefon konumu gelmediği için test konumu kullanıldı."
        ozet = (f"Rota hazır: {hedef_adi}, {mesafe_metni(rota.toplam_m)}, yaklaşık "
                f"{sure_metni(rota.sure_s)} yürüyüş. İlk olarak {ilk}.{not_}")
        self._son_baslat = (hedef_metni, self.saat(), ozet)
        return ozet

    def durdur(self):
        with self._kilit:
            self.rota = None
        self._bildir()
        return "Navigasyon durduruldu."

    def neredeyim(self):
        k = self.guncel_konum()
        if k is None:
            return "Konumunu bilmiyorum. Telefonda konum sayfasını aç."
        try:
            adres = self._dene("ters_adres", k)
        except Exception as e:
            print(f"⚠️  Adres bulunamadı: {e}")
            adres = ""
        cumle = f"{adres} civarındasın." if adres else "Adresini bulamadım."
        if not self.gps_var:
            cumle += " Bu test konumu, telefon konumu gelmiyor."
        if self.aktif:
            kalan = mesafe_m(k, self.rota.hedef)
            cumle += f" {self.rota.hedef_adi} hedefine {mesafe_metni(kalan)} kaldı."
        return cumle

    # ---------- GPS ----------
    def konum_guncelle(self, lat, lon, dogruluk=None):
        with self._kilit:
            self.konum, self.dogruluk, self.konum_zamani = (lat, lon), dogruluk, self.saat()
            if self.rota is None:
                self._bildir()
                return
            if dogruluk is not None and dogruluk > KOTU_GPS_M:
                self._bildir()
                return
            self._takip_et()
        self._bildir()

    def _takip_et(self):
        p, r = self.konum, self.rota
        ilerleme, uzaklik = r.izdusum(p, self.ilerleme)
        if uzaklik <= SAPMA_M:
            self.ilerleme = max(self.ilerleme, ilerleme)  # GPS titremesi geri götürmesin
        if mesafe_m(p, r.hedef) <= VARIS_M or r.km[-1] - self.ilerleme <= VARIS_M:
            self.rota = None
            self._soyle(f"Hedefe vardın: {r.hedef_adi}.", None)
            return
        # Dönüş noktasına gelindi: "Şimdi ...". GPS sıçradıysa birden fazla geçilebilir, sonuncuyu söyle.
        gecilen = None
        while self.adim < len(r.adimlar) and r.adim_km[self.adim] - self.ilerleme <= SIMDI_M:
            gecilen = r.adimlar[self.adim]
            self.adim += 1
            self._yaklasma_soylendi = False
        if gecilen is not None:
            self._soyle(f"Şimdi {gecilen.metin}.", gecilen.yon)
        elif self.adim < len(r.adimlar) and not self._yaklasma_soylendi:
            kalan = r.adim_km[self.adim] - self.ilerleme
            if EN_AZ_YAKLASMA_M <= kalan <= YAKLASMA_M:
                a = r.adimlar[self.adim]
                self._soyle(f"{mesafe_metni(kalan)} sonra {a.metin}.", a.yon)
                self._yaklasma_soylendi = True
        # Rotadan sapma: bütün çizgiye uzaklık. GPS kötüyse (iç mekân) karar verme.
        if self.dogruluk is not None and self.dogruluk > SAPMA_GPS_M:
            self._sapma_basi = None
            return
        if cizgiye_mesafe_m(p, r.cizgi) > SAPMA_M:
            if self._sapma_basi is None:
                self._sapma_basi = self.saat()
            elif (self.saat() - self._sapma_basi >= SAPMA_SURE_S
                  and self.saat() - self._son_yeniden_hesap >= YENIDEN_HESAP_ARALIK_S):
                self._sapma_basi = None
                self._son_yeniden_hesap = self.saat()
                threading.Thread(target=self._yeniden_hesapla, daemon=True).start()
        else:
            self._sapma_basi = None

    def _yeniden_hesapla(self):
        r = self.rota
        if r is None:
            return
        self._soyle("Rotadan çıktın, yeni rota hesaplıyorum.", None)
        try:
            yeni = self._dene("rota", self.konum, r.hedef, r.hedef_adi)
        except Exception as e:
            print(f"⚠️  Yeniden hesaplama hatası: {e}")
            yeni = None
        if yeni is None:
            self._soyle("Yeni rota bulunamadı.", None)
            return
        with self._kilit:
            self.rota, self.adim, self.ilerleme, self._yaklasma_soylendi = yeni, 1, 0.0, False
        ilk = yeni.adimlar[1].metin if len(yeni.adimlar) > 1 else yeni.adimlar[0].metin
        self._soyle(f"Yeni rota hazır. Sonraki adım: {ilk}.", None)
        self._bildir()

    def _soyle(self, metin, yon):
        print(f"🧭 {metin}")
        threading.Thread(target=self.duyur, args=(metin, yon), daemon=True).start()

    # ---------- panel ----------
    def durum(self):
        r = self.rota
        return {
            "konum": self.konum, "dogruluk": self.dogruluk, "gps_var": self.gps_var,
            "yedek_konum": self.yedek_konum,
            "rota": None if r is None else {
                "hedef_adi": r.hedef_adi, "hedef": r.hedef, "cizgi": r.cizgi[::max(1, len(r.cizgi) // 300)],
                "toplam_m": r.toplam_m, "kaynak": r.kaynak, "adim": self.adim,
                "sonraki": r.adimlar[self.adim].metin if self.adim < len(r.adimlar) else "hedef"},
        }

    def _bildir(self):
        if self.degisti:
            try:
                self.degisti(self.durum())
            except Exception:
                pass
