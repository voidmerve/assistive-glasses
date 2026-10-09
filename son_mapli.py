"""
Asistif Gözlük
==============

Görme engelli kullanıcı için Türkçe konuşan akıllı gözlük.

YAPAY ZEKÂ:
    Varsayılan: Gemini Live (canli.py). Cevaplar doğrudan ses olarak gelir,
    mikrofon basılı tutulurken canlı akar, mesafe sensörden fonksiyonla alınır.
    Önemli olaylar (aşağı basamak, yaya geçidi, trafik ışığı ...) kendiliğinden
    söylenir, "sessiz ol" denince kapanır.
    Live bağlı değilse (internet yok, kota bitti) otomatik olarak eski yola
    döner: REST istek + edge-tts, o da yoksa sensörle cevap.
    .env: AI_MOTORU=live | rest, OLAY_ARALIGI_S=5 (0 = olay gözcüsü kapalı)

TASARIM İLKELERİ:
    - Varsayılan durum sessizlik. Kulak trafiği ve çevreyi dinlemek için lazım.
    - Engel uyarısını sadece mesafe sensörü verir (guvenlik.py): internetsiz,
      tık ve titreşimle, sadece çarpışmaya doğru yürürken.
    - Yapay zekâ sadece sorulunca konuşur. Tek cümle, en önemli şey önce.
    - Yönler saat sistemiyle söylenir: tam önü saat 12, sağ saat 2-3, sol saat 9-10.

KONTROL (tek düğme, kontrol.py):
    kısa basış → "Önümde ne var?" (konuşma sürüyorsa önce susturur)
    uzun basış → basılı tutarken soru sor, bırakınca cevaplanır
    çift basış → son söyleneni tekrarla
    Düğme takılı değilken klavye: d = kısa, SPACE = uzun başlat/bitir, r = çift, q = çıkış

SESLİ AYARLAR (uzun basışla söylenir, internetsiz anlaşılır):
    "kısa konuş"    → tek cümlelik cevaplar (varsayılan)
    "detaylı anlat" → 2-3 cümle, sol/orta/sağ mesafe tahmini ile
    "sessiz ol"     → sadece sorunca konuşur (kendiliğinden bildirim yok)
    "durum ne"      → internet ve sensör durumu
    "tekrar et"     → son söyleneni tekrarlar
    Bir yere gitmek için: "Beşiktaş çarşısına gitmek istiyorum" (Maps anahtarı gerekir)

İNTERNET YOKKEN:
    Engel uyarıları aynen çalışır. Kısa basış sensörün ölçtüğü mesafeyi söyler.
    Sorular için "Bunun için internet gerekiyor" denir.

ÇALIŞTIRMADAN ÖNCE:
    1. Bluetooth kulaklığın bağlı olduğundan emin ol.
    2. Mikrofon ve hoparlör varsayılan olmalı:
         pactl set-default-source bluez_input.20:23:03:25:87:1C
         pactl set-default-sink bluez_output.20_23_03_25_87_1C.1

API KEY'LERİ (.env):
    GEMINI_API_KEY=...        # zorunlu
    GOOGLE_MAPS_API_KEY=...   # isteğe bağlı, boşsa navigasyon kapalı
    DUGME_PIN=                # fiziksel düğmenin GPIO (BCM) numarası, boşsa klavye
    .env dosyası git'e gönderilmez.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import re
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
from PIL import Image
from queue import Queue
from collections import deque
from picamera2 import Picamera2
from transformers import pipeline
from dotenv import load_dotenv
import guvenlik
import kontrol
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
    print("⚠️  GOOGLE_MAPS_API_KEY boş — navigasyon kapalı, diğer her şey çalışır.")
# Google anahtarı reddederse (REQUEST_DENIED) oturum boyunca navigasyon kapatılır
maps_key_rejected = False

# Not: 2.5-flash-lite multimodal'dır. Eğer ses inputuyla hata verirse
# "gemini-2.5-flash" olarak değiştir.
MODEL_NAME = "gemini-2.5-flash-lite"
MODEL_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
# Key URL'de değil header'da gider — hata mesajlarında URL basılırsa key görünmesin
GEMINI_HEADERS = {"x-goog-api-key": API_KEY}

HTTP_TIMEOUT = 25
DEPTH_CALIBRATION = 1.0

# ───────────────── MAPS CONFIG ─────────────────


# Şimdilik sabit test konumu — telefon GPS entegre edilince değişecek
# Format: (lat, lng) — Denizli'de sabit test konumu
CURRENT_LOCATION = (37.73831482466314, 29.10723965422727)

# ───────────────── AYARLAR (konuşma düzeyi) ─────────────────
AYAR_DOSYASI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ayarlar.json")
DUZEY_KISA = sesli_komut.KOMUT_KISA
DUZEY_DETAYLI = sesli_komut.KOMUT_DETAYLI
DUZEY_SESSIZ = sesli_komut.KOMUT_SESSIZ


def ayarlari_yukle():
    try:
        with open(AYAR_DOSYASI) as f:
            return json.load(f)
    except Exception:
        return {"konusma_duzeyi": DUZEY_KISA}


def ayarlari_kaydet():
    try:
        with open(AYAR_DOSYASI, "w") as f:
            json.dump(ayarlar, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️  Ayarlar kaydedilemedi: {e}")


ayarlar = ayarlari_yukle()

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

# ───────────────── DEPTH MODEL ─────────────────
DEPTH_MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"


def load_depth_model():
    """
    Önce diskteki önbellekten yükler (internet gerekmez). Önbellekte yoksa
    internetten indirir. İkisi de olmazsa None döner: program çökmez, sadece
    mesafe tahmini olmadan çalışır.
    """
    from huggingface_hub import snapshot_download
    try:
        local_dir = snapshot_download(DEPTH_MODEL_ID, local_files_only=True)
        return pipeline(task="depth-estimation", model=local_dir, device=-1)
    except Exception as e:
        print(f"  Önbellekten yüklenemedi ({type(e).__name__}), internetten deneniyor...")
    try:
        return pipeline(task="depth-estimation", model=DEPTH_MODEL_ID, device=-1)
    except Exception as e:
        print(f"⚠️  DEPTH MODELİ YÜKLENEMEDİ, mesafe tahmini kapalı: {type(e).__name__}: {e}")
        return None


print("DEPTH MODELİ YÜKLENİYOR...")
depth_pipe = load_depth_model()
if depth_pipe is not None:
    print("DEPTH MODELİ HAZIR")


# ───────────────── CAMERA ─────────────────
class Camera:
    def __init__(self):
        self.picam2 = Picamera2()
        self.picam2.configure(self.picam2.create_preview_configuration(
            main={"format": "RGB888", "size": (640, 480)}
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


# ───────────────── DEPTH ESTIMATION ─────────────────
def estimate_depth(frame_rgb):
    if depth_pipe is None:
        return None
    try:
        t0 = time.time()
        small = cv2.resize(frame_rgb, (256, 192))
        pil_img = Image.fromarray(small)
        result = depth_pipe(pil_img)
        if "predicted_depth" in result:
            depth_map = np.array(result["predicted_depth"])
        else:
            depth_map = np.array(result["depth"])
        elapsed = time.time() - t0
        print(f"  🔍 Depth: {elapsed:.1f}s")

        h, w = depth_map.shape
        top_cut = int(h * 0.30)
        bottom_cut = int(h * 0.65)
        roi = depth_map[top_cut:bottom_cut, :]

        def median_of_closest(region, percent=0.30):
            flat = region.flatten()
            flat = flat[flat > 0.1]
            flat = flat[flat < 15.0]
            if len(flat) == 0:
                return 5.0
            s = np.sort(flat)
            cutoff = max(1, int(len(s) * percent))
            return float(np.median(s[:cutoff]))

        left = median_of_closest(roi[:, :w // 3]) * DEPTH_CALIBRATION
        center = median_of_closest(roi[:, w // 3:2 * w // 3]) * DEPTH_CALIBRATION
        right = median_of_closest(roi[:, 2 * w // 3:]) * DEPTH_CALIBRATION
        return {"left": left, "center": center, "right": right}
    except Exception as e:
        print(f"DEPTH ERROR: {type(e).__name__}: {e}")
        return None


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


# ───────────────── KONUŞMA ─────────────────
# Her konuşmanın bir "nesli" var. Kısa basışla susturulunca nesil artar ve
# henüz sentezlenmekte olan cümle de çalınmadan atılır.
_konusma_nesli = 0
_konusma_kilidi = threading.Lock()
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
    return kesildi or hazirlaniyor or canli_kesildi


_konusma_hazirlaniyor = threading.Event()


def oynat(yol):
    """Önceden kaydedilmiş bir sesi çalar (internetsiz) ve tekrar için hatırlar."""
    global son_ses
    son_ses = yol
    ses_yoneticisi.normal_cal(yol)


def konus(text):
    """Metni edge-tts ile seslendirir (internet gerekir), bitene ya da kesilene kadar bekler."""
    global son_ses
    print(f"📢 {text}")
    with _konusma_kilidi:
        nesil = _konusma_nesli
    _konusma_hazirlaniyor.set()
    try:
        f = "/tmp/tts.mp3"
        asyncio.run(edge_tts.Communicate(text, "tr-TR-EmelNeural").save(f))
    except Exception as e:
        print(f"TTS ERROR: {type(e).__name__}: {e}")
        return
    finally:
        _konusma_hazirlaniyor.clear()
    if nesil != _konusma_nesli:
        print("🔇 Susturuldu")
        return
    shutil.copyfile(f, SON_KONUSMA)
    son_ses = SON_KONUSMA
    if not ses_yoneticisi.normal_cal(f):
        print("🔇 Konuşma kesildi")


def internet_var():
    # İzleyici henüz ilk kontrolü yapmadıysa (None) var sayıp denenir
    return guvenlik_sistemi.internet_var is not False


# ───────────────── GEMINI İSTEMLERİ ─────────────────
YON_KURALI = (
    "Yönleri saat sistemiyle söyle: görüntünün ortası saat 12, sol kenarı saat 10, "
    "sağ kenarı saat 2. Örnek: 'Saat 1 yönünde kapı.' "
)


def _duzey_kurali():
    if ayarlar.get("konusma_duzeyi") == DUZEY_DETAYLI:
        return "En fazla 3 kısa cümle kullan. "
    return "Tek cümle, en fazla 12 kelime. "


def _mesafe_bilgisi(sensor_m, derinlik):
    parcalar = []
    if sensor_m is not None:
        parcalar.append(
            f"Lazer sensör tam öndeki (saat 12) nesneye {sensor_m:.1f} metre ölçtü, bu kesin bilgidir. ")
    else:
        parcalar.append("Lazer sensör tam önde 2 metre içinde bir şey ölçmedi. ")
    if derinlik:
        parcalar.append(
            f"Kamera derinlik tahmini (yaklaşık): sol {derinlik['left']:.1f} m, "
            f"orta {derinlik['center']:.1f} m, sağ {derinlik['right']:.1f} m. ")
    parcalar.append("Bunlar dışında mesafe uydurma. Mesafe söylerken ondalığı virgülle yaz (1,5 metre). ")
    return "".join(parcalar)


def istem_tarif(sensor_m, derinlik):
    return (
        "Sen görme engelli bir kullanıcının gözlüğüsün. Kullanıcı 'önümde ne var' diye sordu. "
        "Türkçe cevap ver. " + _duzey_kurali() +
        "En önemli şeyi önce söyle: yoldaki engel, basamak, kapı, insan, araç. "
        "Önemsiz eşyaları sayma. " + YON_KURALI + _mesafe_bilgisi(sensor_m, derinlik) +
        "Görüntü karanlık, bulanık ya da kapalıysa sadece bunu söyle. "
        "Selamlama ve giriş cümlesi yok."
    )


def istem_soru(sensor_m):
    return (
        "Sen görme engelli bir kullanıcının gözlüğüsün. Ses kaydında kullanıcının sorusu, "
        "görüntüde önündeki sahne var. Türkçe, net ve doğrudan cevap ver. " + _duzey_kurali() +
        YON_KURALI + _mesafe_bilgisi(sensor_m, None) +
        "Yazı, etiket, ilaç kutusu ya da para soruluyorsa okuyup söyle. "
        "Selamlama ve giriş cümlesi yok.\n"
        "Özel durumlar, bunlarda SADECE belirtilen kelimeyi yaz:\n"
        "- Kullanıcı bir yere gitmek istiyorsa: NAVIGASYON: <yer adı>\n"
        "- Kısa konuşmamı istiyorsa: AYAR_KISA\n"
        "- Detaylı anlatmamı istiyorsa: AYAR_DETAYLI\n"
        "- Sessiz olmamı / sadece sorunca konuşmamı istiyorsa: AYAR_SESSIZ\n"
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
    """İnternet ya da yapay zekâ yokken: sadece lazer sensörün ölçtüğünü söyler."""
    m = guvenlik_sistemi.son_mesafe
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
    print(f"📏 Sensörle cevap: {'menzil dışı' if m is None else f'{m:.2f} m'}")
    oynat(guvenlik.ses_yolu(ad))


# ───────────────── KISA BASIŞ: ÖNÜMDE NE VAR ─────────────────
def onumde_ne_var(cam):
    if not internet_var():
        sensorle_cevapla()
        return
    frame, img_b64 = kare_al(cam)
    if frame is None:
        print("⚠️ Görüntü yok")
        sensorle_cevapla()
        return
    sensor_m = guvenlik_sistemi.son_mesafe
    # Derinlik tahmini ~5 sn sürüyor, sadece detaylı düzeyde
    derinlik = estimate_depth(frame) if ayarlar.get("konusma_duzeyi") == DUZEY_DETAYLI else None
    payload = {
        "contents": [{"parts": [
            {"text": istem_tarif(sensor_m, derinlik)},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
        ]}],
        "generationConfig": {"maxOutputTokens": 120, "temperature": 0.2},
    }
    cevap = _gemini_post(payload, label="TARİF")
    if not cevap:
        print("⚠️ Yapay zekâdan cevap yok, sensörle cevaplanıyor")
        sensorle_cevapla()
        return
    konus(cevap.replace('"', "").strip())


# ───────────────── UZUN BASIŞ: SORU ─────────────────
def ayari_uygula(komut):
    """Sesli ayar komutunu uygular (internetsiz)."""
    if komut in (DUZEY_KISA, DUZEY_DETAYLI, DUZEY_SESSIZ):
        ayarlar["konusma_duzeyi"] = komut
        ayarlari_kaydet()
        print(f"⚙️  Konuşma düzeyi: {komut}")
        if canli is not None:
            canli.ayar_bildir(komut)
        oynat(guvenlik.ses_yolu(f"duzey_{komut}"))
    elif komut == sesli_komut.KOMUT_DURUM:
        guvenlik_sistemi.durum_soyle()
    elif komut == sesli_komut.KOMUT_TEKRAR:
        tekrarla()


GEMINI_KOMUTLARI = {
    "AYAR_KISA": DUZEY_KISA,
    "AYAR_DETAYLI": DUZEY_DETAYLI,
    "AYAR_SESSIZ": DUZEY_SESSIZ,
    "DURUM": sesli_komut.KOMUT_DURUM,
    "TEKRAR": sesli_komut.KOMUT_TEKRAR,
}


def soruyu_isle(cam, wav_yolu):
    # 1. Önce internetsiz: ayar komutu mu?
    komut, _ = komut_tanici.tani(wav_yolu, bekle_s=5)
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
    parts = [{"text": istem_soru(guvenlik_sistemi.son_mesafe)}]
    if img_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})
    parts.append({"inline_data": {"mime_type": "audio/wav", "data": audio_b64}})
    cevap = _gemini_post({"contents": [{"parts": parts}],
                          "generationConfig": {"maxOutputTokens": 200, "temperature": 0.3}},
                         label="SORU")
    if not cevap:
        oynat(guvenlik.ses_yolu("ai_yok"))
        return

    # 3. Gemini özel bir durum mu bildirdi?
    temiz = cevap.strip().strip(".'\"").strip()
    ust = temiz.upper()
    if ust in GEMINI_KOMUTLARI:
        print(f"  🔁 Gemini komut tanıdı: {ust}")
        ayari_uygula(GEMINI_KOMUTLARI[ust])
    elif ust.startswith("ANLASILMADI") or ust.startswith("ANLAŞILMADI"):
        oynat(guvenlik.ses_yolu("anlamadim"))
    elif ust.startswith("NAVIGASYON"):
        navigasyon(temiz.split(":", 1)[1].strip() if ":" in temiz else "")
    else:
        konus(temiz)


# ───────────────── ÇİFT BASIŞ: TEKRAR ─────────────────
def tekrarla():
    if son_ses and os.path.exists(son_ses):
        print("🔁 Tekrar")
        ses_yoneticisi.normal_cal(son_ses)
    else:
        ses_yoneticisi.normal_cal(guvenlik.ses_yolu("tekrar_yok"))


# ───────────────── MAPS ─────────────────
def get_current_location():
    """Mevcut konumu döner. TODO: telefon GPS entegre edilecek."""
    return CURRENT_LOCATION


def _strip_html(text):
    """Google Directions HTML döndürür, sadece düz metin bırak."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "ve")
                .replace("&#39;", "'")
                .replace("&quot;", '"'))
    return text


def geocode(query):
    """Yer adı → ((lat, lng), formatted_address). Hata olursa (None, None)."""
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "address": query,
            "key": GOOGLE_MAPS_API_KEY,
            "language": "tr",
            "region": "tr",
        }
        t0 = time.time()
        r = requests.get(url, params=params, timeout=10)
        elapsed = time.time() - t0
        print(f"  🌐 Geocode: {elapsed:.1f}s - {r.status_code}")
        if r.status_code != 200:
            print(f"  ❌ HATA: {r.text[:200]}")
            return None, None
        data = r.json()
        if data.get("status") != "OK":
            print(f"  ❌ Geocoding: {data.get('status')} - {data.get('error_message', '')}")
            if data.get("status") == "REQUEST_DENIED":
                global maps_key_rejected
                maps_key_rejected = True
            return None, None
        results = data.get("results", [])
        if not results:
            return None, None
        loc = results[0]["geometry"]["location"]
        formatted = results[0].get("formatted_address", query)
        return (loc["lat"], loc["lng"]), formatted
    except Exception as e:
        print(f"GEOCODING ERROR: {type(e).__name__}: {e}")
        return None, None


def get_directions(origin, destination, mode="walking"):
    """Rotayı döner: total_distance, total_duration, steps, step_count."""
    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            "mode": mode,
            "language": "tr",
            "key": GOOGLE_MAPS_API_KEY,
        }
        t0 = time.time()
        r = requests.get(url, params=params, timeout=15)
        elapsed = time.time() - t0
        print(f"  🗺️  Directions: {elapsed:.1f}s - {r.status_code}")
        if r.status_code != 200:
            print(f"  ❌ HATA: {r.text[:200]}")
            return None
        data = r.json()
        if data.get("status") != "OK":
            print(f"  ❌ Directions: {data.get('status')} - {data.get('error_message', '')}")
            return None
        routes = data.get("routes", [])
        if not routes:
            return None
        leg = routes[0]["legs"][0]
        steps = []
        for s in leg.get("steps", []):
            steps.append({
                "instruction": _strip_html(s.get("html_instructions", "")),
                "distance": s.get("distance", {}).get("text", ""),
                "duration": s.get("duration", {}).get("text", ""),
                "maneuver": s.get("maneuver", ""),
            })
        return {
            "total_distance": leg.get("distance", {}).get("text", ""),
            "total_duration": leg.get("duration", {}).get("text", ""),
            "steps": steps,
            "step_count": len(steps),
        }
    except Exception as e:
        print(f"DIRECTIONS ERROR: {type(e).__name__}: {e}")
        return None


def navigasyon_metni(hedef):
    """Hedef → geocode → yürüyüş rotası → söylenecek metin."""
    print(f"\n📍 Navigasyon: '{hedef}'")
    if not GOOGLE_MAPS_API_KEY or maps_key_rejected:
        return "Navigasyon şu an kullanılamıyor."
    if not hedef:
        return "Nereye gitmek istediğini anlayamadım."
    dest_coords, dest_formatted = geocode(hedef)
    if not dest_coords:
        if maps_key_rejected:
            print("  ❌ Maps anahtarı geçersiz, navigasyon bu oturumda kapatıldı")
            return "Navigasyon şu an kullanılamıyor."
        return f"{hedef} adresi bulunamadı."
    print(f"  ✓ {dest_formatted} → {dest_coords}")
    route = get_directions(get_current_location(), dest_coords)
    if not route:
        return "Rota bulunamadı."
    ozet = (f"Hedef {dest_formatted}. Toplam {route['total_distance']}, "
            f"yaklaşık {route['total_duration']} yürüyüş.")
    if route["steps"]:
        ilk = route["steps"][0]
        ozet += f" İlk adım: {ilk['instruction']}, {ilk['distance']}."
    return ozet


def navigasyon(hedef):
    konus(navigasyon_metni(hedef))


# ───────────────── GEMINI LIVE ─────────────────
AI_MOTORU = (os.getenv("AI_MOTORU") or "live").strip().lower()
canli = None   # main() içinde kamera açıldıktan sonra kurulur
cam = None


def _mesafe_metni(m):
    return "2 metre içinde engel yok" if m is None else f"{m:.1f} metre".replace(".", ",")


def arac_mesafe_olc():
    if guvenlik_sistemi.sensor_thread.arizali:
        return {"sonuc": "Mesafe sensörü şu an çalışmıyor, mesafe bilinmiyor."}
    return {"tam_on": _mesafe_metni(guvenlik_sistemi.son_mesafe)}


def arac_ayar_degistir(duzey):
    if duzey not in (DUZEY_KISA, DUZEY_DETAYLI, DUZEY_SESSIZ):
        return {"hata": "duzey kisa, detayli ya da sessiz olmalı"}
    ayarlar["konusma_duzeyi"] = duzey
    ayarlari_kaydet()
    print(f"⚙️  Konuşma düzeyi: {duzey}")
    return {"sonuc": f"Konuşma düzeyi {duzey} olarak ayarlandı."}


def arac_durum_bildir():
    return {"internet": "var" if internet_var() else "yok",
            "mesafe_sensoru": "çalışmıyor" if guvenlik_sistemi.sensor_thread.arizali else "çalışıyor",
            "konusma_duzeyi": ayarlar.get("konusma_duzeyi")}


def arac_navigasyon_baslat(hedef):
    return {"sonuc": navigasyon_metni(hedef)}


def kare_jpeg():
    if cam is None:
        return None
    _, b64 = kare_al(cam)
    return None if b64 is None else base64.b64decode(b64)


def son_ses_ayarla(yol):
    global son_ses
    son_ses = yol


def canli_kur():
    """AI_MOTORU=live ise Gemini Live'ı başlatır. Kurulamazsa eski yol kullanılır."""
    global canli
    if AI_MOTORU != "live":
        print("🤖 Yapay zekâ: REST (AI_MOTORU=rest)")
        return
    try:
        import canli as canli_modulu
        canli = canli_modulu.CanliAsistan(
            api_key=API_KEY, ses=ses_yoneticisi,
            araclar={"mesafe_olc": arac_mesafe_olc, "ayar_degistir": arac_ayar_degistir,
                     "durum_bildir": arac_durum_bildir, "navigasyon_baslat": arac_navigasyon_baslat},
            kare_al=kare_jpeg, duzey_al=lambda: ayarlar.get("konusma_duzeyi", DUZEY_KISA),
            son_ses_ayarla=son_ses_ayarla).baslat()
        print(f"🤖 Yapay zekâ: Gemini Live ({canli_modulu.CANLI_MODEL}), bağlı değilken REST")
    except Exception as e:
        print(f"⚠️  Gemini Live başlatılamadı, REST kullanılıyor: {type(e).__name__}: {e}")
        canli = None


def canli_hazir():
    return canli is not None and canli.hazir and internet_var()


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
    elif olay == kontrol.KISA:
        # Konuşma sürüyorsa kısa basış önce susturur
        if konusmayi_kes():
            print("\n🤫 Susturuldu")
            return
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


dugme = kontrol.Dugme(olay_geldi)


def keyboard_loop():
    """stdin'den tek tuş okuyup düğme olayına çeviren daemon döngü."""
    while True:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if r:
                key = sys.stdin.read(1)
                if key:
                    dugme.tus(key)
        except Exception as e:
            print(f"keyboard error: {e}")
            time.sleep(0.5)


# ───────────────── MAIN ─────────────────
def main():
    print("SYSTEM STARTING...")
    global cam
    cam = Camera().start()
    canli_kur()
    recorder.open()  # mikrofon sürekli açık: kısa komutların başı kesilmesin
    dugme.baslat()
    if setup_raw_terminal():
        threading.Thread(target=keyboard_loop, daemon=True).start()

    time.sleep(2)
    print("\n" + "=" * 50)
    print("SİSTEM HAZIR")
    print("=" * 50)
    print("Düğme: kısa = önümde ne var | uzun = soru sor | çift = tekrar")
    print("Klavye: d = kısa | SPACE = uzun başlat/bitir | r = çift | q = çıkış")
    print("Sesli ayar (uzun basışla): kısa konuş, detaylı anlat, sessiz ol, durum ne, tekrar et")
    print(f"Konuşma düzeyi: {ayarlar.get('konusma_duzeyi')}")
    print("=" * 50 + "\n")
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
        except Exception as e:
            print(f"❌ {olay} işlenirken hata: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        kapat()
