"""
Asistif Gözlük - Güvenlik Katmanı
=================================

İnternetten ve yapay zekâdan tamamen bağımsız çalışan engel uyarı sistemi.

Tasarım ilkesi: varsayılan durum sessizlik. Gözlük bastonun göremediği baş ve
göğüs hizasındaki engeller için var. Mesafeye değil, çarpışmaya kalan süreye
bakar: duvarın yanında durmak uyarı vermez, duvara doğru yürümek verir.

Parçalar:
  - MesafeSensoru arayüzü: GercekSensor (VL53L0X) ve SimulasyonSensoru
  - SensorThread: sensörü ayrı bir thread'de ~15 Hz okur, hata olsa da çökmez
  - CarpismaMantigi: yaklaşma hızı ve çarpışmaya kalan süre (TTC) hesabı
  - SesYoneticisi: acil ve normal öncelikli ses çıkışı + tık kanalı
  - Titresim: sapta titreşim motoru (TITRESIM_PIN verilirse)
  - InternetIzleyici: 5 saniyede bir bağlantı kontrolü, değişince kısa ton çalar
  - GuvenlikSistemi: hepsini birleştiren tek giriş noktası

Uyarılar:
  Çarpışmaya < TTC_UYARI_S kaldı → tık + hafif titreşim, yaklaştıkça hızlanır (park sensörü gibi)
  Çarpışmaya < TTC_ACIL_S kaldı  → bip + "Dur" + güçlü titreşim
  Duruyorsan ya da engel seninle aynı hızda gidiyorsa (kalabalık) → sessiz

Ayarlar (.env):
  SENSOR_MODU=simulasyon       # veya: gercek, kapali (sensör yok: mesafe kameradan, mesafe.py)
  TTC_UYARI_S=2.0              # çarpışmaya bu kadar saniye kala tıklamaya başla
  TTC_ACIL_S=1.0               # çarpışmaya bu kadar saniye kala "Dur"
  MIN_YAKLASMA_HIZI=0.25       # m/s. Bundan yavaş yaklaşma "duruyor" sayılır
  ACIL_TEKRAR_S=2.5            # iki "Dur" arasında en az bu kadar saniye
  SENSOR_GURULTU_M=0.025       # sensörün okuma gürültüsü (m). Yanlış uyarı çoksa artır
  TITRESIM_PIN=                # titreşim motorunun GPIO (BCM) numarası, boşsa kapalı
  SENSOR_HZ=15                 # saniyedeki okuma sayısı
  INTERNET_KONTROL_S=5         # internet kontrol aralığı
  SIM_HATA_ORANI=0.005         # simülasyonda yapay okuma hatası olasılığı (0 = kapalı)
  SENSOR_ARIZA_S=2             # bu kadar saniye art arda hata → "Mesafe sensörü çalışmıyor"

Tek başına test:
  python3 guvenlik.py                          # karışık simülasyon senaryosu + ses
  python3 guvenlik.py --senaryo duvar_yaninda  # yaklasma, duvar_yaninda, kalabalik, bas_cevirme, karisik
  python3 guvenlik.py --sessiz                 # ses çalmadan sadece ekrana yazar
  python3 guvenlik.py --sure 60                # 60 saniye sonra kendiliğinden çıkar
  python3 guvenlik.py --ariza 5                # 3. saniyede başlayıp 5 sn süren sensör arızası
  python3 guvenlik.py --sesleri-uret           # konuşma dosyalarını üretir (internet gerekir)
"""

import os
import sys
import math
import time
import wave
import socket
import random
import argparse
import threading
import subprocess
from collections import deque

import numpy as np
from dotenv import load_dotenv

PROJE_DIZINI = os.path.dirname(os.path.abspath(__file__))
SES_DIZINI = os.path.join(PROJE_DIZINI, "sesler")
load_dotenv(os.path.join(PROJE_DIZINI, ".env"))


def _env_float(ad, varsayilan):
    deger = os.getenv(ad)
    if deger is None or deger.strip() == "":
        return varsayilan
    try:
        return float(deger.replace(",", "."))
    except ValueError:
        print(f"⚠️  [GÜVENLİK] .env içindeki {ad}={deger!r} sayı değil, varsayılan {varsayilan} kullanılıyor")
        return varsayilan


SENSOR_MODU = (os.getenv("SENSOR_MODU") or "simulasyon").strip().lower()
TTC_UYARI_S = _env_float("TTC_UYARI_S", 2.0)
TTC_ACIL_S = _env_float("TTC_ACIL_S", 1.0)
MIN_YAKLASMA_HIZI = _env_float("MIN_YAKLASMA_HIZI", 0.25)
ACIL_TEKRAR_S = _env_float("ACIL_TEKRAR_S", 2.5)
TITRESIM_PIN = (os.getenv("TITRESIM_PIN") or "").strip()
SENSOR_GURULTU_M = _env_float("SENSOR_GURULTU_M", 0.025)
SENSOR_HZ = _env_float("SENSOR_HZ", 15.0)
INTERNET_KONTROL_S = _env_float("INTERNET_KONTROL_S", 5.0)
SIM_HATA_ORANI = _env_float("SIM_HATA_ORANI", 0.005)
SENSOR_ARIZA_S = _env_float("SENSOR_ARIZA_S", 2.0)
# Ses dosyaları
SES_DOSYALARI = {
    "dikkat": "Dikkat",
    "dur": "Dur",
    "internet_kesildi": "İnternet bağlantısı kesildi, sadece engel uyarısı aktif",
    "internet_geldi": "İnternet bağlantısı geri geldi",
    "sensor_calismiyor": "Mesafe sensörü çalışmıyor",
    "sensor_calisiyor": "Mesafe sensörü tekrar çalışıyor",
    # Durum sorgusu ("durum ne") için parçalar
    "durum_internet_var": "İnternet var",
    "durum_internet_yok": "İnternet yok",
    "durum_sensor_iyi": "Mesafe sensörü çalışıyor",
    "durum_sensor_yok": "Mesafe sensörü çalışmıyor",
    # Konuşma düzeyi onayları
    "duzey_kisa": "Kısa konuşma",
    "duzey_detayli": "Detaylı konuşma",
    "duzey_sessiz": "Sessiz mod. Sadece sorunca konuşacağım",
    "tekrar_yok": "Tekrarlanacak bir şey yok",
    "anlamadim": "Anlayamadım, tekrar sorar mısın",
    "internet_gerekli": "Bunun için internet gerekiyor",
    "ai_yok": "Şu an cevap alamıyorum, birazdan tekrar dener misin",
    # İnternet yokken "önümde ne var" cevabı: sadece sensör ölçümü
    "engel_yok": "Önünde iki metre içinde engel yok",
    "engel_yarim": "Tam önünde, yarım metrede engel var",
    "engel_bir": "Tam önünde, yaklaşık bir metrede engel var",
    "engel_bir_bucuk": "Tam önünde, yaklaşık bir buçuk metrede engel var",
    "engel_iki": "Tam önünde, yaklaşık iki metrede engel var",
    # Modlar ve sokak anlatıcısı
    "ev_modu": "Ev modu",
    "sokak_modu": "Sokak modu",
    "siklik_cok": "Daha sık anlatacağım",
    "siklik_az": "Daha seyrek anlatacağım",
    "tarama": "Başını yavaşça sağdan sola çevir",
    "kamera_karanlik": "Kamera görüntüsü çok karanlık, önünü göremiyorum",
    # Kameradan engel uyarısı: "Önünde sandalye var" + "sağdan geç" birleştirilerek çalınır
    "on_engel": "Önünde engel var",
    "on_insan": "Önünde insan var",
    "on_sandalye": "Önünde sandalye var",
    "on_masa": "Önünde masa var",
    "on_kanepe": "Önünde koltuk var",
    "on_yatak": "Önünde yatak var",
    "on_araba": "Önünde araba var",
    "on_bisiklet": "Önünde bisiklet var",
    "on_motosiklet": "Önünde motosiklet var",
    "on_otobus": "Önünde otobüs var",
    "on_kamyon": "Önünde kamyon var",
    "on_bank": "Önünde bank var",
    "on_kopek": "Önünde köpek var",
    "on_saksi": "Önünde saksı var",
    "on_buzdolabi": "Önünde buzdolabı var",
    "on_bavul": "Önünde bavul var",
    "on_direk": "Önünde direk var",
    "gec_sag": "sağdan geç",
    "gec_sol": "soldan geç",
    "gec_yok": "önün kapalı",
}
# İnternetsiz üretilen kısa tonlar (sözsüz durum bildirimleri)
TONLAR = ["ton_internet_yok", "ton_internet_var", "ton_onay", "ton_hazir", "ton_dinliyor"]
TTS_SESI = "tr-TR-EmelNeural"
ORNEKLEME_HIZI = 24000  # edge-tts çıktısıyla aynı


def ses_yolu(ad):
    return os.path.join(SES_DIZINI, f"{ad}.wav")


def _log(mesaj):
    print(f"[{time.strftime('%H:%M:%S')}] {mesaj}", flush=True)


# ═════════════════════════ MESAFE SENSÖRLERİ ═════════════════════════
class MesafeSensoru:
    """Ortak arayüz. oku() metre cinsinden mesafe döner, menzilde engel yoksa None."""

    ad = "soyut"

    def baslat(self):
        pass

    def oku(self):
        raise NotImplementedError

    def kapat(self):
        pass


class GercekSensor(MesafeSensoru):
    """VL53L0X lazer mesafe sensörü (I2C, adafruit_vl53l0x kütüphanesi)."""

    ad = "gercek (VL53L0X)"
    MAKS_GECERLI_MM = 2000  # VL53L0X bunun üstünde güvenilir değil, 8190/8191 = hedef yok

    def __init__(self, zamanlama_us=50000):
        self.zamanlama_us = zamanlama_us
        self.sensor = None

    def baslat(self):
        # Kütüphaneler sadece gerçek modda yüklenir, simülasyon bunlara ihtiyaç duymaz
        import board
        import busio
        import adafruit_vl53l0x

        i2c = busio.I2C(board.SCL, board.SDA)
        self.sensor = adafruit_vl53l0x.VL53L0X(i2c)
        self.sensor.io_timeout_s = 0.5  # sensör takılırsa sonsuza kadar beklemesin
        # 15 Hz için okuma başına ~66 ms var. 50 ms bütçe hız ve doğruluk dengesi.
        self.sensor.measurement_timing_budget = self.zamanlama_us
        self.sensor.start_continuous()

    def oku(self):
        if self.sensor is None:
            raise RuntimeError("sensör başlatılmadı")
        mm = self.sensor.range
        if mm <= 0 or mm >= self.MAKS_GECERLI_MM:
            return None
        return mm / 1000.0

    def kapat(self):
        if self.sensor is not None:
            try:
                self.sensor.stop_continuous()
            except Exception:
                pass
            self.sensor = None


class SimulasyonSensoru(MesafeSensoru):
    """
    Gerçekçi senaryolar, döngü halinde tekrarlar. Her parça (süre sn,
    başlangıç m, bitiş m) olarak verilir ve doğrusal ilerler. 2 m'nin
    üstü gerçek sensördeki gibi "menzil dışı" (None) döner.

    Senaryolar:
      yaklasma      : 1 m/s ile duvara yürü, dibinde dur, geri çekil → uyarı beklenir
      duvar_yaninda : duvarın 45 cm önünde dur, sonra yan dön → uyarı beklenmez
      kalabalik     : önündeki kişi aynı hızda yürüyor, ara sıra yavaşlıyor → uyarı beklenmez
      bas_cevirme   : dururken başını sağa sola çevir, mesafe sıçrar → uyarı beklenmez
      karisik       : hepsi art arda
    """

    ad = "simulasyon"

    SENARYOLAR = {
        "yaklasma": [
            (2.0, 3.0, 3.0),   # uzakta, engel yok
            (2.5, 3.0, 0.5),   # 1 m/s ile duvara yürüme
            (3.0, 0.5, 0.5),   # duvarın dibinde durma (sessiz olmalı)
            (2.0, 0.5, 1.5),   # geri çekilme
            (1.5, 3.0, 3.0),
        ],
        "duvar_yaninda": [
            (6.0, 0.45, 0.45),  # duvarın önünde duruyor
            (3.0, 0.90, 0.90),  # yan döndü, köşe
            (3.0, 0.45, 0.45),
        ],
        "kalabalik": [
            (3.0, 1.00, 1.05),  # önündeki kişi aynı hızda yürüyor
            (1.5, 1.05, 0.80),  # kişi biraz yavaşladı (0,17 m/s)
            (2.0, 0.80, 1.00),  # tekrar açıldı
            (3.0, 1.00, 0.95),
        ],
        "bas_cevirme": [
            (1.2, 3.0, 3.0),    # karşı boş
            (1.0, 0.80, 0.80),  # baş sola döndü: yan duvar
            (1.2, 3.0, 3.0),
            (0.8, 1.30, 1.30),  # baş sağa döndü: dolap
            (1.0, 3.0, 3.0),
            (0.6, 0.60, 0.60),
        ],
    }
    SENARYOLAR["karisik"] = (SENARYOLAR["yaklasma"] + SENARYOLAR["duvar_yaninda"]
                             + SENARYOLAR["kalabalik"] + SENARYOLAR["bas_cevirme"])

    def __init__(self, senaryo="karisik", hata_orani=SIM_HATA_ORANI, gurultu_m=0.02, ariza=None):
        if senaryo not in self.SENARYOLAR:
            raise ValueError(f"bilinmeyen senaryo {senaryo!r}, seçenekler: {', '.join(self.SENARYOLAR)}")
        self.senaryo_adi = senaryo
        self.senaryo = self.SENARYOLAR[senaryo]
        self.ad = f"simulasyon ({senaryo})"
        self.hata_orani = hata_orani
        self.gurultu_m = gurultu_m
        # ariza=(başlangıç sn, süre sn): bu aralıkta her okuma hata verir (arıza testi).
        # Süre, sensör yeniden başlatılsa da ilk başlatmadan itibaren sayılır.
        self.ariza = ariza
        self._ilk_baslatma = None
        self.toplam_sure = sum(p[0] for p in self.senaryo)
        self.t0 = None

    def baslat(self):
        if self._ilk_baslatma is None:
            self._ilk_baslatma = time.monotonic()
        # Senaryo saati ilk başlatmadan sayılır, yeniden başlatma senaryoyu sıfırlamaz
        self.t0 = self._ilk_baslatma

    def gercek_mesafe(self, t=None):
        if t is None:
            t = time.monotonic() - self.t0
        t = t % self.toplam_sure
        for sure, bas, bit in self.senaryo:
            if t < sure:
                return bas + (bit - bas) * (t / sure)
            t -= sure
        return self.senaryo[-1][2]

    def oku(self):
        if self.t0 is None:
            raise RuntimeError("sensör başlatılmadı")
        if self.ariza is not None:
            gecen = time.monotonic() - self._ilk_baslatma
            if self.ariza[0] <= gecen < self.ariza[0] + self.ariza[1]:
                raise OSError("simüle edilmiş sensör arızası (kablo çıktı)")
        if random.random() < self.hata_orani:
            raise OSError("simüle edilmiş I2C okuma hatası")
        d = self.gercek_mesafe() + random.gauss(0.0, self.gurultu_m)
        if d >= GercekSensor.MAKS_GECERLI_MM / 1000.0:
            return None  # gerçek sensör gibi: menzil dışı = engel yok
        return max(0.03, d)


class KapaliSensor(MesafeSensoru):
    """Sensör takılı değil ya da askıda: hiç engel görmez, arıza da bildirmez.
    Bu durumda mesafe kameranın derinlik yapay zekâsından gelir (mesafe.py)."""

    ad = "kapalı"

    def oku(self):
        return None


def sensor_olustur(mod=None, ariza=None, senaryo="karisik"):
    """ariza=(başlangıç sn, süre sn) ve senaryo sadece simülasyonda kullanılır."""
    mod = (mod or SENSOR_MODU).strip().lower()
    if mod in ("gercek", "gerçek"):
        return GercekSensor()
    if mod in ("kapali", "kapalı", "yok"):
        return KapaliSensor()
    if mod not in ("simulasyon", "simülasyon"):
        print(f"⚠️  [GÜVENLİK] Bilinmeyen SENSOR_MODU={mod!r}, simülasyon kullanılıyor")
    return SimulasyonSensoru(senaryo=senaryo, ariza=ariza)


class SensorThread(threading.Thread):
    """
    Sensörü sabit hızda okur ve her okumayı geri çağırma fonksiyonuna verir.
    Hiçbir hata bu thread'i durdurmaz. Art arda çok hata olursa sensörü
    yeniden başlatmayı dener.
    """

    YENIDEN_BASLAT_ESIGI = 15  # art arda bu kadar hata → sensörü yeniden başlat

    def __init__(self, sensor, geri_cagir, hz=SENSOR_HZ, ariza_bildir=None,
                 ariza_suresi=SENSOR_ARIZA_S):
        super().__init__(daemon=True, name="guvenlik-sensor")
        self.sensor = sensor
        self.geri_cagir = geri_cagir
        # ariza_bildir(True) arıza başlayınca, ariza_bildir(False) düzelince bir kere çağrılır
        self.ariza_bildir = ariza_bildir
        self.ariza_suresi = ariza_suresi
        self.arizali = False
        self._hata_baslangici = None  # art arda hataların başladığı an
        self.periyot = 1.0 / max(1.0, hz)
        self.durdur_olayi = threading.Event()
        self.son_mesafe = None
        self.son_okuma_zamani = None
        self.hata_sayisi = 0
        self._ardisik_hata = 0
        self._son_hata_logu = 0.0
        self._baslatildi = False
        self._medyan = deque(maxlen=3)  # tek seferlik sıçramaları yumuşatır

    def _baslatmayi_dene(self):
        try:
            self.sensor.kapat()
        except Exception:
            pass
        try:
            self.sensor.baslat()
            self._baslatildi = True
            _log(f"🛡️  Sensör hazır: {self.sensor.ad}")
        except Exception as e:
            self._baslatildi = False
            self.hata_sayisi += 1
            self._hata_yaz(f"sensör başlatılamadı: {type(e).__name__}: {e}")
            self._hata_kaydet()

    def _hata_kaydet(self):
        """Okuma veya başlatma hatası. Hatalar ariza_suresi'nden uzun sürerse arıza bildirir."""
        simdi = time.monotonic()
        if self._hata_baslangici is None:
            self._hata_baslangici = simdi
        if not self.arizali and simdi - self._hata_baslangici >= self.ariza_suresi:
            self.arizali = True
            _log(f"🚨 [SENSÖR] {simdi - self._hata_baslangici:.1f} sn'dir okuma yok — ARIZA")
            self._bildir(True)

    def _basari_kaydet(self):
        self._hata_baslangici = None
        if self.arizali:
            self.arizali = False
            _log("✅ [SENSÖR] Okumalar düzeldi")
            self._bildir(False)

    def _bildir(self, arizali):
        if self.ariza_bildir is None:
            return
        try:
            self.ariza_bildir(arizali)
        except Exception as e:
            _log(f"⚠️  [SENSÖR] arıza bildirimi hatası: {e}")

    def _hata_yaz(self, mesaj):
        # Ekranı doldurmamak için en fazla 2 sn'de bir yaz
        simdi = time.monotonic()
        if simdi - self._son_hata_logu >= 2.0:
            _log(f"⚠️  [SENSÖR] {mesaj} (toplam hata: {self.hata_sayisi})")
            self._son_hata_logu = simdi

    def run(self):
        self._baslatmayi_dene()
        sonraki = time.monotonic()
        while not self.durdur_olayi.is_set():
            try:
                if not self._baslatildi:
                    self._baslatmayi_dene()
                    if not self._baslatildi:
                        # Arıza süresini kaçırmamak için kısa aralıklarla tekrar dene
                        self.durdur_olayi.wait(min(2.0, max(0.5, self.ariza_suresi / 2)))
                        sonraki = time.monotonic()
                        continue

                try:
                    ham = self.sensor.oku()
                    self._ardisik_hata = 0
                    self._basari_kaydet()
                except Exception as e:
                    self.hata_sayisi += 1
                    self._ardisik_hata += 1
                    self._hata_yaz(f"okuma hatası: {type(e).__name__}: {e}")
                    self._hata_kaydet()
                    if self._ardisik_hata >= self.YENIDEN_BASLAT_ESIGI:
                        _log("⚠️  [SENSÖR] Art arda çok hata, sensör yeniden başlatılıyor")
                        self._ardisik_hata = 0
                        self._baslatildi = False
                    ham = "hata"

                if ham != "hata":
                    self._medyan.append(math.inf if ham is None else ham)
                    filtreli = float(np.median(self._medyan))
                    mesafe = None if math.isinf(filtreli) else filtreli
                    self.son_mesafe = mesafe
                    self.son_okuma_zamani = time.monotonic()
                    self.geri_cagir(mesafe)
            except Exception as e:
                # Geri çağırma dahil hiçbir hata thread'i öldürmesin
                self.hata_sayisi += 1
                self._hata_yaz(f"beklenmeyen hata: {type(e).__name__}: {e}")

            sonraki += self.periyot
            bekle = sonraki - time.monotonic()
            if bekle > 0:
                self.durdur_olayi.wait(bekle)
            else:
                sonraki = time.monotonic()  # geride kaldıysak yetişmeye çalışma

        try:
            self.sensor.kapat()
        except Exception:
            pass

    def durdur(self):
        self.durdur_olayi.set()


# ═════════════════════════ ÇARPIŞMA MANTIĞI ═════════════════════════
SEVIYE_YOK = 0
SEVIYE_YAKLASMA = 1
SEVIYE_ACIL = 2
SEVIYE_ADI = {SEVIYE_YOK: "yok", SEVIYE_YAKLASMA: "YAKLAŞMA", SEVIYE_ACIL: "ACİL"}

OLAY_TIK = "tik"
OLAY_DUR = "dur"


class CarpismaMantigi:
    """
    Mesafe okumalarından yaklaşma hızını ve çarpışmaya kalan süreyi (TTC) hesaplar.

    - Son PENCERE_S saniyedeki okumalara doğru uydurulur, eğimi yaklaşma hızıdır.
      Tek tek okumalar yerine eğim kullanmak sensör gürültüsünü bastırır.
    - Ardışık iki okuma arasında SICRAMA_M'den büyük fark fiziksel bir yürüme
      olamaz: baş çevrildi ya da önüne başka bir şey girdi. Pencere sıfırlanır,
      böylece baş çevirmek sahte bir "hızlı yaklaşma" üretmez.
    - Hız MIN_YAKLASMA_HIZI'nın altındaysa (duruyorsun ya da önündeki kişi
      seninle aynı hızda yürüyor) TTC sonsuzdur, uyarı yoktur.

    guncelle() her okumada çağrılır ve bu okumada yapılacak işlerin listesini döner:
      (OLAY_TIK, güç 0..1)  → tık sesi + kısa titreşim
      (OLAY_DUR, ttc)       → bip + "Dur" + uzun titreşim
    """

    PENCERE_S = 0.5      # hız hesabı için bakılan geçmiş
    MIN_ORNEK = 6        # hız hesabı için gereken en az okuma (~0,35 sn)
    SICRAMA_M = 0.30     # iki okuma arasında bundan büyük fark = yeni nesne
    GUVEN_KATSAYISI = 3.0  # hız, standart hatasının bu katından küçükse "hareket yok"
    # Sensör gürültüsü en az bu kadar varsayılır (az örnekte kendini kandırmasın).
    # Gerçek sensörün gürültüsü ölçülünce SENSOR_GURULTU_M ile ayarlanır.
    GURULTU_TABANI_M = SENSOR_GURULTU_M
    TIK_ARALIK_ACIL = 0.12
    TIK_ARALIK_YAVAS = 0.60
    DUR_SONRASI_SUSMA_S = 0.9  # "Dur" sesi bitene kadar tık çalma

    def __init__(self, ttc_uyari=TTC_UYARI_S, ttc_acil=TTC_ACIL_S,
                 min_hiz=MIN_YAKLASMA_HIZI, acil_tekrar=ACIL_TEKRAR_S, saat=time.monotonic):
        if ttc_acil >= ttc_uyari:
            raise ValueError(f"TTC_ACIL_S ({ttc_acil}) TTC_UYARI_S'den ({ttc_uyari}) küçük olmalı")
        self.ttc_uyari = ttc_uyari
        self.ttc_acil = ttc_acil
        self.min_hiz = min_hiz
        self.acil_tekrar = acil_tekrar
        self.saat = saat
        self.seviye = SEVIYE_YOK
        self.hiz = 0.0          # m/s, pozitif = yaklaşıyor
        self.ttc = math.inf
        self._pencere = deque()
        self._son_tik = -math.inf
        self._son_dur = -math.inf
        self._acil_ardisik = 0
        self._dur_soylendi = False

    def _hiz_hesapla(self):
        if len(self._pencere) < self.MIN_ORNEK:
            return 0.0
        t = np.array([p[0] for p in self._pencere])
        d = np.array([p[1] for p in self._pencere])
        if t[-1] - t[0] < 0.3:
            return 0.0
        t = t - t.mean()
        egim, kesim = np.polyfit(t, d, 1)
        # Güven kontrolü: eğim, gürültünün tek başına üretebileceğinden belirgin
        # büyük değilse hareket yok say. Durana sahte "Dur" demeyi önler.
        artik = d - (egim * t + kesim)
        gurultu = max(self.GURULTU_TABANI_M, math.sqrt(float(np.sum(artik ** 2)) / (len(d) - 2)))
        std_hata = gurultu / math.sqrt(float(np.sum(t ** 2)))
        if abs(egim) < self.GUVEN_KATSAYISI * std_hata:
            return 0.0
        return float(-egim)

    def _yeni_seviye(self):
        ttc, s = self.ttc, self.seviye
        if ttc < self.ttc_acil:
            return SEVIYE_ACIL
        if s == SEVIYE_ACIL and ttc < self.ttc_acil * 1.3:
            return SEVIYE_ACIL  # histerezis: titreşen TTC uyarıyı açıp kapatmasın
        if ttc < self.ttc_uyari:
            return SEVIYE_YAKLASMA
        if s >= SEVIYE_YAKLASMA and ttc < self.ttc_uyari * 1.25:
            return SEVIYE_YAKLASMA
        return SEVIYE_YOK

    def guncelle(self, mesafe):
        simdi = self.saat()
        if mesafe is None:
            self._pencere.clear()  # menzilde engel yok
        else:
            if self._pencere:
                # Son üç okumanın ortancasıyla karşılaştır: tek gürültülü okuma sıçramayı gizlemesin
                son3 = sorted(p[1] for p in list(self._pencere)[-3:])
                if abs(mesafe - son3[len(son3) // 2]) > self.SICRAMA_M:
                    self._pencere.clear()  # baş çevrildi ya da yeni nesne
            self._pencere.append((simdi, mesafe))
            while self._pencere and simdi - self._pencere[0][0] > self.PENCERE_S:
                self._pencere.popleft()

        self.hiz = self._hiz_hesapla() if mesafe is not None else 0.0
        self.ttc = mesafe / self.hiz if (mesafe is not None and self.hiz > self.min_hiz) else math.inf

        self.seviye = yeni = self._yeni_seviye()
        olaylar = []
        if yeni < SEVIYE_ACIL:
            self._dur_soylendi = False  # acil bitti, bir sonraki acil yeniden "Dur" der
        if yeni == SEVIYE_YOK:
            self._acil_ardisik = 0
            return olaylar

        # "Dur" için acil durum art arda iki okumada görülmeli (tek gürültülü okuma yetmez)
        self._acil_ardisik = self._acil_ardisik + 1 if self.ttc < self.ttc_acil else 0
        if (yeni == SEVIYE_ACIL and self._acil_ardisik >= 2 and not self._dur_soylendi
                and simdi - self._son_dur >= self.acil_tekrar):
            self._son_dur = simdi
            self._dur_soylendi = True
            olaylar.append((OLAY_DUR, self.ttc))
            return olaylar

        if simdi - self._son_dur < self.DUR_SONRASI_SUSMA_S:
            return olaylar
        # Park sensörü: çarpışma yaklaştıkça tıklar sıklaşır ve güçlenir
        if yeni == SEVIYE_ACIL:
            aralik, guc = self.TIK_ARALIK_ACIL, 1.0
        else:
            oran = (min(self.ttc, self.ttc_uyari) - self.ttc_acil) / (self.ttc_uyari - self.ttc_acil)
            oran = min(1.0, max(0.0, oran))  # 0 = acile yakın, 1 = uyarının başı
            aralik = self.TIK_ARALIK_ACIL + oran * (self.TIK_ARALIK_YAVAS - self.TIK_ARALIK_ACIL)
            guc = 0.9 - 0.5 * oran
        if simdi - self._son_tik >= aralik:
            self._son_tik = simdi
            olaylar.append((OLAY_TIK, guc))
        return olaylar


# ═════════════════════════ SES SİSTEMİ ═════════════════════════
class TikKanali(threading.Thread):
    """
    Varsayılan ses çıkışına sürekli açık, düşük gecikmeli bir akış.

    İki işi var:
      1. Sessizlik göndererek Bluetooth çıkışını uyanık tutar. Uyuyan çıkış,
         uyanırken sesin başını yutuyordu.
      2. Tık seslerini bu akışa karıştırır. Tıklar ayrı bir akışta olduğu için
         o an çalan konuşmayı kesmez, üstüne karışır. Her tık için yeni süreç
         açılmadığından gecikme ~50 ms + Bluetooth gecikmesidir.

    Varsayılan çıkış değişirse (başka kulaklık bağlandıysa) akışı yeniden açar.
    pacat ölürse 2 sn sonra yeniden başlatır.
    """

    ORNEKLEME = 16000
    PARCA_ORNEK = 320  # 20 ms

    def __init__(self):
        super().__init__(daemon=True, name="ses-tik")
        self.durdur_olayi = threading.Event()
        self._proc = None
        self._kilit = threading.Lock()
        self._tampon = np.zeros(0, dtype=np.float32)

    @classmethod
    def tik_sesi(cls, guc):
        """25 ms'lik yumuşak tık. Güç arttıkça hem yükselir hem tizleşir."""
        n = int(cls.ORNEKLEME * 0.025)
        t = np.arange(n) / cls.ORNEKLEME
        frekans = 1200 + 800 * guc
        zarf = np.exp(-t * 180)  # hızlı sönümlenen vuruş
        return (np.sin(2 * np.pi * frekans * t) * zarf * (0.15 + 0.45 * guc)).astype(np.float32)

    def tik(self, guc):
        ses = self.tik_sesi(guc)
        with self._kilit:
            if len(self._tampon) < len(ses):
                self._tampon = np.concatenate([self._tampon, np.zeros(len(ses) - len(self._tampon), np.float32)])
            self._tampon[:len(ses)] += ses

    @staticmethod
    def _varsayilan_cikis():
        try:
            return subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except Exception:
            return ""

    def _baslat(self):
        return subprocess.Popen(
            ["pacat", "--playback", "--raw", "--format=s16le", "--channels=1",
             f"--rate={self.ORNEKLEME}", "--latency-msec=50",
             "--client-name=guvenlik-tik", "--stream-name=tik-ve-sessizlik"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _kapat(self):
        with self._kilit:
            p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
            try:
                p.stdin.close()
            except Exception:
                pass  # süreç öldüğü için boru kırık olabilir, önemsiz

    def _sonraki_parca(self):
        with self._kilit:
            parca = self._tampon[:self.PARCA_ORNEK]
            self._tampon = self._tampon[self.PARCA_ORNEK:]
        if len(parca) < self.PARCA_ORNEK:
            parca = np.concatenate([parca, np.zeros(self.PARCA_ORNEK - len(parca), np.float32)])
        return (np.clip(parca, -1, 1) * 32767).astype(np.int16).tobytes()

    def run(self):
        cikis = None
        son_kontrol = 0.0
        while not self.durdur_olayi.is_set():
            try:
                simdi = time.monotonic()
                if simdi - son_kontrol >= 5.0:
                    son_kontrol = simdi
                    yeni = self._varsayilan_cikis()
                    if yeni and yeni != cikis:
                        if cikis is not None:
                            _log(f"🎧 Ses çıkışı değişti: {yeni}")
                        cikis = yeni
                        self._kapat()
                with self._kilit:
                    if self.durdur_olayi.is_set():
                        break
                    if self._proc is None or self._proc.poll() is not None:
                        self._proc = self._baslat()
                    p = self._proc
                # pacat tamponu dolunca yazma bloklanır, döngü gerçek zamanlı ilerler
                p.stdin.write(self._sonraki_parca())
                p.stdin.flush()
            except Exception:
                self._kapat()
                self.durdur_olayi.wait(2.0)
        self._kapat()

    def durdur(self):
        self.durdur_olayi.set()
        self._kapat()


class Titresim:
    """
    Sapa takılacak titreşim motoru. Motor bir transistör ya da sürücü kartı
    üzerinden GPIO'ya bağlanmalı, doğrudan pine bağlanmamalı.
    TITRESIM_PIN boşsa hiçbir şey yapmaz.
    """

    def __init__(self, pin=TITRESIM_PIN):
        self.cihaz = None
        self._zamanlayici = None
        self._kilit = threading.Lock()
        if not pin:
            return
        try:
            from gpiozero import PWMOutputDevice
            self.cihaz = PWMOutputDevice(int(pin), frequency=200)
            _log(f"📳 Titreşim motoru GPIO{pin} üzerinde hazır")
        except Exception as e:
            _log(f"⚠️  Titreşim motoru açılamadı (GPIO{pin}): {type(e).__name__}: {e}")

    @property
    def var(self):
        return self.cihaz is not None

    def titret(self, guc, sure_s):
        if self.cihaz is None:
            return
        with self._kilit:
            try:
                if self._zamanlayici is not None:
                    self._zamanlayici.cancel()
                self.cihaz.value = max(0.0, min(1.0, guc))
                self._zamanlayici = threading.Timer(sure_s, self._sustur)
                self._zamanlayici.daemon = True
                self._zamanlayici.start()
            except Exception as e:
                _log(f"⚠️  Titreşim hatası: {e}")

    def _sustur(self):
        try:
            self.cihaz.value = 0
        except Exception:
            pass

    def kapat(self):
        if self.cihaz is not None:
            self._sustur()
            try:
                self.cihaz.close()
            except Exception:
                pass


ONCELIK_NORMAL = "normal"
ONCELIK_ACIL = "acil"


class SesYoneticisi:
    """
    İki öncelikli ses çıkışı.

    acil_cal(dosya): beklemeden döner. O an çalan normal ses varsa paplay
        süreci sonlandırılır ve acil ses hemen çalar. Sırada birden fazla acil
        ses birikirse sadece en sonuncusu çalar (Dikkat beklerken Dur geldiyse Dur).
    normal_cal(dosya): çalma bitene kadar bekler. Acil ses çalıyor veya
        sıradaysa önce onun bitmesini bekler. Acil tarafından kesilirse False döner.
    """

    def __init__(self, sessiz=False):
        self.sessiz = sessiz
        self._kosul = threading.Condition()
        self._normal_kilit = threading.Lock()
        self._acil_bekleyen = None
        self._acil_mesgul = False
        self._proc = None
        self._proc_oncelik = None
        self._proc_dosya = None
        threading.Thread(target=self._acil_dongusu, daemon=True, name="ses-acil").start()
        self._tik_kanali = None
        if not sessiz:
            self._tik_kanali = TikKanali()
            self._tik_kanali.start()

    def _baslat(self, dosya, oncelik):
        """Kilit içinde çağrılır."""
        if self.sessiz:
            return None
        if not os.path.exists(dosya):
            _log(f"⚠️  [SES] Dosya yok: {dosya}")
            return None
        try:
            proc = subprocess.Popen(["paplay", dosya],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            _log(f"⚠️  [SES] paplay başlatılamadı: {e}")
            return None
        self._proc, self._proc_oncelik, self._proc_dosya = proc, oncelik, dosya
        return proc

    def _bitti(self, proc):
        with self._kosul:
            if self._proc is proc:
                self._proc = self._proc_oncelik = self._proc_dosya = None
            self._kosul.notify_all()

    def acil_cal(self, dosya):
        with self._kosul:
            p = self._proc
            calisiyor = p is not None and p.poll() is None
            if calisiyor and self._proc_oncelik == ONCELIK_ACIL and self._proc_dosya == dosya \
                    and self._acil_bekleyen is None:
                return  # aynı acil ses zaten çalıyor
            self._acil_bekleyen = dosya
            self._acil_mesgul = True
            if calisiyor:
                p.kesildi = True  # paplay SIGTERM'de 0 ile çıkabiliyor, kesmeyi ayrıca işaretle
                p.terminate()  # normal sesi (veya farklı acil sesi) hemen kes
            self._kosul.notify_all()

    def _acil_dongusu(self):
        while True:
            try:
                with self._kosul:
                    while self._acil_bekleyen is None:
                        self._kosul.wait()
                    # Önceki süreç kapanana kadar bekle (terminate gönderildi)
                    p = self._proc
                    if p is not None:
                        try:
                            p.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            p.kill()
                    dosya = self._acil_bekleyen
                    self._acil_bekleyen = None
                    proc = self._baslat(dosya, ONCELIK_ACIL)
                if proc is not None:
                    proc.wait()
                    if proc.returncode != 0 and not getattr(proc, "kesildi", False):
                        # Örn. kulaklık o an kopuksa: terminalde yazan uyarı kulağa gitmedi
                        _log(f"⚠️  [SES] uyarı çalınamadı ({os.path.basename(dosya)}, kod {proc.returncode})")
                    self._bitti(proc)
                with self._kosul:
                    if self._acil_bekleyen is None:
                        self._acil_mesgul = False
                        self._kosul.notify_all()
            except Exception as e:
                _log(f"⚠️  [SES] acil döngü hatası: {e}")
                time.sleep(0.1)

    def normal_cal(self, dosya):
        """Çalma tamamlandıysa True, acil ses tarafından kesildiyse False."""
        with self._normal_kilit:
            with self._kosul:
                while self._acil_mesgul:
                    self._kosul.wait(0.1)
                proc = self._baslat(dosya, ONCELIK_NORMAL)
            if proc is None:
                return False
            proc.wait()
            self._bitti(proc)
            return proc.returncode == 0 and not getattr(proc, "kesildi", False)

    def akis_ac(self, ornekleme=24000):
        """
        Akan ses için normal öncelikli oynatıcı (Gemini Live cevapları). HEMEN döner:
        acil ses çalıyorsa bitmesini oynatıcının kendi thread'i bekler. (Eskiden burada
        beklenince Live'ın asyncio döngüsü donuyor, engel uyarıları sık çalınca cevaplar
        hiç işlenemiyordu.) "Dur" ya da kısa basış bu akışı normal bir ses gibi keser.
        Sessiz moddaysa None döner.
        """
        if self.sessiz:
            return None
        return AkisOynatici(self, ornekleme)

    def normal_kes(self):
        """O an çalan normal sesi (konuşmayı) keser. Kesildiyse True."""
        with self._kosul:
            p = self._proc
            if p is not None and p.poll() is None and self._proc_oncelik == ONCELIK_NORMAL:
                p.kesildi = True
                p.terminate()
                return True
        return False

    @property
    def normal_caliyor(self):
        p = self._proc
        return p is not None and p.poll() is None and self._proc_oncelik == ONCELIK_NORMAL

    def tik(self, guc):
        """Konuşmayı kesmeden üstüne karışan kısa tık."""
        if self._tik_kanali is not None:
            self._tik_kanali.tik(guc)

    @property
    def acil_caliyor(self):
        return self._acil_mesgul

    def durdur(self):
        if self._tik_kanali is not None:
            self._tik_kanali.durdur()
        with self._kosul:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()


# ═════════════════════════ İNTERNET İZLEYİCİ ═════════════════════════
class AkisOynatici:
    """
    Parça parça gelen PCM sesi çalar. Bekleme, süreç başlatma ve yazma kendi thread'inde
    yapılır: çağıran (asyncio döngüsü) ne acil sesi ne de pacat tamponunu bekler.
    """

    def __init__(self, yonetici, ornekleme):
        self.yonetici = yonetici
        self.ornekleme = ornekleme
        self.proc = None
        self._kesildi = False
        self._kuyruk = deque()
        self._olay = threading.Event()
        self._kapaniyor = False
        threading.Thread(target=self._yazici, daemon=True, name="ses-akis").start()

    @property
    def kesildi(self):
        p = self.proc
        return self._kesildi or (p is not None and (getattr(p, "kesildi", False) or p.poll() is not None))

    def yaz(self, veri):
        if self.kesildi:
            return False
        self._kuyruk.append(veri)
        self._olay.set()
        return True

    def bitir(self):
        """Kalan sesi çalıp kapanır (beklemez)."""
        self._kapaniyor = True
        self._olay.set()

    def kes(self):
        self._kesildi = True
        p = self.proc
        if p is not None:
            p.kesildi = True
            try:
                p.terminate()
            except Exception:
                pass
        self.bitir()

    def _baslat(self):
        """Acil ses bitene kadar bekler, sonra pacat'ı normal öncelikli ses olarak kaydeder."""
        y = self.yonetici
        with y._kosul:
            while y._acil_mesgul and not self._kesildi:
                y._kosul.wait(0.1)
            if self._kesildi:
                return False
            try:
                self.proc = subprocess.Popen(
                    ["pacat", "--playback", "--raw", "--format=s16le", "--channels=1",
                     f"--rate={self.ornekleme}", "--latency-msec=100",
                     "--client-name=gozluk-canli", "--stream-name=cevap"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                _log(f"⚠️  [SES] akış açılamadı: {e}")
                self._kesildi = True
                return False
            y._proc, y._proc_oncelik, y._proc_dosya = self.proc, ONCELIK_NORMAL, "<akış>"
        return True

    def _yazici(self):
        if self._baslat():
            try:
                while True:
                    self._olay.wait()
                    self._olay.clear()
                    while self._kuyruk:
                        veri = self._kuyruk.popleft()
                        if self.kesildi:
                            self._kuyruk.clear()
                            break
                        self.proc.stdin.write(veri)
                        self.proc.stdin.flush()
                    if self._kapaniyor or self.kesildi:
                        break
            except Exception:
                pass  # süreç öldürüldüyse boru kırılır
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=30)
            except Exception:
                self.proc.kill()
            self.yonetici._bitti(self.proc)


class InternetIzleyici(threading.Thread):
    """
    Her INTERNET_KONTROL_S saniyede bir bağlantıyı kontrol eder.
    Yanlış alarm olmasın diye kopma için art arda 2 başarısız kontrol gerekir.
    Durum değişince konuşma yerine kısa bir ton çalar (alçalan = koptu,
    yükselen = geldi). Ayrıntı "durum ne" diye sorulunca söylenir.
    """

    HEDEFLER = [("1.1.1.1", 53), ("8.8.8.8", 53), ("generativelanguage.googleapis.com", 443)]

    def __init__(self, ses, aralik=INTERNET_KONTROL_S, kopma_esigi=2):
        super().__init__(daemon=True, name="guvenlik-internet")
        self.ses = ses
        self.aralik = aralik
        self.kopma_esigi = kopma_esigi
        self.bagli = None  # ilk kontrolden önce bilinmiyor
        self._basarisiz = 0
        self.durdur_olayi = threading.Event()

    @classmethod
    def kontrol(cls, zaman_asimi=2.0):
        for host, port in cls.HEDEFLER:
            try:
                with socket.create_connection((host, port), timeout=zaman_asimi):
                    return True
            except OSError:
                continue
        return False

    def _bildir(self, ad, mesaj):
        _log(mesaj)
        # Kendi thread'inde çalar, izleyiciyi bekletmez
        threading.Thread(target=self.ses.normal_cal, args=(ses_yolu(ad),),
                         daemon=True).start()

    def run(self):
        while not self.durdur_olayi.is_set():
            try:
                var = self.kontrol()
                if var:
                    self._basarisiz = 0
                    if self.bagli is False:
                        self._bildir("ton_internet_var", "🌐 İnternet bağlantısı geri geldi")
                    elif self.bagli is None:
                        _log("🌐 İnternet bağlantısı var")
                    self.bagli = True
                else:
                    self._basarisiz += 1
                    if self._basarisiz >= self.kopma_esigi and self.bagli is not False:
                        self.bagli = False
                        self._bildir("ton_internet_yok",
                                     "📵 İnternet bağlantısı kesildi, sadece engel uyarısı aktif")
            except Exception as e:
                _log(f"⚠️  [İNTERNET] kontrol hatası: {e}")
            self.durdur_olayi.wait(self.aralik)

    def durdur(self):
        self.durdur_olayi.set()


# ═════════════════════════ SES DOSYALARI ═════════════════════════
def bip_uret(yol, frekans=1000, sure=0.15, tekrar=2, ara=0.08, ses_seviyesi=0.6):
    """numpy ile kısa, iki vuruşlu bip. İnternet gerektirmez."""
    t = np.arange(int(ORNEKLEME_HIZI * sure)) / ORNEKLEME_HIZI
    ton = np.sin(2 * np.pi * frekans * t)
    # Tıklama olmasın diye 10 ms yumuşak giriş/çıkış
    rampa = int(ORNEKLEME_HIZI * 0.01)
    zarf = np.ones_like(ton)
    zarf[:rampa] = np.linspace(0, 1, rampa)
    zarf[-rampa:] = np.linspace(1, 0, rampa)
    vurus = ton * zarf * ses_seviyesi
    sessizlik = np.zeros(int(ORNEKLEME_HIZI * ara))
    parcalar = []
    for i in range(tekrar):
        parcalar.append(vurus)
        if i < tekrar - 1:
            parcalar.append(sessizlik)
    _wav_yaz(yol, np.concatenate(parcalar))


def _ton(notalar, nota_s=0.09, ara_s=0.03, seviye=0.35):
    """Kısa melodik ton dizisi (Hz listesi)."""
    parcalar = []
    for f in notalar:
        t = np.arange(int(ORNEKLEME_HIZI * nota_s)) / ORNEKLEME_HIZI
        zarf = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)  # 10 ms yumuşak giriş/çıkış
        parcalar += [np.sin(2 * np.pi * f * t) * zarf * seviye, np.zeros(int(ORNEKLEME_HIZI * ara_s))]
    return np.concatenate(parcalar)


def tonlari_uret(zorla=False):
    """Sözsüz durum tonları. numpy ile üretilir, internet gerekmez."""
    os.makedirs(SES_DIZINI, exist_ok=True)
    tanim = {
        "ton_internet_yok": [784, 523],        # alçalan: bir şey gitti
        "ton_internet_var": [523, 784],        # yükselen: bir şey geldi
        "ton_onay": [988],                     # tek kısa nota: ayar değişti
        "ton_hazir": [523, 659, 784],          # açılış
        "ton_dinliyor": [1047, 1319],          # uzun basış: dinlemeye başladım
    }
    for ad, notalar in tanim.items():
        if zorla or not os.path.exists(ses_yolu(ad)):
            _wav_yaz(ses_yolu(ad), _ton(notalar))


def _wav_yaz(yol, ornekler):
    veri = (np.clip(ornekler, -1, 1) * 32767).astype(np.int16)
    with wave.open(yol, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(ORNEKLEME_HIZI)
        wf.writeframes(veri.tobytes())


def _wav_oku(yol):
    with wave.open(yol, "rb") as wf:
        veri = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return veri.astype(np.float32) / 32767


def _sessizligi_kirp(ornekler, esik=0.01, bas_birak_s=0.02, son_birak_s=0.08):
    """TTS çıktısının başındaki ve sonundaki sessizliği atar, uyarı daha hızlı duyulsun."""
    yuksek = np.flatnonzero(np.abs(ornekler) > esik)
    if len(yuksek) == 0:
        return ornekler
    bas = max(0, yuksek[0] - int(ORNEKLEME_HIZI * bas_birak_s))
    son = min(len(ornekler), yuksek[-1] + int(ORNEKLEME_HIZI * son_birak_s))
    return ornekler[bas:son]


def sesleri_uret(zorla=False):
    """edge-tts ile konuşma dosyalarını bir kere üretir. İnternet gerekir."""
    import asyncio
    import tempfile
    import edge_tts

    os.makedirs(SES_DIZINI, exist_ok=True)
    tonlari_uret(zorla=zorla)
    bip = ses_yolu("bip")
    if zorla or not os.path.exists(bip):
        bip_uret(bip)
        print(f"  ✓ {bip}")

    for ad, metin in SES_DOSYALARI.items():
        hedef = ses_yolu(ad)
        if os.path.exists(hedef) and not zorla:
            print(f"  = {hedef} (zaten var)")
            continue
        with tempfile.TemporaryDirectory() as gecici:
            mp3 = os.path.join(gecici, "ses.mp3")
            ham = os.path.join(gecici, "ses.wav")
            asyncio.run(edge_tts.Communicate(metin, TTS_SESI).save(mp3))
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", mp3,
                            "-ac", "1", "-ar", str(ORNEKLEME_HIZI), "-sample_fmt", "s16", ham],
                           check=True)
            _wav_yaz(hedef, _sessizligi_kirp(_wav_oku(ham)))
        print(f"  ✓ {hedef}  ← \"{metin}\"")

    # Acil uyarı: bip + "Dur" tek dosyada (iki ayrı paplay gecikmesi olmasın)
    bip_dur = ses_yolu("bip_dur")
    _wav_yaz(bip_dur, np.concatenate([_wav_oku(bip), np.zeros(int(ORNEKLEME_HIZI * 0.05)),
                                      _wav_oku(ses_yolu("dur"))]))
    print(f"  ✓ {bip_dur}")


def eksik_sesler():
    adlar = list(SES_DOSYALARI) + ["bip", "bip_dur"] + TONLAR
    return [ad for ad in adlar if not os.path.exists(ses_yolu(ad))]


# ═════════════════════════ BİRLEŞİK SİSTEM ═════════════════════════
class GuvenlikSistemi:
    """
    Kullanım (ana programdan):
        guvenlik = GuvenlikSistemi(ses=ses_yoneticisi).baslat()
        guvenlik.ses.normal_cal("/tmp/tts.mp3")   # normal öncelikli konuşma
        guvenlik.durum_soyle()                    # "İnternet var. Mesafe sensörü çalışıyor"
        guvenlik.son_mesafe                       # tam öndeki ölçülen mesafe (m) ya da None
    """

    def __init__(self, sensor=None, ses=None, sessiz=False, ekran=False, internet_izle=True,
                 titresim=None):
        self.sensor = sensor or sensor_olustur()
        self.ses = ses or SesYoneticisi(sessiz=sessiz)
        self.titresim = titresim or Titresim()
        self.mantik = CarpismaMantigi()
        self.ekran = ekran
        self.sensor_thread = SensorThread(self.sensor, self._okuma_geldi,
                                          ariza_bildir=self._ariza_degisti)
        self.internet = InternetIzleyici(self.ses) if internet_izle else None
        self.sayac = {OLAY_TIK: 0, OLAY_DUR: 0}
        self._ekran_sayac = 0

    def baslat(self):
        tonlari_uret()  # internetsiz üretilebilir
        if not os.path.exists(ses_yolu("bip")):
            bip_uret(ses_yolu("bip"))
        eksik = eksik_sesler()
        if eksik:
            _log(f"⚠️  [GÜVENLİK] Eksik ses dosyaları: {', '.join(eksik)} — "
                 f"internet varken 'python3 guvenlik.py --sesleri-uret' çalıştırın. "
                 f"Eksik uyarılar yerine bip çalacak.")
        _log(f"🛡️  Güvenlik katmanı başlıyor | sensör: {self.sensor.ad} | "
             f"tık < {TTC_UYARI_S:.1f} s | Dur < {TTC_ACIL_S:.1f} s | "
             f"en düşük yaklaşma hızı {MIN_YAKLASMA_HIZI:.2f} m/s | "
             f"titreşim: {'var' if self.titresim.var else 'yok'}")
        self.sensor_thread.start()
        if self.internet:
            self.internet.start()
        return self

    # ---------- arıza ----------
    def _ariza_degisti(self, arizali):
        if arizali:
            # Kullanıcı artık engel uyarısı alamayacak: bunu acil öncelikle söyle
            self.ses.acil_cal(self._dosya_ya_da_bip("sensor_calismiyor"))
            self.titresim.titret(1.0, 0.8)
        else:
            threading.Thread(target=self.ses.normal_cal,
                             args=(self._dosya_ya_da_bip("sensor_calisiyor"),),
                             daemon=True).start()

    @staticmethod
    def _dosya_ya_da_bip(ad):
        yol = ses_yolu(ad)
        return yol if os.path.exists(yol) else ses_yolu("bip")

    # ---------- okuma ----------
    def _okuma_geldi(self, mesafe):
        olaylar = self.mantik.guncelle(mesafe)
        if self.ekran:
            self._ekrana_yaz(mesafe)
        for olay, deger in olaylar:
            self.sayac[olay] += 1
            if olay == OLAY_TIK:
                self.ses.tik(deger)
                self.titresim.titret(deger, 0.05)
            elif olay == OLAY_DUR:
                self.ses.acil_cal(self._dosya_ya_da_bip("bip_dur"))
                self.titresim.titret(1.0, 0.4)
                if self.ekran and sys.stdout.isatty():
                    print()  # durum satırını koru
                _log(f"🛑 DUR — {mesafe:.2f} m, {self.mantik.hiz:.2f} m/s ile yaklaşıyor, "
                     f"çarpışmaya {deger:.1f} s")

    def _ekrana_yaz(self, mesafe):
        self._ekran_sayac += 1
        m = "  > 2.00 m" if mesafe is None else f"{mesafe:6.2f} m"
        ttc = "   —  " if math.isinf(self.mantik.ttc) else f"{self.mantik.ttc:4.1f} s"
        seviye = SEVIYE_ADI[self.mantik.seviye]
        satir = f"📏 {m} | hız {self.mantik.hiz:+5.2f} m/s | çarpışmaya {ttc} | {seviye:8}"
        if sys.stdout.isatty():
            print("\r" + satir, end="", flush=True)
        elif self._ekran_sayac % 5 == 0:  # dosyaya yönlendirildiyse satır satır, seyrek
            print(f"[{time.strftime('%H:%M:%S')}] {satir}", flush=True)

    # ---------- durum ----------
    @property
    def internet_var(self):
        return None if self.internet is None else self.internet.bagli

    @property
    def sensor_kapali(self):
        return isinstance(self.sensor, KapaliSensor)

    @property
    def son_mesafe(self):
        """Son 0,5 sn içinde okunan mesafe, yoksa None."""
        t = self.sensor_thread.son_okuma_zamani
        if t is None or time.monotonic() - t > 0.5:
            return None
        return self.sensor_thread.son_mesafe

    def durum_soyle(self):
        """İnternet ve sensör durumunu önceden kaydedilmiş seslerle söyler (internetsiz)."""
        parcalar = ["durum_internet_var" if self.internet_var else "durum_internet_yok"]
        if not self.sensor_kapali:
            parcalar.append("durum_sensor_yok" if self.sensor_thread.arizali else "durum_sensor_iyi")
        for p in parcalar:
            self.ses.normal_cal(self._dosya_ya_da_bip(p))

    def durdur(self):
        self.sensor_thread.durdur()
        if self.internet:
            self.internet.durdur()
        self.ses.durdur()
        self.titresim.kapat()


# ═════════════════════════ TEK BAŞINA TEST ═════════════════════════
def main():
    p = argparse.ArgumentParser(description="Güvenlik katmanı testi")
    p.add_argument("--sesleri-uret", action="store_true",
                   help="sesler/ klasöründeki dosyaları edge-tts ile üret (internet gerekir)")
    p.add_argument("--zorla", action="store_true", help="--sesleri-uret ile: var olanların üzerine yaz")
    p.add_argument("--mod", choices=["simulasyon", "gercek"], default=None,
                   help="sensör modu (varsayılan: .env içindeki SENSOR_MODU)")
    p.add_argument("--sessiz", action="store_true", help="ses çalma, sadece ekrana yaz")
    p.add_argument("--sure", type=float, default=0, help="bu kadar saniye sonra çık (0 = Ctrl+C'ye kadar)")
    p.add_argument("--internetsiz", action="store_true", help="internet izleyiciyi başlatma")
    p.add_argument("--senaryo", default="karisik", choices=list(SimulasyonSensoru.SENARYOLAR),
                   help="simülasyon senaryosu (varsayılan: karisik)")
    p.add_argument("--ariza", type=float, default=0, metavar="SN",
                   help="simülasyonda 3. saniyede başlayıp SN saniye süren sensör arızası")
    a = p.parse_args()

    if a.sesleri_uret:
        print("🔊 Ses dosyaları üretiliyor →", SES_DIZINI)
        sesleri_uret(zorla=a.zorla)
        return

    ariza = (3.0, a.ariza) if a.ariza > 0 else None
    sistem = GuvenlikSistemi(sensor=sensor_olustur(a.mod, ariza=ariza, senaryo=a.senaryo),
                             sessiz=a.sessiz, ekran=True,
                             internet_izle=not a.internetsiz).baslat()
    print("Ctrl+C ile çık\n")
    try:
        bitis = time.monotonic() + a.sure if a.sure > 0 else math.inf
        while time.monotonic() < bitis:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        sistem.durdur()
        print(f"\nÇıkış. Tık: {sistem.sayac[OLAY_TIK]}, Dur: {sistem.sayac[OLAY_DUR]}, "
              f"sensör hata sayısı: {sistem.sensor_thread.hata_sayisi}")


if __name__ == "__main__":
    main()
