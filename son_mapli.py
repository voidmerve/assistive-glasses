"""
Asistif Gözlük - Ev Modu + Sokak Modu
=====================================

KURULUM (ilk seferde):
    sudo apt install libportaudio2
    pip install sounddevice
    pip install python-dotenv

ÇALIŞTIRMADAN ÖNCE:
    1. Bluetooth kulaklığın bağlı olduğundan emin ol.
    2. Mikrofonu default source yap:
         pactl set-default-source bluez_input.20:23:03:25:87:1C
       (MAC adresini kendi cihazınkiyle değiştir)
    3. Hoparlörü default sink yap (genelde otomatik olur):
         pactl set-default-sink bluez_output.20:23:03:25:87:1C.a2dp-sink

KONTROLLER (SSH üzerinden, Enter'a gerek yok — tek tuş):
    h     → EV MODU         (SPACE = sesli soru sor)
    s     → SOKAK MODU      (sürekli analiz + SPACE = navigasyon)
    q     → çıkış

SESLİ MOD DEĞİŞİMİ (internetsiz, vosk):
    SPACE bas → "ev modu" / "eve geç" ya da "sokak modu" / "dışarı çık" → SPACE bas.
    Kayıt önce yerelde yazıya çevrilir. Mod komutuysa mod değişir ve Gemini'ye
    gönderilmez. Komut değilse eskisi gibi Gemini'ye gider. h/s tuşları yedektir.

EV MODU:
    SPACE bas → konuş → SPACE bas → görüntü + ses Gemini'ye gider, cevap TTS.

SOKAK MODU:
    Arka planda sürekli engel/yön bildirimi yapar.
    SPACE bas → "Beşiktaş çarşısına gitmek istiyorum" → SPACE bas →
    Gemini hedefi çıkarır, Google Maps'ten rota alınır, TTS ile söylenir.

API KEY'LERİ:
    Anahtarlar kodda değil, bu dosyayla aynı klasördeki .env dosyasında durur:
        GEMINI_API_KEY=...        # Gemini key
        GOOGLE_MAPS_API_KEY=...   # Maps key
    İlk kurulumda .env.example dosyasını .env adıyla kopyalayıp değerleri doldur.
    Yeni key alınca sadece .env dosyasını değiştir, koda dokunmaya gerek yok.
    .env dosyası .gitignore'dadır, git'e gönderilmez.
    Gemini anahtarı eksikse program hata mesajı verip durur.
    Maps anahtarı boş bırakılabilir, o zaman sadece navigasyon kapalı olur.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import re
import sys
import cv2
import time
import tty
import wave
import select
import atexit
import base64
import termios
import requests
import threading
import asyncio
import edge_tts
import subprocess
import numpy as np
import sounddevice as sd
import torch
from PIL import Image
from queue import Queue
from picamera2 import Picamera2
from transformers import pipeline
from dotenv import load_dotenv
import guvenlik
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

# Not: 2.5-flash-lite multimodal'dır. Eğer ses inputuyla hata verirse
# "gemini-2.5-flash" olarak değiştir.
MODEL_NAME = "gemini-2.5-flash-lite"
MODEL_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
# Key URL'de değil header'da gider — hata mesajlarında URL basılırsa key görünmesin
GEMINI_HEADERS = {"x-goog-api-key": API_KEY}

HTTP_TIMEOUT = 25
RATE_LIMIT_DELAY = 2
DEPTH_CALIBRATION = 1.0

# ───────────────── MAPS CONFIG ─────────────────


# Şimdilik sabit test konumu — telefon GPS entegre edilince değişecek
# Format: (lat, lng) — Denizli'de sabit test konumu
CURRENT_LOCATION = (37.73831482466314, 29.10723965422727)

# ───────────────── MOD STATE ─────────────────
MODE_HOME = "home"
MODE_STREET = "street"
current_mode = MODE_STREET  # başlangıç
mode_lock = threading.Lock()

# ───────────────── TTS QUEUE ─────────────────
tts_queue = Queue(maxsize=1)
tts_busy = threading.Event()

# ───────────────── GÜVENLİK KATMANI ─────────────────
# Mesafe sensörü + engel uyarıları + internet izleyici. İnternetten ve yapay
# zekâdan bağımsız kendi thread'lerinde çalışır. Derinlik modeli yüklenmeden
# önce başlatılır ki yükleme sırasında da uyarılar çalışsın.
# Sensör seçimi .env içindeki SENSOR_MODU ile yapılır (simulasyon / gercek).
# Tüm sesler (TTS dahil) bu ses yöneticisinden geçer: engel uyarıları acil
# önceliklidir ve o an çalan normal konuşmayı keser.
ses_yoneticisi = guvenlik.SesYoneticisi()
guvenlik_sistemi = guvenlik.GuvenlikSistemi(ses=ses_yoneticisi).baslat()

# Sesli mod komutları için Türkçe vosk modeli, açılışı geciktirmemek için arka planda yüklenir
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
    """Push-to-talk: start() kayda başlar, stop() durdurup WAV kaydeder."""

    def __init__(self, samplerate=16000, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self.frames = []
        self.recording = False
        self.stream = None
        self.lock = threading.Lock()

    def _callback(self, indata, frame_count, time_info, status):
        if self.recording:
            self.frames.append(indata.copy())

    def start(self):
        with self.lock:
            if self.recording:
                return
            self.frames = []
            self.recording = True
            try:
                self.stream = sd.InputStream(
                    samplerate=self.samplerate,
                    channels=self.channels,
                    dtype='int16',
                    callback=self._callback,
                )
                self.stream.start()
                print("\n🎙️  Kayıt başladı (SPACE'e tekrar bas → AI'ya gönder)...")
            except Exception as e:
                print(f"❌ Kayıt başlatılamadı: {e}")
                self.recording = False
                self.stream = None


    def stop(self, path="/tmp/prompt.wav"):
        with self.lock:
            if not self.recording:
                return None
            self.recording = False
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

            if not self.frames:
                print("⚠️  Boş kayıt")
                return None

            audio = np.concatenate(self.frames, axis=0)
            dur = len(audio) / self.samplerate

            if dur < 0.3:
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
voice_ready = threading.Event()         # ev modu için
street_voice_ready = threading.Event()  # sokak modu (navigasyon) için


# ───────────────── KEYBOARD (SSH/terminal) ─────────────────
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


def handle_key(key):
    global current_mode
    if key == 'h':
        with mode_lock:
            if current_mode != MODE_HOME:
                current_mode = MODE_HOME
                print("\n🏠 EV MODU — SPACE: kayıt başlat/durdur")
    elif key == 's':
        with mode_lock:
            if current_mode != MODE_STREET:
                current_mode = MODE_STREET
                print("\n🚶 SOKAK MODU — sürekli analiz")
    elif key == 'q':
        print("\nÇıkılıyor...")
        guvenlik_sistemi.durdur()
        restore_terminal()
        os._exit(0)
    elif key == ' ':
        with mode_lock:
            mode = current_mode
        if mode == MODE_HOME:
            if not recorder.recording:
                recorder.start()
            else:
                path = recorder.stop()
                if path:
                    voice_ready.set()
        elif mode == MODE_STREET:
            if not recorder.recording:
                print("\n🗺️  Navigasyon — hedefi söyleyin (SPACE'e tekrar bas → işle)")
                recorder.start()
            else:
                path = recorder.stop()
                if path:
                    street_voice_ready.set()


def sesli_komut_isle(path="/tmp/prompt.wav"):
    """
    Kaydı internetsiz olarak yazıya çevirir. Mod komutuysa modu değiştirir,
    onay sesini çalar ve True döner (kayıt Gemini'ye gönderilmez).
    Komut değilse ya da model hazır değilse False döner.
    """
    global current_mode
    komut, _ = komut_tanici.tani(path, bekle_s=5)
    if komut is None:
        return False
    yeni = MODE_HOME if komut == sesli_komut.KOMUT_EV else MODE_STREET
    with mode_lock:
        current_mode = yeni
    if yeni == MODE_HOME:
        print("\n🏠 EV MODU (sesli komut) — SPACE: kayıt başlat/durdur")
        ses_yoneticisi.normal_cal(guvenlik.ses_yolu("ev_modu"))
    else:
        print("\n🚶 SOKAK MODU (sesli komut) — sürekli analiz")
        ses_yoneticisi.normal_cal(guvenlik.ses_yolu("sokak_modu"))
    return True


def keyboard_loop():
    """stdin'den tek tuş okuyup handle_key'e veren daemon döngü."""
    while True:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if r:
                key = sys.stdin.read(1)
                if key:
                    handle_key(key)
        except Exception as e:
            print(f"keyboard error: {e}")
            time.sleep(0.5)


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


# ───────────────── GEMINI: SOKAK (sadece görüntü) ─────────────────
def ask_ai_street(img_b64):
    payload = {
        "contents": [{
            "parts": [
                {"text": (
                    "Görüntüde gördüğün en fazla 3 nesneyi yön bilgisiyle birlikte tek cümlede Türkçe söyle. "
                    "Format: 'Önünde X var, sağında Y, solunda Z.' "
                    "Eğer 2 nesne varsa: 'Önünde X var, sağında Y.' "
                    "Eğer 1 nesne varsa: 'Önünde X var.' "
                    "Yön: nesne ortadaysa 'önünde', sağda 'sağında', solda 'solunda'. "
                    "Sadece bu format, başka hiçbir şey yazma."
                )},
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 60, "temperature": 0.1},
    }
    return _gemini_post(payload, label="SOKAK")


# ───────────────── GEMINI: EV (görüntü + ses) ─────────────────
def ask_ai_home(img_b64, audio_b64):
    payload = {
        "contents": [{
            "parts": [
                {"text": (
                    "Sen görme engelli bir kullanıcıya yardım eden bir asistansın. "
                    "Kullanıcının ses kaydındaki sorusunu dinle ve görüntüye bakarak "
                    "Türkçe, kısa (en fazla 2 cümle), net ve doğrudan cevap ver. "
                    "Selamlama, giriş cümlesi veya gereksiz açıklama yapma."
                )},
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 150, "temperature": 0.3},
    }
    return _gemini_post(payload, label="EV")


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
                print(f"     • RATE_LIMIT_DELAY artır (sokak modu döngüsü yavaşlasın)")
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


# ───────────────── PARALEL İŞLEM (sokak modu) ─────────────────
def parallel_process(frame_rgb, img_b64):
    results = {"ai": None, "depth": None}

    def run_ai():
        results["ai"] = ask_ai_street(img_b64)

    def run_depth():
        results["depth"] = estimate_depth(frame_rgb)

    t1 = threading.Thread(target=run_ai)
    t2 = threading.Thread(target=run_depth)
    t1.start(); t2.start()
    t1.join(); t2.join()
    return results["ai"], results["depth"]


def combine_results(ai_text, depth_info):
    if not ai_text:
        return None
    spoken = ai_text.replace('"', '').replace("'", "").strip()
    if not depth_info:
        return spoken

    direction_map = {
        "önünde": depth_info["center"],
        "sağında": depth_info["right"],
        "solunda": depth_info["left"],
    }

    parts = [p.strip() for p in spoken.split(",")]
    new_parts = []
    for part in parts:
        pl = part.lower()
        added = False
        for direction, dist in direction_map.items():
            if direction in pl and dist is not None:
                idx = pl.find(direction)
                rest = part[idx:]
                if rest:
                    rest = rest[0].lower() + rest[1:]
                rest = rest.rstrip(".")
                dist_str = f"{dist:.1f}".replace(".", ",")  # TTS için Türkçe ondalık
                new_parts.append(f"{dist_str} metre {rest}")
                added = True
                break
        if not added:
            new_parts.append(part.rstrip("."))
    return ", ".join(new_parts)


# ───────────────── TTS ─────────────────
async def speak(text):
    try:
        communicate = edge_tts.Communicate(text, "tr-TR-EmelNeural")
        f = "/tmp/tts.mp3"
        await communicate.save(f)
        # Normal öncelik: engel uyarısı gelirse bu konuşma kesilir
        if not ses_yoneticisi.normal_cal(f):
            print("🔇 Konuşma acil uyarı nedeniyle kesildi veya çalınamadı")
    except Exception as e:
        print("TTS ERROR:", e)


def tts_worker():
    while True:
        text = tts_queue.get()
        if not text:
            continue
        tts_busy.set()
        try:
            asyncio.run(speak(text))
        finally:
            tts_busy.clear()


def speak_blocking(text):
    """TTS söyle ve bitene kadar bekle (handler'lar için yardımcı)."""
    print(f"📢 {text}")
    tts_queue.put(text)
    t_start = time.time()
    while not tts_busy.is_set() and time.time() - t_start < 3:
        time.sleep(0.05)
    while tts_busy.is_set():
        time.sleep(0.2)


# ───────────────── MAPS / NAVİGASYON ─────────────────

def get_current_location():
    """Mevcut konumu döner. TODO: telefon GPS entegre edilecek."""
    return CURRENT_LOCATION


def extract_destination_from_audio(audio_b64):
    """Gemini'ye ses gönderir, sadece hedef yer ismini geri alır."""
    payload = {
        "contents": [{
            "parts": [
                {"text": (
                    "Bu ses kaydında kullanıcı bir yere gitmek istediğini söylüyor. "
                    "Sadece gidilmek istenen yerin ismini yaz, başka hiçbir şey yazma. "
                    "Açıklama, cümle, noktalama, fazladan boşluk yok. Sadece yer ismi. "
                    "Örnekler: 'Beşiktaş çarşısına gitmek istiyorum' → 'Beşiktaş Çarşısı'. "
                    "'Taksim meydanına gidicem' → 'Taksim Meydanı'. "
                    "'Sultanahmet camiine götür beni' → 'Sultanahmet Camii'."
                )},
                {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 50, "temperature": 0.0},
    }
    result = _gemini_post(payload, label="HEDEF")
    if result:
        # Gemini bazen tırnak veya noktalama ekliyor, temizle
        result = result.strip().strip('"').strip("'").rstrip('.,!?').strip()
    return result


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


def handle_navigation_request():
    """Sesi al → hedef çıkar → geocode → directions → TTS."""
    print("\n📍 Navigasyon isteği işleniyor...")
    if not GOOGLE_MAPS_API_KEY:
        speak_blocking("Navigasyon şu an kullanılamıyor.")
        return
    try:
        with open("/tmp/prompt.wav", "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    except FileNotFoundError:
        print("⚠️ Ses dosyası yok")
        return

    # 1. Hedefi sesten çıkar
    destination = extract_destination_from_audio(audio_b64)
    if not destination:
        speak_blocking("Hedefi anlayamadım, lütfen tekrar deneyin.")
        return
    print(f"  📍 Anlaşılan hedef: '{destination}'")

    # 2. Geocode
    dest_coords, dest_formatted = geocode(destination)
    if not dest_coords:
        speak_blocking(f"{destination} adresi bulunamadı.")
        return
    print(f"  ✓ {dest_formatted} → {dest_coords}")

    # 3. Mevcut konum
    origin = get_current_location()
    print(f"  📍 Mevcut konum: {origin}")

    # 4. Rotayı al
    route = get_directions(origin, dest_coords)
    if not route:
        speak_blocking("Rota bulunamadı.")
        return

    # 5. Duyur
    summary = (
        f"Hedef {dest_formatted}. "
        f"Toplam mesafe {route['total_distance']}, "
        f"yaklaşık {route['total_duration']} yürüyüş. "
    )
    if route["steps"]:
        first = route["steps"][0]
        summary += f"İlk adım: {first['instruction']}, {first['distance']}."
    speak_blocking(summary)
    print(f"  ✓ Toplam {route['step_count']} adım var (sonraki versiyonda adım adım duyurulacak)")


# ───────────────── EV MODU HANDLER ─────────────────
def handle_home_mode(cam):
    # Voice event'i bekle (timeout ile ki mod değişimini de yakalayalım)
    if not voice_ready.wait(timeout=0.5):
        return
    voice_ready.clear()

    # Kayıt bir mod komutuysa Gemini'ye gönderme
    if sesli_komut_isle():
        return

    frame = cam.get()
    if frame is None:
        print("⚠️ Görüntü yok")
        return

    small = cv2.resize(frame, (320, 240))
    _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
    img_b64 = base64.b64encode(buf).decode("utf-8")

    try:
        with open("/tmp/prompt.wav", "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    except FileNotFoundError:
        print("⚠️ Ses dosyası yok")
        return

    print("🤖 Gemini'ye gönderiliyor (ses + görüntü)...")
    response = ask_ai_home(img_b64, audio_b64)

    if response:
        print(f"📢 {response}")
        tts_queue.put(response)
        t_start = time.time()
        while not tts_busy.is_set() and time.time() - t_start < 3:
            time.sleep(0.05)
        while tts_busy.is_set():
            time.sleep(0.2)
    else:
        print("⚠️ Cevap alınamadı")
        tts_queue.put("Cevap alınamadı, lütfen tekrar deneyin.")
        while tts_busy.is_set():
            time.sleep(0.2)


# ───────────────── SOKAK MODU HANDLER ─────────────────
def handle_street_mode(cam, last_text):
    # 1. Navigasyon isteği bekliyorsa önce onu işle
    if street_voice_ready.is_set():
        street_voice_ready.clear()
        # Kayıt bir mod komutuysa navigasyon isteği sayma
        if sesli_komut_isle():
            return ""
        handle_navigation_request()
        return ""  # last_text sıfırla, obstacle detection yeniden başlasın

    # 2. Kayıt sırasında bekle — TTS ile mikrofon çakışmasın
    if recorder.recording:
        time.sleep(0.2)
        return last_text

    # 3. Normal obstacle detection
    frame = cam.get()
    if frame is None:
        time.sleep(0.3)
        return last_text

    small = cv2.resize(frame, (320, 240))
    _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 50])
    img_b64 = base64.b64encode(buf).decode("utf-8")

    print("🚶 [SOKAK] Frame işleniyor (paralel)...")
    t_total = time.time()
    ai_text, depth_info = parallel_process(frame, img_b64)
    print(f"  ⏱️ Toplam: {time.time() - t_total:.1f}s")

    if depth_info:
        print(f"   📏 Sol: {depth_info['left']:.2f}m | "
              f"Orta: {depth_info['center']:.2f}m | "
              f"Sağ: {depth_info['right']:.2f}m")

    spoken = combine_results(ai_text, depth_info)
    # Cycle sırasında kullanıcı kayda başladıysa TTS atla — çakışmayı önle
    if spoken and not recorder.recording:
        print(f"📢 {spoken}")
        if spoken.lower() != last_text.lower():
            last_text = spoken
            tts_queue.put(spoken)
            t_start = time.time()
            while not tts_busy.is_set() and time.time() - t_start < 3:
                time.sleep(0.05)
            while tts_busy.is_set():
                time.sleep(0.2)
            print(f"⏳ {RATE_LIMIT_DELAY}s bekleniyor...")
            time.sleep(RATE_LIMIT_DELAY)
        else:
            print("⏭️ Aynı sonuç")
            time.sleep(1)
    else:
        time.sleep(1)
    return last_text


# ───────────────── MAIN ─────────────────
def main():
    print("SYSTEM STARTING...")
    cam = Camera().start()
    threading.Thread(target=tts_worker, daemon=True).start()

    if setup_raw_terminal():
        threading.Thread(target=keyboard_loop, daemon=True).start()
    else:
        print("⚠️  Klavye thread başlatılmadı, sokak modunda sabit kalacak")

    time.sleep(2)
    print("\n" + "=" * 50)
    print("SİSTEM HAZIR")
    print("=" * 50)
    print("Klavye kontrolleri (tek tuş, Enter'sız):")
    print("  h     → EV MODU      (SPACE: sesli soru sor)")
    print("  s     → SOKAK MODU   (sürekli analiz + SPACE: navigasyon)")
    print("  SPACE + \"ev modu\" / \"sokak modu\" → sesli mod değişimi")
    print("  q     → çıkış")
    print(f"Başlangıç modu: {current_mode}")
    print(f"Test konumu: {CURRENT_LOCATION} (GPS sonra)")
    print("=" * 50 + "\n")

    last_text = ""

    while True:
        with mode_lock:
            mode = current_mode

        if mode == MODE_HOME:
            handle_home_mode(cam)
        elif mode == MODE_STREET:
            last_text = handle_street_mode(cam, last_text)
        else:
            time.sleep(0.5)


if __name__ == "__main__":
    main()