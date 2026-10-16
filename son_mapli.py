"""
Asistif Gözlük
==============

Görme engelli kullanıcı için Türkçe konuşan akıllı gözlük (tez projesi).

İKİ MOD (sesle "ev modu" / "sokak modu", klavyede h / s):
    Ev modu   — sessiz yardımcı: sadece sorulunca konuşur. Arka planda anahtar,
                cüzdan, telefon ve bilgisayarın yerini metin olarak hatırlar
                ("masanın üzerinde, sol tarafta"); görüntü saklanmaz.
                "Anahtarım nerede?" → "En son 10 dakika önce masanın üzerinde gördüm."
    Sokak modu — akıllı anlatıcı (orta yol): yürürken susar; basamak, yaya geçidi,
                trafik ışığı, gelen otobüs gibi olayları hemen, eczane, durak, tabela
                gibi yerleri en fazla 30 sn'de bir söyler. "Daha çok/az anlat" ile değişir.
    İki modda da: engel uyarısı (sensör, internetsiz), sorulara cevap, yazı ve para
    okuma, "ne görüyorsun" (zengin tarif), "çevreyi tara", navigasyon.

NAVİGASYON:
    "Pamukkale Üniversitesi'ne götür" → telefon GPS'i ile adım adım yönlendirme,
    rotadan sapınca yeniden hesaplama, "neredeyim?". Google Maps anahtarı yoksa
    OpenStreetMap kullanılır (ücretsiz). Telefonda https://<pi-ip>:8443/telefon açılır.

MESAFE:
    Lazer sensör (VL53L0X) varsa kesin mesafe ondan. Yoksa (SENSOR_MODU=kapali) kameradan
    derinlik yapay zekâsı (mesafe.py, Depth Anything V2 Metric): tam öndeki mesafe ve YOLO'nun
    bulduğu her nesne için "saat 1 yönünde sandalye, yaklaşık 1,8 metre". Boyu bilinen nesnelerle
    kendini kalibre eder, internetsiz çalışır (~1 sn/kare).

JÜRİ PANELİ (sunum için): http://<pi-ip>:8080/ — kamera + YOLO kutuları, sensör
    grafiği, konuşma dökümü, eşya hafızası, harita, gecikme ölçümleri.

YAPAY ZEKÂ:
    Varsayılan Gemini Live (canli.py): cevaplar ses olarak gelir, mikrofon basılı
    tutulurken canlı akar, mesafe sensörden fonksiyonla alınır. Live bağlı değilse
    REST + edge-tts, o da yoksa sensörle cevap.

KONTROL (tek düğme, kontrol.py):
    kısa basış → "Önümde ne var?" (konuşma sürüyorsa önce susturur)
    uzun basış → basılı tutarken soru sor, bırakınca cevaplanır
    çift basış → son söyleneni tekrarla
    Klavye: d = kısa, SPACE = uzun başlat/bitir, r = çift, h = ev, s = sokak, q = çıkış

ÇALIŞTIRMADAN ÖNCE:
    1. Bluetooth kulaklık bağlı, mikrofon ve hoparlör varsayılan olmalı:
         pactl set-default-source bluez_input.20:23:03:25:87:1C
         pactl set-default-sink bluez_output.20_23_03_25_87_1C.1
    2. .env: GEMINI_API_KEY (zorunlu), GOOGLE_MAPS_API_KEY (isteğe bağlı),
       DUGME_PIN, TITRESIM_PIN (donanım gelince). .env git'e gönderilmez.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import sys
import cv2
import json
import time
import wave
import select
import atexit
import base64
import shutil
import termios
import requests
import threading
import asyncio
import edge_tts
import numpy as np
import sounddevice as sd
from queue import Queue
from collections import deque
from picamera2 import Picamera2
from dotenv import load_dotenv
import anlatici
import goru
import guvenlik
import hafiza
import kontrol
import kulaklik
import mesafe
import navigasyon
import panel
import sesli_komut

# ───────────────── CONFIG ─────────────────
# API key'ler bu dosyayla aynı klasördeki .env dosyasından okunur.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

if not API_KEY:
    print("❌ HATA: .env dosyasında GEMINI_API_KEY eksik veya boş.")
    print("   .env.example dosyasını .env adıyla kopyalayıp anahtarı doldurun.")
    sys.exit(1)
# Maps anahtarı isteğe bağlı: yoksa program çalışır, sadece navigasyon kapalı olur
if not GOOGLE_MAPS_API_KEY:
    print("ℹ️  GOOGLE_MAPS_API_KEY boş — navigasyon OpenStreetMap ile çalışacak.")

# Not: 2.5-flash-lite multimodal'dır. Eğer ses inputuyla hata verirse
# "gemini-2.5-flash" olarak değiştir.
MODEL_NAME = "gemini-2.5-flash-lite"
MODEL_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
# Key URL'de değil header'da gider — hata mesajlarında URL basılırsa key görünmesin
GEMINI_HEADERS = {"x-goog-api-key": API_KEY}

HTTP_TIMEOUT = 25

# Telefon GPS'i gelene kadar kullanılan sabit test konumu (Denizli)
TEST_KONUMU = (37.73831482466314, 29.10723965422727)

# ───────────────── AYARLAR (mod, konuşma düzeyi, anlatıcı sıklığı) ─────────────────
AYAR_DOSYASI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ayarlar.json")
DUZEY_KISA = sesli_komut.KOMUT_KISA
DUZEY_DETAYLI = sesli_komut.KOMUT_DETAYLI
DUZEY_SESSIZ = sesli_komut.KOMUT_SESSIZ
MOD_EV, MOD_SOKAK = "ev", "sokak"


def ayarlari_yukle():
    try:
        with open(AYAR_DOSYASI) as f:
            return json.load(f)
    except Exception:
        return {}


def ayarlari_kaydet():
    try:
        with open(AYAR_DOSYASI, "w") as f:
            json.dump(ayarlar, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️  Ayarlar kaydedilemedi: {e}")


ayarlar = {"konusma_duzeyi": DUZEY_KISA, "mod": MOD_SOKAK, "siklik": "orta", **ayarlari_yukle()}

# ───────────────── GÜVENLİK KATMANI ─────────────────
# Mesafe sensörü + engel uyarıları + internet izleyici. İnternetten ve yapay
# zekâdan bağımsız kendi thread'lerinde çalışır. Derinlik modeli yüklenmeden
# önce başlatılır ki yükleme sırasında da uyarılar çalışsın.
# Tüm sesler (TTS dahil) bu ses yöneticisinden geçer: "Dur" uyarısı acil
# önceliklidir ve o an çalan konuşmayı keser. Tıklar konuşmanın üstüne karışır.
ses_yoneticisi = guvenlik.SesYoneticisi()
guvenlik_sistemi = guvenlik.GuvenlikSistemi(ses=ses_yoneticisi).baslat()

# Sesli ayar komutları için Türkçe vosk modeli, açılışı geciktirmemek için arka planda yüklenir
komut_tanici = sesli_komut.KomutTanici().yukle_arkaplanda()

# ───────────────── CAMERA ─────────────────
class Camera:
    def __init__(self):
        self.picam2 = Picamera2()
        # 1280 × 960: anahtar ve cüzdan gibi küçük eşyalar Gemini'ye giden karede seçilebilsin.
        # Kamera bu boyutta da saniyede ~15 kare veriyor; YOLO ve derinlik kendileri küçültüyor.
        boyut = (int(os.getenv("KAMERA_GENISLIK") or 1280), int(os.getenv("KAMERA_YUKSEKLIK") or 960))
        self.picam2.configure(self.picam2.create_preview_configuration(
            main={"format": "RGB888", "size": boyut}
        ))
        self.frame = None
        self.lock = threading.Lock()

    def start(self):
        self.picam2.start()
        threading.Thread(target=self.update, daemon=True).start()
        return self

    def update(self):
        print("CAMERA THREAD STARTED")
        while True:
            try:
                f = self.picam2.capture_array()
                with self.lock:
                    self.frame = f
            except Exception as e:
                print("CAMERA FAIL:", e)
                time.sleep(0.3)

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()


# ───────────────── VOICE RECORDER ─────────────────
class VoiceRecorder:
    """
    Push-to-talk: start() kayda başlar, stop() durdurup WAV kaydeder.

    Mikrofon akışı sürekli açık kalır ve son PREROLL_S saniye hafızada tutulur.
    Akışı SPACE'e basınca açmak 150-350 ms sürüyordu, Bluetooth gecikmesiyle
    birlikte "ev modu" gibi kısa komutların başı kayboluyordu. Artık kayıt,
    SPACE'e basılmadan hemen önceki sesi de içeriyor.
    """

    PREROLL_S = 0.6

    def __init__(self, samplerate=16000, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self.frames = []
        self.recording = False
        self.stream = None
        self.lock = threading.Lock()
        self._preroll = deque()
        self._preroll_len = 0
        self.dinleyici = None  # kayıt sırasında her mikrofon parçasıyla çağrılır

    def _callback(self, indata, frame_count, time_info, status):
        block = indata.copy()
        if self.recording:
            self.frames.append(block)
            dinleyici = self.dinleyici
            if dinleyici is not None:
                dinleyici(block.tobytes())  # Gemini Live'a canlı akış
        else:
            self._preroll.append(block)
            self._preroll_len += len(block)
            limit = int(self.samplerate * self.PREROLL_S)
            while self._preroll and self._preroll_len - len(self._preroll[0]) >= limit:
                self._preroll_len -= len(self._preroll.popleft())

    def open(self):
        """Mikrofon akışını açar (açıksa dokunmaz). Başarılıysa True."""
        if self.stream is not None and self.stream.active:
            return True
        if self.stream is not None:  # kulaklık koptuysa akış ölmüş olabilir
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        try:
            self._preroll.clear()
            self._preroll_len = 0
            self.stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype='int16',
                callback=self._callback,
            )
            self.stream.start()
            return True
        except Exception as e:
            print(f"❌ Mikrofon açılamadı: {e}")
            self.stream = None
            return False

    def yeniden_ac(self):
        """Kulaklık kopup gelince: eski (ölü) mikrofon akışını kapatıp yenisini aç."""
        with self.lock:
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            return self.open()

    def start(self):
        with self.lock:
            if self.recording:
                return
            if not self.open():
                return
            self.frames = list(self._preroll)  # basmadan önceki 0,6 sn
            self._preroll.clear()
            self._preroll_len = 0
            self.recording = True
            print("\n🎙️  Dinliyorum...")
            return b"".join(f.tobytes() for f in self.frames)

    def stop(self, path="/tmp/prompt.wav"):
        with self.lock:
            if not self.recording:
                return None
            self.recording = False  # akış açık kalır, ön kayıt tamponu dolmaya devam eder

            if not self.frames:
                print("⚠️  Boş kayıt")
                return None

            audio = np.concatenate(self.frames, axis=0)
            dur = len(audio) / self.samplerate

            if dur < 0.3 + self.PREROLL_S:
                print(f"⚠️  Çok kısa ({dur:.1f}s), atlanıyor")
                return None

            with wave.open(path, 'wb') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)  # int16 = 2 byte
                wf.setframerate(self.samplerate)
                wf.writeframes(audio.tobytes())

            print(f"🎙️  Kayıt durdu ({dur:.1f}s)")
            return path


recorder = VoiceRecorder()
olay_kuyrugu = Queue()   # düğme olayları → ana döngü
SORU = "soru"            # (SORU, wav_yolu) olayı: uzun basış bitti, kayıt hazır


# ───────────────── TERMİNAL (SSH, klavye yedeği) ─────────────────
# stdin'i cbreak moduna alıp Enter beklemeden tek tuş yakalıyoruz.
# VS Code SSH terminal'inde sorunsuz çalışır.

_old_term_settings = None


def setup_raw_terminal():
    """Terminal'i tek-tuş moduna al, echo'yu kapat. Çıkışta otomatik restore."""
    global _old_term_settings
    if not sys.stdin.isatty():
        print("⚠️  stdin tty değil — klavye kontrolü devre dışı")
        return False
    _old_term_settings = termios.tcgetattr(sys.stdin)
    new = termios.tcgetattr(sys.stdin)
    # ICANON kapat → Enter beklenmesin, ECHO kapat → bastıkça ekrana yazmasın
    new[3] = new[3] & ~(termios.ICANON | termios.ECHO)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, new)
    atexit.register(restore_terminal)
    return True


def restore_terminal():
    if _old_term_settings is not None:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term_settings)


# ───────────────── GEMINI İSTEĞİ ─────────────────
_rate_limit_cooldown_until = 0.0  # epoch — bu zamandan önce API çağrısı atlanır


def _gemini_post(payload, label="", max_retries=2):
    """429 → uzun cool-down (retry yok). 5xx → kısa retry."""
    global _rate_limit_cooldown_until

    # Cool-down kontrolü — rate limit aktifse API'yi hiç çağırma
    now = time.time()
    if now < _rate_limit_cooldown_until:
        remaining = _rate_limit_cooldown_until - now
        print(f"  ⏸️ Rate limit cool-down: {remaining:.0f}s daha (API atlandı)")
        return None

    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            r = requests.post(MODEL_URL, json=payload, headers=GEMINI_HEADERS, timeout=HTTP_TIMEOUT)
            elapsed = time.time() - t0
            print(f"  ⚡ AI ({label}): {elapsed:.1f}s - {r.status_code}")

            if r.status_code == 200:
                data = r.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    return None
                return parts[0].get("text", "").strip()

            # 429 = rate limit. Retry zaman kaybı — uzun cool-down koy, dön.
            if r.status_code == 429:
                _rate_limit_cooldown_until = time.time() + 30
                print(f"  ❌ 429 RATE LIMIT — 30s cool-down başladı")
                print(f"     Free tier: ~15 req/dakika. Çözümler:")
                print(f"     • Billing'i aç (~$200 ücretsiz kredi)")
                print(f"     • Daha seyrek soru sor")
                return None

            # 5xx = geçici sunucu hatası. Kısa backoff'la retry.
            if r.status_code in (500, 502, 503, 504):
                if attempt < max_retries:
                    wait = 2 ** attempt  # 1s, 2s
                    print(f"  ⏳ {r.status_code} (geçici) — {wait}s sonra tekrar ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  ❌ {r.status_code} sonrası retry başarısız")
                    return None

            # Kalıcı hata (400, 403, vb.)
            print(f"  ❌ HATA ({r.status_code}): {r.text[:400]}")
            return None

        except requests.Timeout:
            print(f"⏱️ Timeout (deneme {attempt+1}/{max_retries+1})")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            print(f"AI ERROR: {type(e).__name__}: {e}")
            return None

    return None


# ───────────────── PANEL YAYINI ─────────────────
juri_paneli = None  # main() içinde kurulur


def yayinla(tur, veri):
    if juri_paneli is not None:
        juri_paneli.yayinla(tur, veri)


def olcum(ad, deger, birim="s"):
    if juri_paneli is not None:
        juri_paneli.olcum(ad, deger, birim)


# ───────────────── KONUŞMA ─────────────────
# Her konuşmanın bir "nesli" var. Kısa basışla susturulunca nesil artar ve
# henüz sentezlenmekte olan cümle de çalınmadan atılır.
_konusma_nesli = 0
_konusma_kilidi = threading.Lock()
_konusma_hazirlaniyor = threading.Event()
SON_KONUSMA = "/tmp/son_konusma.mp3"
son_ses = None  # çift basışta tekrarlanacak dosya


def konusmayi_kes():
    """Kısa basış: o an çalan ya da hazırlanan konuşmayı susturur. Bir şey kesildiyse True."""
    global _konusma_nesli
    with _konusma_kilidi:
        hazirlaniyor = _konusma_hazirlaniyor.is_set()
        _konusma_nesli += 1
    kesildi = ses_yoneticisi.normal_kes()
    canli_kesildi = canli.konusmayi_kes() if canli is not None else False
    sebep = [ad for ad, v in (("hazırlanan konuşma", hazirlaniyor), ("çalan ses", kesildi),
                              ("Live cevabı", canli_kesildi)) if v]
    if sebep:
        print(f"   (kesilen: {', '.join(sebep)})")
    return bool(sebep)


def oynat(yol):
    """Önceden kaydedilmiş bir sesi çalar (internetsiz) ve tekrar için hatırlar."""
    global son_ses
    son_ses = yol
    ses_yoneticisi.normal_cal(yol)


def konus(text):
    """Metni edge-tts ile seslendirir (internet gerekir), bitene ya da kesilene kadar bekler."""
    global son_ses
    print(f"📢 {text}")
    yayinla("gozluk", text)
    with _konusma_kilidi:
        nesil = _konusma_nesli
    _konusma_hazirlaniyor.set()
    f = f"/tmp/tts_{threading.get_ident()}.mp3"
    try:
        # Microsoft servisi ara sıra 403 dönüyor: bir kez daha dene
        for deneme in range(2):
            try:
                asyncio.run(edge_tts.Communicate(text, "tr-TR-EmelNeural").save(f))
                break
            except Exception as e:
                if deneme == 1:
                    print(f"TTS ERROR: {type(e).__name__}: {str(e)[:120]}")
                    return
                time.sleep(1.0)
    finally:
        _konusma_hazirlaniyor.clear()
    if nesil != _konusma_nesli:
        print("🔇 Susturuldu")
        return
    shutil.copyfile(f, SON_KONUSMA)
    son_ses = SON_KONUSMA
    if not ses_yoneticisi.normal_cal(f):
        print("🔇 Konuşma kesildi")


def soyle(metin):
    """Kullanıcıya bir cümle söyler: Live bağlıysa onun sesiyle, değilse edge-tts ile."""
    if canli_hazir() and canli.soyle(metin):
        return
    if internet_var():
        konus(metin)
    else:
        print(f"🔇 İnternet yok, söylenemedi: {metin}")


def internet_var():
    # İzleyici henüz ilk kontrolü yapmadıysa (None) var sayıp denenir
    return guvenlik_sistemi.internet_var is not False


# ───────────────── MOD VE AYARLAR ─────────────────
anlatim = anlatici.Anlatici(siklik=ayarlar.get("siklik", "orta"))


def mod():
    return ayarlar.get("mod", MOD_SOKAK)


def durum_ozeti():
    nav_durum = navigator.durum() if navigator else None
    return {
        "mod": "home" if mod() == MOD_EV else "street",
        "duzey": ayarlar.get("konusma_duzeyi"),
        "siklik": anlatim.siklik,
        "internet": bool(internet_var()),
        "ai": ("Live (bağlı)" if canli_hazir() else "Live (bağlanıyor)") if canli is not None else "REST",
        "hafiza": esya_hafizasi.liste(),
        "nav": nav_durum,
        "gozcu": dict(canli_gozcu.sayac) if canli_gozcu is not None else None,
        "kulaklik": None if kulaklik_bekcisi is None or not kulaklik_bekcisi.etkin else kulaklik_bekcisi.bagli,
    }


def mod_degistir(yeni, sesli_onay=True):
    """yeni: "ev" | "sokak". sesli_onay: önceden kaydedilmiş "Ev modu" sesini çal."""
    if yeni not in (MOD_EV, MOD_SOKAK):
        return
    ayarlar["mod"] = yeni
    ayarlari_kaydet()
    print(f"\n{'🏠 EV' if yeni == MOD_EV else '🚶 SOKAK'} MODU")
    for oturum in (canli, canli_gozcu):
        if oturum is not None:
            oturum.not_dus(f"Mod değişti: artık {yeni} modundasın.")
    yayinla("durum", durum_ozeti())
    if sesli_onay:
        oynat(guvenlik.ses_yolu(f"{yeni}_modu"))


def siklik_degistir(yon):
    yeni = anlatim.siklik_degistir(yon)
    ayarlar["siklik"] = yeni
    ayarlari_kaydet()
    print(f"⚙️  Anlatıcı sıklığı: {yeni} ({anlatim.aralik:.0f} sn)")
    yayinla("durum", durum_ozeti())
    return yeni


def ayari_uygula(komut):
    """Sesli komutu uygular (internetsiz; vosk ya da REST yolundan gelir)."""
    if komut in (DUZEY_KISA, DUZEY_DETAYLI, DUZEY_SESSIZ):
        ayarlar["konusma_duzeyi"] = komut
        ayarlari_kaydet()
        print(f"⚙️  Konuşma düzeyi: {komut}")
        if canli is not None:
            canli.not_dus(f"Bundan sonra konuşma düzeyi: {komut}.")
        yayinla("durum", durum_ozeti())
        oynat(guvenlik.ses_yolu(f"duzey_{komut}"))
    elif komut == sesli_komut.KOMUT_DURUM:
        guvenlik_sistemi.durum_soyle()
    elif komut == sesli_komut.KOMUT_TEKRAR:
        tekrarla()
    elif komut in (sesli_komut.KOMUT_EV, sesli_komut.KOMUT_SOKAK):
        mod_degistir(komut)
    elif komut in (sesli_komut.KOMUT_DAHA_COK, sesli_komut.KOMUT_DAHA_AZ):
        siklik_degistir(komut)
        oynat(guvenlik.ses_yolu("siklik_cok" if komut == sesli_komut.KOMUT_DAHA_COK else "siklik_az"))
    elif komut.startswith(sesli_komut.KOMUT_NEREDE):
        soyle(esya_hafizasi.sorgula(komut[len(sesli_komut.KOMUT_NEREDE):]))


# ───────────────── EŞYA HAFIZASI ─────────────────
esya_hafizasi = hafiza.EsyaHafizasi(
    degisti=lambda esya, kayit: yayinla("hafiza", {"esya": esya, "kayit": {**kayit, "once": "az önce"}}))


def yolo_esya_gordu(sinif, kutu, digerleri, genislik):
    """YOLO telefon ya da bilgisayar gördü: ev modunda yerini internetsiz kaydet."""
    if mod() != MOD_EV:
        return
    tarif = hafiza.yolo_konum_tarifi(kutu, digerleri, genislik)
    # "sol tarafta" gibi sadece yöne bağlı tarif sonradan işe yaramaz: yüzey ya da yakında bir eşya şart
    if "üzerinde" in tarif or "yanında" in tarif:
        esya_hafizasi.kaydet(hafiza.YOLO_ESYA[sinif], tarif, "yolo")


# ───────────────── MESAFE (sensör + kamera derinlik yapay zekâsı) ─────────────────
kamera_mesafe = None  # main() içinde kurulur


def en_yakin_mesafe():
    """(mesafe_m, kaynak): önce lazer sensör, yoksa kamera tahmini; hiçbiri yoksa (None, None)."""
    if not guvenlik_sistemi.sensor_kapali and not guvenlik_sistemi.sensor_thread.arizali:
        return guvenlik_sistemi.son_mesafe, "sensör"
    o = kamera_mesafe.guncel() if kamera_mesafe else None
    if o is not None:
        return o["tam_on"], "kamera"
    return None, None


def mesafe_bilgisi_metni(_sensor_m=None):
    """Yapay zekâya verilen mesafe bilgisi: lazer sensör (kesin) + kamera tahmini (yaklaşık)."""
    parcalar = []
    if not guvenlik_sistemi.sensor_kapali and not guvenlik_sistemi.sensor_thread.arizali:
        m = guvenlik_sistemi.son_mesafe
        parcalar.append("Lazer sensör (kesin): tam önde 2 metre içinde engel yok." if m is None else
                        f"Lazer sensör (kesin): tam öndeki nesne {m:.1f} metre.".replace(".", ",", 1))
    if kamera_mesafe is not None:
        parcalar.append(kamera_mesafe.ozet_metni())
    return " ".join(p for p in parcalar if p)


ENGEL_SES = {"person": "insan", "chair": "sandalye", "dining table": "masa", "couch": "kanepe", "bed": "yatak",
             "car": "araba", "bicycle": "bisiklet", "motorcycle": "motosiklet", "bus": "otobus", "truck": "kamyon",
             "bench": "bank", "dog": "kopek", "potted plant": "saksi", "refrigerator": "buzdolabi", "suitcase": "bavul"}
_engel_sayac = 0


def engel_uyarisi(e):
    """
    Kameradan engel uyarısı (mesafe.py): "Önünde sandalye var, sağdan geç"; çok yakınsa önce bip + "Dur".
    Önceden kaydedilmiş seslerden birleştirilir: internet gerekmez, anında çalar, konuşmayı keser.
    Sensör takılıysa uyarıyı sensör verir. Ev modunda kapalı (ENGEL_UYARI_EV=1 ile açılır).
    """
    global _engel_sayac
    if not guvenlik_sistemi.sensor_kapali:
        return
    if mod() == MOD_EV and (os.getenv("ENGEL_UYARI_EV") or "0").strip() != "1":
        return
    # Aynı engelin hatırlatması, kullanıcının sorduğu bir cevap çalarken onu kesmesin.
    # Yeni engel ya da "Dur"a yükselme her zaman keser.
    # Sessiz gözcü turları sayılmaz: sadece kullanıcıya çalınan bir cevap
    cevap_suruyor = ses_yoneticisi.normal_caliyor or (
        canli is not None and canli._tur is not None and canli._tur["tur"] == "cevap" and not canli._tur["atla"])
    if e.get("tekrar") and cevap_suruyor:
        print(f"🚧 (hatırlatma atlandı, cevap sürüyor) önünde {e['ad']} {e['mesafe']:.1f} m")
        return
    parcalar = []
    if e["seviye"] == 2:
        parcalar.append("bip_dur")
    parcalar.append("on_" + ENGEL_SES.get(e["sinif"], "engel"))
    parcalar.append({"sag": "gec_sag", "sol": "gec_sol"}.get(e["taraf"], "gec_yok"))
    yollar = [guvenlik.ses_yolu(p) for p in parcalar if os.path.exists(guvenlik.ses_yolu(p))]
    metin = (f"{'DUR — ' if e['seviye'] == 2 else ''}önünde {e['ad']} ({e['mesafe']:.1f} m), "
             f"{ {'sag': 'sağdan geç', 'sol': 'soldan geç'}.get(e['taraf'], 'önü kapalı') }")
    print(f"🚧 {metin}")
    yayinla("uyari", metin)
    if not yollar:
        ses_yoneticisi.acil_cal(guvenlik.ses_yolu("bip"))
        return
    # Parçaları tek dosyada birleştir (arada kısa sessizlik): iki ayrı çalma gecikmesi olmasın
    ses = [guvenlik._wav_oku(y) for y in yollar]
    bosluk = np.zeros(int(guvenlik.ORNEKLEME_HIZI * 0.08), np.float32)
    birlesik = np.concatenate([x for parca in ses for x in (parca, bosluk)])
    _engel_sayac += 1
    yol = f"/tmp/engel_{_engel_sayac % 4}.wav"
    guvenlik._wav_yaz(yol, birlesik)
    ses_yoneticisi.acil_cal(yol)
    guvenlik_sistemi.titresim.titret(1.0 if e["seviye"] == 2 else 0.6, 0.4 if e["seviye"] == 2 else 0.15)


# ───────────────── KULAKLIK ─────────────────
kulaklik_bekcisi = None  # main() içinde kurulur


def kulaklik_degisti(bagli):
    if bagli:
        recorder.yeniden_ac()  # mikrofon yeni bağlanan kulaklığa geçsin
        yayinla("uyari", "Kulaklık yeniden bağlandı")
        threading.Thread(target=ses_yoneticisi.normal_cal, args=(guvenlik.ses_yolu("ton_hazir"),),
                         daemon=True).start()
    else:
        yayinla("uyari", "Kulaklık bağlantısı koptu, yeniden bağlanılıyor")
    yayinla("durum", durum_ozeti())


# ───────────────── NAVİGASYON ─────────────────
navigator = None  # main() içinde kurulur


def navigasyon_duyur(metin, yon):
    """Navigasyon duyurusu: Live bağlıysa görüntüdeki bir işaretle zenginleştirerek söyler."""
    yayinla("nav_duyuru", metin)
    if canli_hazir() and canli.yonlendir(metin):
        return
    if internet_var():
        konus(metin)


# ───────────────── GEMINI İSTEMLERİ (REST yedeği) ─────────────────
YON_KURALI = (
    "Yönleri saat sistemiyle söyle: görüntünün ortası saat 12, sol kenarı saat 10, "
    "sağ kenarı saat 2. Örnek: 'Saat 1 yönünde kapı.' "
)


def _duzey_kurali():
    if ayarlar.get("konusma_duzeyi") == DUZEY_DETAYLI:
        return "En fazla 3 kısa cümle kullan. "
    return "Tek cümle, en fazla 12 kelime. "


def _mesafe_bilgisi():
    bilgi = mesafe_bilgisi_metni()
    return (bilgi + " " if bilgi else "") + \
        "Bunlar dışında mesafe uydurma. Mesafe söylerken ondalığı virgülle yaz (1,5 metre). "


def istem_tarif():
    return (
        "Sen görme engelli bir kullanıcının gözlüğüsün. Kullanıcı 'önümde ne var' diye sordu. "
        "Türkçe cevap ver. " + _duzey_kurali() +
        "En önemli şeyi önce söyle: yoldaki engel, basamak, kapı, insan, araç. "
        "Önemsiz eşyaları sayma. " + YON_KURALI + _mesafe_bilgisi() +
        "Görüntü karanlık, bulanık ya da kapalıysa sadece bunu söyle. "
        "Selamlama ve giriş cümlesi yok."
    )


def istem_soru():
    return (
        "Sen görme engelli bir kullanıcının gözlüğüsün. Ses kaydında kullanıcının sorusu, "
        "görüntüde önündeki sahne var. Türkçe, net ve doğrudan cevap ver. " + _duzey_kurali() +
        YON_KURALI + _mesafe_bilgisi() +
        "Yazı, etiket, ilaç kutusu ya da para soruluyorsa okuyup söyle. "
        "'Ne görüyorsun' gibi isteklerde en fazla 5 cümleyle zengin anlat. "
        "Selamlama ve giriş cümlesi yok.\n"
        "Özel durumlar, bunlarda SADECE belirtilen kelimeyi yaz:\n"
        "- Kullanıcı bir yere gitmek istiyorsa: NAVIGASYON: <yer adı>\n"
        "- Nerede olduğunu soruyorsa: NEREDEYIM\n"
        "- Anahtar, cüzdan, telefon ya da bilgisayarının yerini soruyorsa: NEREDE: <eşya>\n"
        "- Ev moduna geçmek istiyorsa: MOD_EV. Sokak moduna: MOD_SOKAK\n"
        "- Kısa konuşmamı istiyorsa: AYAR_KISA. Detaylı: AYAR_DETAYLI. Sessiz: AYAR_SESSIZ\n"
        "- Cihazın durumunu soruyorsa: DURUM\n"
        "- Son söylediğimi tekrar istiyorsa: TEKRAR\n"
        "- Kayıt boş, gürültü ya da anlaşılmıyorsa: ANLASILMADI"
    )


def kare_al(cam, boyut=(480, 360), kalite=60):
    frame = cam.get()
    if frame is None:
        return None, None
    small = cv2.resize(frame, boyut)
    _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, kalite])
    return frame, base64.b64encode(buf).decode("utf-8")


# ───────────────── SENSÖRE DAYALI İNTERNETSİZ CEVAP ─────────────────
def sensorle_cevapla():
    """İnternet ya da yapay zekâ yokken: sensörün ya da kameranın (cihazda çalışır) ölçtüğünü söyler."""
    m, kaynak = en_yakin_mesafe()
    if kaynak is None and kamera_mesafe is not None and kamera_mesafe.karanlik:
        print("🌑 İnternetsiz cevap: kamera karanlık")
        yayinla("gozluk", "(internetsiz) Kamera görüntüsü çok karanlık")
        oynat(guvenlik.ses_yolu("kamera_karanlik"))
        return
    if m is None:
        ad = "engel_yok"
    elif m < 0.75:
        ad = "engel_yarim"
    elif m < 1.25:
        ad = "engel_bir"
    elif m < 1.75:
        ad = "engel_bir_bucuk"
    else:
        ad = "engel_iki"
    print(f"📏 İnternetsiz cevap ({kaynak}): {'menzil dışı' if m is None else f'{m:.2f} m'}")
    yayinla("gozluk", f"(internetsiz, {kaynak}) {'2 metre içinde engel yok' if m is None else f'{m:.1f} m'}")
    oynat(guvenlik.ses_yolu(ad))


# ───────────────── KISA BASIŞ: ÖNÜMDE NE VAR (REST yedeği) ─────────────────
def onumde_ne_var(cam):
    if not internet_var():
        sensorle_cevapla()
        return
    frame, img_b64 = kare_al(cam)
    if frame is None:
        print("⚠️ Görüntü yok")
        sensorle_cevapla()
        return
    payload = {
        "contents": [{"parts": [
            {"text": istem_tarif()},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
        ]}],
        "generationConfig": {"maxOutputTokens": 120, "temperature": 0.2},
    }
    t0 = time.time()
    cevap = _gemini_post(payload, label="TARİF")
    if not cevap:
        print("⚠️ Yapay zekâdan cevap yok, sensörle cevaplanıyor")
        sensorle_cevapla()
        return
    olcum("REST cevap", time.time() - t0)
    konus(cevap.replace('"', "").strip())


# ───────────────── UZUN BASIŞ: SORU (REST yedeği) ─────────────────
GEMINI_KOMUTLARI = {
    "AYAR_KISA": DUZEY_KISA,
    "AYAR_DETAYLI": DUZEY_DETAYLI,
    "AYAR_SESSIZ": DUZEY_SESSIZ,
    "DURUM": sesli_komut.KOMUT_DURUM,
    "TEKRAR": sesli_komut.KOMUT_TEKRAR,
    "MOD_EV": sesli_komut.KOMUT_EV,
    "MOD_SOKAK": sesli_komut.KOMUT_SOKAK,
}


def soruyu_isle(cam, wav_yolu):
    # 1. Önce internetsiz: komut mu?
    komut, metin = komut_tanici.tani(wav_yolu, bekle_s=5)
    if metin:
        yayinla("kullanici", metin)
    if komut is not None:
        ayari_uygula(komut)
        return

    # 2. Soru: yapay zekâ gerekir
    if not internet_var():
        oynat(guvenlik.ses_yolu("internet_gerekli"))
        return
    _, img_b64 = kare_al(cam)
    with open(wav_yolu, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    parts = [{"text": istem_soru()}]
    if img_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})
    parts.append({"inline_data": {"mime_type": "audio/wav", "data": audio_b64}})
    t0 = time.time()
    cevap = _gemini_post({"contents": [{"parts": parts}],
                          "generationConfig": {"maxOutputTokens": 250, "temperature": 0.3}},
                         label="SORU")
    if not cevap:
        oynat(guvenlik.ses_yolu("ai_yok"))
        return
    olcum("REST cevap", time.time() - t0)

    # 3. Gemini özel bir durum mu bildirdi?
    temiz = cevap.strip().strip(".'\"").strip()
    ust = temiz.upper()
    if ust in GEMINI_KOMUTLARI:
        print(f"  🔁 Gemini komut tanıdı: {ust}")
        ayari_uygula(GEMINI_KOMUTLARI[ust])
    elif ust.startswith("ANLASILMADI") or ust.startswith("ANLAŞILMADI"):
        oynat(guvenlik.ses_yolu("anlamadim"))
    elif ust.startswith("NAVIGASYON"):
        konus(navigator.baslat(temiz.split(":", 1)[1].strip() if ":" in temiz else ""))
    elif ust.startswith("NEREDEYIM"):
        konus(navigator.neredeyim())
    elif ust.startswith("NEREDE"):
        konus(esya_hafizasi.sorgula(temiz.split(":", 1)[1] if ":" in temiz else ""))
    else:
        konus(temiz)


# ───────────────── ÇİFT BASIŞ: TEKRAR ─────────────────
def tekrarla():
    if son_ses and os.path.exists(son_ses):
        print("🔁 Tekrar")
        ses_yoneticisi.normal_cal(son_ses)
    else:
        ses_yoneticisi.normal_cal(guvenlik.ses_yolu("tekrar_yok"))


# ───────────────── GEMINI LIVE ─────────────────
AI_MOTORU = (os.getenv("AI_MOTORU") or "live").strip().lower()
canli = None        # kullanıcıyla konuşan Live oturumu (main() içinde kurulur)
canli_gozcu = None  # arka planda sessizce bakan ikinci oturum (sokak olayları, eşya hafızası)
cam = None
nesne_tanima = None


def _mesafe_metni(m):
    return "2 metre içinde engel yok" if m is None else f"{m:.1f} metre".replace(".", ",")


def arac_mesafe_olc():
    sonuc = {}
    if not guvenlik_sistemi.sensor_kapali and not guvenlik_sistemi.sensor_thread.arizali:
        sonuc["lazer_sensor_tam_on"] = _mesafe_metni(guvenlik_sistemi.son_mesafe) + " (kesin)"
    o = kamera_mesafe.guncel() if kamera_mesafe else None
    if o is not None:
        sonuc["kamera_tam_on"] = f"yaklaşık {_mesafe_metni(o['tam_on'])}"
        sonuc["kamera_nesneler"] = [f"{n['ad']}: saat {n['saat']} yönünde, yaklaşık {_mesafe_metni(n['mesafe'])}"
                                    for n in o["nesneler"][:6]]
    return sonuc or {"sonuc": "Şu an mesafe ölçülemiyor."}


def arac_ayar_degistir(duzey):
    if duzey not in (DUZEY_KISA, DUZEY_DETAYLI, DUZEY_SESSIZ):
        return {"hata": "duzey kisa, detayli ya da sessiz olmalı"}
    ayarlar["konusma_duzeyi"] = duzey
    ayarlari_kaydet()
    print(f"⚙️  Konuşma düzeyi: {duzey}")
    yayinla("durum", durum_ozeti())
    return {"sonuc": f"Konuşma düzeyi {duzey} olarak ayarlandı."}


def arac_anlatim_sikligi(yon):
    yeni = siklik_degistir(yon)
    return {"sonuc": f"Anlatım sıklığı: {yeni}, en fazla {anlatim.aralik:.0f} saniyede bir yer bildirilecek."}


def arac_mod_degistir(mod):
    mod_degistir(MOD_EV if mod == "ev" else MOD_SOKAK, sesli_onay=False)
    return {"sonuc": f"{mod} moduna geçildi."}


def arac_durum_bildir():
    return {"internet": "var" if internet_var() else "yok",
            "mesafe_sensoru": "çalışmıyor" if guvenlik_sistemi.sensor_thread.arizali else "çalışıyor",
            "mod": mod(), "konusma_duzeyi": ayarlar.get("konusma_duzeyi"),
            "navigasyon": "etkin" if navigator and navigator.aktif else "yok",
            "telefon_gps": "geliyor" if navigator and navigator.gps_var else "yok"}


def arac_esya_kaydet(esya, konum):
    kaydedildi = esya_hafizasi.kaydet(esya, konum, "gemini")
    return {"sonuc": "kaydedildi" if kaydedildi else "kaydedilmedi"}


def arac_esya_nerede(esya):
    return {"cevap": esya_hafizasi.sorgula(esya)}


def arac_bildir(tur, metin):
    """Sokak gözcüsünün bildirimi: orta yol kurallarına göre söylenir ya da susulur."""
    if mod() != MOD_SOKAK:
        return {"sonuc": "ev modunda bildirim yok"}
    soylenecek = anlatim.karar(tur, metin)
    yayinla("olay", f"{'🔊' if soylenecek else '🔇'} [{tur}] {metin}")
    if soylenecek:
        print(f"👁️  {tur}: {metin}")
        if canli is not None:
            canli.soyle(metin)
    return {"sonuc": "tamam"}


_son_tarama = 0.0


def arac_cevreyi_tara():
    """Kullanıcıya başını çevirmesini söyler, 5 kare toplar, Live'a anlattırır."""
    global _son_tarama
    # Model bazen fonksiyonu art arda iki kez çağırıyor: ikinci taramayı yapma
    if time.monotonic() - _son_tarama < 15:
        return {"sonuc": "Tarama zaten yapılıyor, tekrar çağırma ve bir şey söyleme."}
    _son_tarama = time.monotonic()
    ses_yoneticisi.normal_cal(guvenlik.ses_yolu("tarama"))
    kareler = []
    for _ in range(5):
        kareler.append(kare_jpeg())
        time.sleep(1.0)
    kareler = [k for k in kareler if k is not None]
    yayinla("olay", f"🔄 Çevre taraması: {len(kareler)} kare")
    if not kareler:
        return {"sonuc": "Kamera görüntüsü alınamadı."}
    return {"sonuc": f"{len(kareler)} kare çekildi: ilki sağda (saat 3), sonuncusu solda (saat 9). "
                     "Gördüklerini saat yönleriyle anlat.", "_resimler": kareler}


def arac_navigasyon_baslat(hedef):
    return {"sonuc": navigator.baslat(hedef)}


def arac_navigasyon_durdur():
    return {"sonuc": navigator.durdur()}


def arac_neredeyim():
    return {"sonuc": navigator.neredeyim()}


def kare_jpeg():
    """Live'a giden kare: 960 × 720, küçük eşyalar seçilebilsin."""
    if cam is None:
        return None
    _, b64 = kare_al(cam, boyut=(960, 720), kalite=75)
    return None if b64 is None else base64.b64decode(b64)


def son_ses_ayarla(yol):
    global son_ses
    son_ses = yol


def canli_kur():
    """
    AI_MOTORU=live ise iki Gemini Live oturumu açar: biri kullanıcıyla konuşur, diğeri
    arka planda kareye bakıp fonksiyonla bildirir. Tek oturumda gözcü kareleri geçmişte
    birikip kullanıcının cevaplarını 5-15 sn geciktiriyordu. Kurulamazsa eski yol kullanılır.
    """
    global canli, canli_gozcu
    if AI_MOTORU != "live":
        print("🤖 Yapay zekâ: REST (AI_MOTORU=rest)")
        return
    try:
        import canli as canli_modulu
        araclar = {"mesafe_olc": arac_mesafe_olc, "ayar_degistir": arac_ayar_degistir,
                   "anlatim_sikligi": arac_anlatim_sikligi, "mod_degistir": arac_mod_degistir,
                   "durum_bildir": arac_durum_bildir, "esya_kaydet": arac_esya_kaydet,
                   "esya_nerede": arac_esya_nerede, "bildir": arac_bildir,
                   "cevreyi_tara": arac_cevreyi_tara, "navigasyon_baslat": arac_navigasyon_baslat,
                   "navigasyon_durdur": arac_navigasyon_durdur, "neredeyim": arac_neredeyim}
        ortak = dict(api_key=API_KEY, ses=ses_yoneticisi, araclar=araclar, kare_al=kare_jpeg,
                     duzey_al=lambda: ayarlar.get("konusma_duzeyi", DUZEY_KISA), mod_al=mod,
                     son_ses_ayarla=son_ses_ayarla, mesafe_bilgisi=mesafe_bilgisi_metni)
        canli = canli_modulu.CanliAsistan(**ortak, yayinla=yayinla, olcum=olcum, gozcu=False, ad="kullanıcı").baslat()
        canli_gozcu = canli_modulu.CanliAsistan(
            **ortak, gozcu=True, ad="gözcü",
            mesgul_al=lambda: canli.dinliyor or canli._tur is not None or bool(canli._bekleyen_turlar)).baslat()
        print(f"🤖 Yapay zekâ: Gemini Live ({canli_modulu.CANLI_MODEL}, kullanıcı + gözcü oturumu), "
              f"bağlı değilken REST")
    except Exception as e:
        print(f"⚠️  Gemini Live başlatılamadı, REST kullanılıyor: {type(e).__name__}: {e}")
        canli = canli_gozcu = None


def canli_hazir():
    return canli is not None and canli.hazir and internet_var()


# ───────────────── JÜRİ PANELİ ─────────────────
def panel_karesi():
    """Panel için: YOLO kutuları ve sensör bilgisi çizilmiş JPEG."""
    if cam is None:
        return None
    kare = cam.get()
    if kare is None:
        return None
    olcek = 640 / kare.shape[1]  # kutular kamera çözünürlüğünde, panel karesi 640 × 480
    kare = cv2.resize(kare, (640, 480))
    if nesne_tanima is not None:
        kare = nesne_tanima.ciz(kare, olcek)
    o = kamera_mesafe.guncel() if kamera_mesafe else None
    if o is not None:
        for n in o["nesneler"]:
            x1, y1, x2, y2 = (int(v * olcek) for v in n["kutu"])
            etiket = f"{n['mesafe']:.1f} m"
            cv2.rectangle(kare, (x1, y2 - 20), (x1 + 62, y2), (20, 20, 20), -1)
            cv2.putText(kare, etiket, (x1 + 4, y2 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 220, 255), 1)
    m, kaynak = en_yakin_mesafe()
    yazi = (f"{'EV' if mod() == MOD_EV else 'SOKAK'} | tam onde: {'>2 m' if m is None else f'{m:.2f} m'}"
            f" ({'lazer' if kaynak == 'sensör' else 'kamera AI' if kaynak else '-'})")
    cv2.rectangle(kare, (0, 0), (640, 26), (0, 0, 0), -1)
    cv2.putText(kare, yazi, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    _, buf = cv2.imencode(".jpg", kare, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


def panel_besleyici():
    """Sensör, YOLO ve "Dur" uyarılarını panele aktarır; "Dur" anındaki TTC'yi ölçüm olarak yazar."""
    son_dur = guvenlik_sistemi.sayac[guvenlik.OLAY_DUR]
    son_yolo = 0.0
    son_derinlik = 0.0
    sayac = 0
    while True:
        try:
            mt = guvenlik_sistemi.mantik
            o = kamera_mesafe.guncel() if kamera_mesafe else None
            yeni_derinlik = o is not None and o["zaman"] != son_derinlik
            if yeni_derinlik:
                son_derinlik = o["zaman"]
                yayinla("mesafe", {k: o[k] for k in ("tam_on", "nesneler", "olcek", "model", "sure_ms")})
            if guvenlik_sistemi.sensor_kapali:
                # Sensör yokken sadece yeni ölçüm gelince gönder (saniyede 5 boş olay göndermesin)
                if yeni_derinlik:
                    yayinla("sensor", {"mesafe": o["tam_on"], "hiz": 0.0, "ttc": None,
                                       "seviye": kamera_mesafe.engel_seviye, "arizali": False,
                                       "kaynak": "kamera (derinlik AI)"})
            else:
                yayinla("sensor", {
                "mesafe": guvenlik_sistemi.son_mesafe, "hiz": mt.hiz,
                "ttc": None if mt.ttc == float("inf") else mt.ttc, "seviye": mt.seviye,
                "arizali": guvenlik_sistemi.sensor_thread.arizali, "kaynak": guvenlik_sistemi.sensor.ad})
            dur = guvenlik_sistemi.sayac[guvenlik.OLAY_DUR]
            if dur != son_dur:
                son_dur = dur
                m = guvenlik_sistemi.son_mesafe
                yayinla("uyari", f"DUR — {'?' if m is None else f'{m:.2f} m'}, {mt.hiz:.2f} m/s")
                if mt.ttc != float("inf"):
                    olcum("Dur anında çarpışmaya kalan", mt.ttc)
            if nesne_tanima is not None and nesne_tanima.son_zaman != son_yolo:
                son_yolo = nesne_tanima.son_zaman
                yayinla("nesneler", nesne_tanima.ozet())
            sayac += 1
            if sayac % 25 == 0:
                yayinla("durum", durum_ozeti())
        except Exception as e:
            print(f"⚠️  Panel besleyici: {type(e).__name__}: {e}")
        time.sleep(0.2)


# ───────────────── DÜĞME OLAYLARI ─────────────────
def kapat():
    print("\nÇıkılıyor...")
    try:
        dugme.kapat()
        guvenlik_sistemi.durdur()
    finally:
        restore_terminal()
        os._exit(0)


def olay_geldi(olay):
    """Düğme/klavye thread'inden çağrılır. Hızlı işler burada, ağır işler ana döngüde."""
    if olay == kontrol.CIKIS:
        kapat()
    elif olay == "sustur":
        if konusmayi_kes():
            print("\n🤫 Susturuldu")
    elif olay == kontrol.KISA:
        # Önce o an süren konuşmayı kes, sonra her zaman sor (önceden sadece susturuyordu:
        # bekleyen navigasyon duyuruları varken soru hiç sorulmuyordu)
        if konusmayi_kes():
            print("\n🤫 Önceki konuşma kesildi")
        if canli_hazir() and canli.onumde_ne_var(guvenlik_sistemi.son_mesafe):
            return
        olay_kuyrugu.put((kontrol.KISA, None))
    elif olay == kontrol.UZUN_BASLADI:
        konusmayi_kes()
        threading.Thread(target=ses_yoneticisi.normal_cal,
                         args=(guvenlik.ses_yolu("ton_dinliyor"),), daemon=True).start()
        onkayit = recorder.start()
        if onkayit is not None and canli_hazir() and canli.dinlemeye_basla(onkayit, guvenlik_sistemi.son_mesafe):
            recorder.dinleyici = canli.ses_parcasi
    elif olay == kontrol.UZUN_BITTI:
        canli_dinledi = recorder.dinleyici is not None
        recorder.dinleyici = None
        yol = recorder.stop()
        # Ses Live'a canlı aktıysa cevabı Live verir. Bağlantı bu arada koptuysa eski yol.
        if canli_dinledi and canli.dinlemeyi_bitir():
            return
        if yol:
            # Bir sonraki kayıt üzerine yazmasın diye kopyala
            kopya = f"/tmp/soru_{time.time():.0f}.wav"
            shutil.copyfile(yol, kopya)
            olay_kuyrugu.put((SORU, kopya))
    elif olay == kontrol.CIFT:
        konusmayi_kes()
        olay_kuyrugu.put((kontrol.CIFT, None))
    elif olay in ("ev", "sokak"):
        olay_kuyrugu.put(("mod", olay))


dugme = kontrol.Dugme(olay_geldi)


def telefon_dugmesi(olay):
    """Telefondaki kumanda düğmeleri (fiziksel düğme gelene kadar). Ağ thread'inden çağrılır."""
    print(f"📱 Telefon: {olay}")
    olay_geldi(olay)  # hızlı döner; uzun basışın başı ve sonu sırası korunsun diye thread'e atılmaz


def keyboard_loop():
    """stdin'den tek tuş okuyup düğme olayına çeviren daemon döngü (h / s: mod)."""
    while True:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if r:
                key = sys.stdin.read(1)
                if key in ("h", "H"):
                    olay_geldi("ev")
                elif key in ("s", "S"):
                    olay_geldi("sokak")
                elif key in ("x", "X"):
                    olay_geldi("sustur")
                elif key:
                    dugme.tus(key)
        except Exception as e:
            print(f"keyboard error: {e}")
            time.sleep(0.5)


# ───────────────── MAIN ─────────────────
def main():
    global cam, juri_paneli, navigator, nesne_tanima, kamera_mesafe, kulaklik_bekcisi
    print("SYSTEM STARTING...")
    cam = Camera().start()
    navigator = navigasyon.Navigasyon(
        duyur=navigasyon_duyur, google_anahtari=GOOGLE_MAPS_API_KEY or None,
        degisti=lambda d: yayinla("nav", d), yedek_konum=TEST_KONUMU)
    juri_paneli = panel.Panel(kare_al=panel_karesi, durum_al=durum_ozeti,
                              konum_geldi=navigator.konum_guncelle,
                              harita_al=lambda: kamera_mesafe.harita_jpeg() if kamera_mesafe else None,
                              dugme_olayi=telefon_dugmesi).baslat()
    if goru.YOLO_ACIK:
        # Ayrı thread başlatılmaz: görü hattı (mesafe.py) her karede önce YOLO'yu, sonra derinliği çalıştırır
        nesne_tanima = goru.NesneTanima(kare_al=cam.get, esya_gordu=yolo_esya_gordu)
    kamera_mesafe = mesafe.KameraMesafe(kare_al=cam.get, mod_al=mod, yolo=nesne_tanima, uyari=engel_uyarisi)
    kamera_mesafe.start()
    kulaklik_bekcisi = kulaklik.KulaklikBekcisi(degisti=kulaklik_degisti)
    kulaklik_bekcisi.start()
    canli_kur()
    recorder.open()  # mikrofon sürekli açık: kısa komutların başı kesilmesin
    dugme.baslat()
    if setup_raw_terminal():
        threading.Thread(target=keyboard_loop, daemon=True).start()
    threading.Thread(target=panel_besleyici, daemon=True, name="panel-besleyici").start()

    time.sleep(2)
    print("\n" + "=" * 60)
    print("SİSTEM HAZIR")
    print("=" * 60)
    print("Düğme: kısa = önümde ne var | uzun = soru sor | çift = tekrar")
    print("Klavye: d = kısa | SPACE = uzun başlat/bitir | r = çift | x = sustur | h = ev | s = sokak | q = çıkış")
    print("Sesle: ev modu, sokak modu, ne görüyorsun, çevreyi tara, anahtarım nerede,")
    print("       ... götür, neredeyim, daha çok/az anlat, kısa konuş, detaylı anlat, sessiz ol")
    print(f"Mod: {mod()} | konuşma düzeyi: {ayarlar.get('konusma_duzeyi')} | anlatıcı: {anlatim.siklik}")
    print("=" * 60 + "\n")
    ses_yoneticisi.normal_cal(guvenlik.ses_yolu("ton_hazir"))

    while True:
        olay, veri = olay_kuyrugu.get()
        try:
            if olay == kontrol.KISA:
                onumde_ne_var(cam)
            elif olay == SORU:
                soruyu_isle(cam, veri)
                try:
                    os.remove(veri)
                except OSError:
                    pass
            elif olay == kontrol.CIFT:
                tekrarla()
            elif olay == "mod":
                mod_degistir(veri)
        except Exception as e:
            print(f"❌ {olay} işlenirken hata: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        kapat()
