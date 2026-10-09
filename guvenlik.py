"""
Asistif Gözlük - Güvenlik Katmanı (Aşama 1)
===========================================

İnternetten ve yapay zekâdan tamamen bağımsız çalışan engel uyarı sistemi.

Parçalar:
  - MesafeSensoru arayüzü: GercekSensor (VL53L0X) ve SimulasyonSensoru
  - SensorThread: sensörü ayrı bir thread'de ~15 Hz okur, hata olsa da çökmez
  - UyariMantigi: iki eşik (uyarı / acil), histerezis, bekleme süresi
  - SesYoneticisi: acil ve normal öncelikli ses çıkışı (acil, normali keser)
  - InternetIzleyici: 5 saniyede bir bağlantı kontrolü, değişince sesli bildirir
  - GuvenlikSistemi: hepsini birleştiren tek giriş noktası

Ayarlar (.env):
  SENSOR_MODU=simulasyon       # veya: gercek
  UYARI_ESIGI_M=1.2            # bu mesafenin altında "Dikkat"
  ACIL_ESIGI_M=0.7             # bu mesafenin altında "Dur"
  HISTEREZIS_M=0.15            # uyarıdan çıkmak için eşiğin bu kadar üstüne çıkmalı
  UYARI_BEKLEME_S=4            # aynı uyarı en erken bu kadar saniye sonra tekrar çalar
  ACIL_YAKLASMA_M=0.15         # acil durumda bu kadar daha yaklaşılırsa bekleme süresi beklenmez
  SENSOR_HZ=15                 # saniyedeki okuma sayısı
  INTERNET_KONTROL_S=5         # internet kontrol aralığı
  SIM_HATA_ORANI=0.005         # simülasyonda yapay okuma hatası olasılığı (0 = kapalı)
  SENSOR_ARIZA_S=2             # bu kadar saniye art arda hata → "Mesafe sensörü çalışmıyor"
  SES_UYANIK_TUT=1             # Bluetooth çıkışını uyutmamak için arka planda sessizlik çal (0 = kapalı)

Tek başına test:
  python3 guvenlik.py                 # simülasyon + ses
  python3 guvenlik.py --sessiz        # simülasyon, ses çalmadan sadece ekrana yazar
  python3 guvenlik.py --sure 60       # 60 saniye sonra kendiliğinden çıkar
  python3 guvenlik.py --ariza 5       # 3. saniyede başlayıp 5 sn süren sensör arızası simüle eder
  python3 guvenlik.py --sesleri-uret  # sesler/ klasöründeki WAV dosyalarını yeniden üretir (internet gerekir)
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
UYARI_ESIGI_M = _env_float("UYARI_ESIGI_M", 1.2)
ACIL_ESIGI_M = _env_float("ACIL_ESIGI_M", 0.7)
HISTEREZIS_M = _env_float("HISTEREZIS_M", 0.15)
UYARI_BEKLEME_S = _env_float("UYARI_BEKLEME_S", 4.0)
ACIL_YAKLASMA_M = _env_float("ACIL_YAKLASMA_M", 0.15)
SENSOR_HZ = _env_float("SENSOR_HZ", 15.0)
INTERNET_KONTROL_S = _env_float("INTERNET_KONTROL_S", 5.0)
SIM_HATA_ORANI = _env_float("SIM_HATA_ORANI", 0.005)
SENSOR_ARIZA_S = _env_float("SENSOR_ARIZA_S", 2.0)
# Bluetooth çıkışı 5 sn boşta kalınca uyuyor, uyanırken sesin başı kayboluyor.
# 1 iken arka planda sürekli sessizlik çalınır, çıkış hep uyanık kalır.
SES_UYANIK_TUT = (os.getenv("SES_UYANIK_TUT") or "1").strip() not in ("0", "hayir", "hayır", "false")

# Ses dosyaları
SES_DOSYALARI = {
    "dikkat": "Dikkat",
    "dur": "Dur",
    "internet_kesildi": "İnternet bağlantısı kesildi, sadece engel uyarısı aktif",
    "internet_geldi": "İnternet bağlantısı geri geldi",
    "sensor_calismiyor": "Mesafe sensörü çalışmıyor",
    "sensor_calisiyor": "Mesafe sensörü tekrar çalışıyor",
    "ev_modu": "Ev modu",
    "sokak_modu": "Sokak modu",
}
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
    Gerçekçi engel senaryosu, döngü halinde tekrarlar:
      3 m'de bekle → yavaşça yaklaş (yürüme hızı) → 0,4 m'de kısa dur →
      uzaklaş → 3 m'de bekle.
    Her okumaya küçük Gauss gürültüsü eklenir, ara sıra yapay okuma hatası atılır.
    """

    ad = "simulasyon"

    def __init__(self, hata_orani=SIM_HATA_ORANI, gurultu_m=0.02, ariza=None):
        self.hata_orani = hata_orani
        self.gurultu_m = gurultu_m
        # ariza=(başlangıç sn, süre sn): bu aralıkta her okuma hata verir (arıza testi).
        # Süre, sensör yeniden başlatılsa da ilk başlatmadan itibaren sayılır.
        self.ariza = ariza
        self._ilk_baslatma = None
        # (süre sn, başlangıç m, bitiş m)
        self.senaryo = [
            (2.0, 3.0, 3.0),   # uzakta, engel yok
            (8.0, 3.0, 0.4),   # ~0,33 m/s ile yaklaşma
            (2.0, 0.4, 0.4),   # engelin dibinde bekleme
            (6.0, 0.4, 3.0),   # uzaklaşma
            (2.0, 3.0, 3.0),
        ]
        self.toplam_sure = sum(s[0] for s in self.senaryo)
        self.t0 = None

    def baslat(self):
        self.t0 = time.monotonic()
        if self._ilk_baslatma is None:
            self._ilk_baslatma = self.t0

    def gercek_mesafe(self):
        t = (time.monotonic() - self.t0) % self.toplam_sure
        for sure, bas, bit in self.senaryo:
            if t < sure:
                oran = t / sure
                # yumuşak hızlanma/yavaşlama
                oran = 0.5 - 0.5 * math.cos(math.pi * oran)
                return bas + (bit - bas) * oran
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


def sensor_olustur(mod=None, ariza=None):
    """ariza=(başlangıç sn, süre sn) sadece simülasyonda kullanılır."""
    mod = (mod or SENSOR_MODU).strip().lower()
    if mod in ("gercek", "gerçek"):
        return GercekSensor()
    if mod not in ("simulasyon", "simülasyon"):
        print(f"⚠️  [GÜVENLİK] Bilinmeyen SENSOR_MODU={mod!r}, simülasyon kullanılıyor")
    return SimulasyonSensoru(ariza=ariza)


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


# ═════════════════════════ UYARI MANTIĞI ═════════════════════════
SEVIYE_YOK = 0
SEVIYE_UYARI = 1
SEVIYE_ACIL = 2
SEVIYE_ADI = {SEVIYE_YOK: "yok", SEVIYE_UYARI: "UYARI", SEVIYE_ACIL: "ACİL"}


class UyariMantigi:
    """
    Mesafe → uyarı seviyesi, histerezisli durum makinesi.

    Seviyeye giriş eşiğin altına inince, çıkış eşik + histerezisin üstüne
    çıkınca olur. Böylece eşiğin etrafındaki titremeler uyarıyı açıp kapatmaz.

    Ne zaman ses çalınır:
      - UYARI'ya girince "Dikkat" (son Dikkat'ten bu yana bekleme süresi geçtiyse)
      - ACİL'e girince her zaman "Dur" (bu bir tırmanmadır)
      - Seviye sürdükçe bekleme süresi dolunca hatırlatma olarak tekrar
      - ACİL'deyken engel ACIL_YAKLASMA_M kadar daha yaklaşırsa beklemeden "Dur"
    """

    def __init__(self, uyari_esigi=UYARI_ESIGI_M, acil_esigi=ACIL_ESIGI_M,
                 histerezis=HISTEREZIS_M, bekleme=UYARI_BEKLEME_S,
                 acil_yaklasma=ACIL_YAKLASMA_M, saat=time.monotonic):
        if acil_esigi >= uyari_esigi:
            raise ValueError(f"ACIL_ESIGI_M ({acil_esigi}) UYARI_ESIGI_M'den ({uyari_esigi}) küçük olmalı")
        self.uyari_esigi = uyari_esigi
        self.acil_esigi = acil_esigi
        self.histerezis = histerezis
        self.bekleme = bekleme
        self.acil_yaklasma = acil_yaklasma
        self.saat = saat
        self.seviye = SEVIYE_YOK
        self._son_calma = {SEVIYE_UYARI: -math.inf, SEVIYE_ACIL: -math.inf}
        self._son_acil_mesafe = math.inf

    def _yeni_seviye(self, d):
        s = self.seviye
        # Yukarı çıkış (yaklaşma): eşiğin altı
        if d < self.acil_esigi:
            return SEVIYE_ACIL
        if s == SEVIYE_ACIL:
            # ACİL'den çıkmak için acil eşiği + histerezisi geçmeli
            if d <= self.acil_esigi + self.histerezis:
                return SEVIYE_ACIL
            s = SEVIYE_UYARI
        if d < self.uyari_esigi:
            return SEVIYE_UYARI
        if s == SEVIYE_UYARI and d <= self.uyari_esigi + self.histerezis:
            return SEVIYE_UYARI
        return SEVIYE_YOK

    def guncelle(self, mesafe):
        """
        Yeni okumayı işler. Çalınması gereken bir uyarı varsa
        (seviye, sebep) döner, yoksa None.
        """
        d = math.inf if mesafe is None else mesafe
        simdi = self.saat()
        eski = self.seviye
        yeni = self._yeni_seviye(d)
        self.seviye = yeni

        if yeni == SEVIYE_YOK:
            self._son_acil_mesafe = math.inf
            return None

        if yeni < eski:
            # Engel uzaklaşıyor: alt seviyenin hatırlatması tam bekleme süresi sonra gelsin
            self._son_calma[yeni] = simdi
            return None

        gecen = simdi - self._son_calma[yeni]
        sebep = None
        if yeni > eski:
            if yeni == SEVIYE_ACIL:
                sebep = "acil eşiği aşıldı"
            elif gecen >= self.bekleme:
                sebep = "uyarı eşiği aşıldı"
        elif yeni == SEVIYE_ACIL and d <= self._son_acil_mesafe - self.acil_yaklasma:
            sebep = "engel yaklaşmaya devam ediyor"
        elif gecen >= self.bekleme and yeni == eski:
            sebep = "hatırlatma"

        if sebep is None:
            return None
        self._son_calma[yeni] = simdi
        if yeni == SEVIYE_ACIL:
            self._son_acil_mesafe = d
        return yeni, sebep


# ═════════════════════════ SES SİSTEMİ ═════════════════════════
class CikisUyanikTutucu(threading.Thread):
    """
    Varsayılan ses çıkışına sürekli sessizlik gönderir. PipeWire, çalan bir
    akış olduğu sürece çıkışı uyutmaz, böylece Bluetooth bağlantısı açık kalır
    ve uyarılar baştan kesilmeden, gecikmesiz çalar. Bedeli: kulaklık pili
    biraz daha hızlı biter.

    Varsayılan çıkış değişirse (başka kulaklık bağlandıysa) akışı yeniden açar.
    pacat ölürse 2 sn sonra yeniden başlatır.
    """

    ORNEKLEME = 8000
    PARCA = b"\x00\x00" * 800  # 100 ms sessizlik

    def __init__(self):
        super().__init__(daemon=True, name="ses-uyanik")
        self.durdur_olayi = threading.Event()
        self._proc = None
        self._kilit = threading.Lock()

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
             f"--rate={self.ORNEKLEME}", "--client-name=guvenlik-uyanik",
             "--stream-name=sessizlik"],
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
                p.stdin.write(self.PARCA)
                p.stdin.flush()
            except Exception:
                self._kapat()
                self.durdur_olayi.wait(2.0)
        self._kapat()

    def durdur(self):
        self.durdur_olayi.set()
        self._kapat()


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
        self._uyanik = None
        if SES_UYANIK_TUT and not sessiz:
            self._uyanik = CikisUyanikTutucu()
            self._uyanik.start()

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

    @property
    def acil_caliyor(self):
        return self._acil_mesgul

    def durdur(self):
        if self._uyanik is not None:
            self._uyanik.durdur()
        with self._kosul:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()


# ═════════════════════════ İNTERNET İZLEYİCİ ═════════════════════════
class InternetIzleyici(threading.Thread):
    """
    Her INTERNET_KONTROL_S saniyede bir bağlantıyı kontrol eder.
    Yanlış alarm olmasın diye kopma için art arda 2 başarısız kontrol gerekir.
    Durum değişince ilgili ses dosyasını bir kere çalar.
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
                        self._bildir("internet_geldi", "🌐 İnternet bağlantısı geri geldi")
                    elif self.bagli is None:
                        _log("🌐 İnternet bağlantısı var")
                    self.bagli = True
                else:
                    self._basarisiz += 1
                    if self._basarisiz >= self.kopma_esigi and self.bagli is not False:
                        self.bagli = False
                        self._bildir("internet_kesildi",
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
    adlar = list(SES_DOSYALARI) + ["bip", "bip_dur"]
    return [ad for ad in adlar if not os.path.exists(ses_yolu(ad))]


# ═════════════════════════ BİRLEŞİK SİSTEM ═════════════════════════
class GuvenlikSistemi:
    """
    Kullanım (ana programdan):
        guvenlik = GuvenlikSistemi().baslat()
        guvenlik.ses.normal_cal("/tmp/tts.mp3")   # normal öncelikli konuşma
    """

    UYARI_SESLERI = {SEVIYE_UYARI: "dikkat", SEVIYE_ACIL: "bip_dur"}

    def __init__(self, sensor=None, ses=None, sessiz=False, ekran=False, internet_izle=True):
        self.sensor = sensor or sensor_olustur()
        self.ses = ses or SesYoneticisi(sessiz=sessiz)
        self.mantik = UyariMantigi()
        self.ekran = ekran
        self.sensor_thread = SensorThread(self.sensor, self._okuma_geldi,
                                          ariza_bildir=self._ariza_degisti)
        self.internet = InternetIzleyici(self.ses) if internet_izle else None
        self.son_uyari = None
        self._sayac = 0

    def baslat(self):
        eksik = eksik_sesler()
        if "bip" in eksik:
            os.makedirs(SES_DIZINI, exist_ok=True)
            bip_uret(ses_yolu("bip"))  # internetsiz üretilebilir
            eksik.remove("bip")
        if eksik:
            _log(f"⚠️  [GÜVENLİK] Eksik ses dosyaları: {', '.join(eksik)} — "
                 f"internet varken 'python3 guvenlik.py --sesleri-uret' çalıştırın. "
                 f"Eksik uyarılar yerine bip çalacak.")
        _log(f"🛡️  Güvenlik katmanı başlıyor | sensör: {self.sensor.ad} | "
             f"uyarı < {UYARI_ESIGI_M:.2f} m | acil < {ACIL_ESIGI_M:.2f} m | "
             f"histerezis {HISTEREZIS_M:.2f} m | bekleme {UYARI_BEKLEME_S:.0f} s")
        self.sensor_thread.start()
        if self.internet:
            self.internet.start()
        return self

    def _ariza_degisti(self, arizali):
        if arizali:
            # Kullanıcı artık engel uyarısı alamayacak: bunu acil öncelikle söyle
            self.ses.acil_cal(self._dosya_ya_da_bip("sensor_calismiyor"))
        else:
            threading.Thread(target=self.ses.normal_cal,
                             args=(self._dosya_ya_da_bip("sensor_calisiyor"),),
                             daemon=True).start()

    @staticmethod
    def _dosya_ya_da_bip(ad):
        yol = ses_yolu(ad)
        return yol if os.path.exists(yol) else ses_yolu("bip")

    def _okuma_geldi(self, mesafe):
        sonuc = self.mantik.guncelle(mesafe)
        if self.ekran:
            self._ekrana_yaz(mesafe)
        if sonuc is None:
            return
        seviye, sebep = sonuc
        ad = self.UYARI_SESLERI[seviye]
        dosya = ses_yolu(ad)
        if not os.path.exists(dosya):
            dosya = ses_yolu("bip")
        self.ses.acil_cal(dosya)
        self.son_uyari = (time.monotonic(), seviye, sebep, mesafe)
        mesafe_str = "menzil dışı" if mesafe is None else f"{mesafe:.2f} m"
        ikon = "🛑" if seviye == SEVIYE_ACIL else "⚠️ "
        if self.ekran and sys.stdout.isatty():
            print()  # durum satırını koru
        _log(f"{ikon} {SEVIYE_ADI[seviye]} — {mesafe_str} — {sebep} → {ad}.wav")

    def _ekrana_yaz(self, mesafe):
        self._sayac += 1
        seviye = SEVIYE_ADI[self.mantik.seviye]
        if mesafe is None:
            m, cubuk = "  > 2.00 m", ""
        else:
            m = f"{mesafe:6.2f} m"
            cubuk = "█" * int(max(0, 3.0 - mesafe) / 3.0 * 30)
        satir = f"📏 {m}  [{seviye:5}]  {cubuk:<30}"
        if sys.stdout.isatty():
            print("\r" + satir, end="", flush=True)
        elif self._sayac % 5 == 0:  # dosyaya yönlendirildiyse satır satır, seyrek
            print(f"[{time.strftime('%H:%M:%S')}] {satir.rstrip()}", flush=True)

    @property
    def internet_var(self):
        return None if self.internet is None else self.internet.bagli

    def durdur(self):
        self.sensor_thread.durdur()
        if self.internet:
            self.internet.durdur()
        self.ses.durdur()


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
    p.add_argument("--ariza", type=float, default=0, metavar="SN",
                   help="simülasyonda 3. saniyede başlayıp SN saniye süren sensör arızası")
    a = p.parse_args()

    if a.sesleri_uret:
        print("🔊 Ses dosyaları üretiliyor →", SES_DIZINI)
        sesleri_uret(zorla=a.zorla)
        return

    ariza = (3.0, a.ariza) if a.ariza > 0 else None
    sistem = GuvenlikSistemi(sensor=sensor_olustur(a.mod, ariza=ariza), sessiz=a.sessiz, ekran=True,
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
        print(f"\nÇıkış. Sensör hata sayısı: {sistem.sensor_thread.hata_sayisi}")


if __name__ == "__main__":
    main()
