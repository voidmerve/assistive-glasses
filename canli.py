"""
Asistif Gözlük - Gemini Live Bağlantısı
=======================================

gemini-3.8-live ile sürekli açık WebSocket oturumu. Cevaplar doğrudan ses
olarak gelir, edge-tts gerekmez.

Kullanıcının başlattığı akışlar:
  onumde_ne_var()          → kamera karesi + sensör mesafesi + "Önümde ne var?"
  dinlemeye_basla()        → uzun basış: mikrofon sesi canlı akar
  ses_parcasi(pcm)         → mikrofon parçası (16 kHz, 16 bit, mono)
  dinlemeyi_bitir()        → bırakınca model cevaplar
  konusmayi_kes()          → kısa basış: o anki ve beklenen cevabı susturur
  soyle(metin)             → verilen cümleyi Live sesiyle söyletir (anlatıcı, hafıza)
  yonlendir(metin)         → navigasyon duyurusu; model görüntüde bir işaret görürse ekler

Gözcü (kullanıcı sormadan, SESSİZ turlar):
  Sokak modu: OLAY_ARALIGI_S'de bir kare. Model önemli bir şey görürse
    bildir(tur, metin) fonksiyonunu çağırır; söylenip söylenmeyeceğine
    anlatici.py karar verir (orta yol: olaylar hemen, yerler en fazla 30 sn'de bir).
  Ev modu: ESYA_ARALIGI_S'de bir, sahne değiştiyse kare. Model anahtar, cüzdan,
    telefon ya da bilgisayar görürse esya_kaydet(esya, konum) çağırır.
  Gözcü turlarının sesi hiç çalınmaz: model ne derse desin kullanıcı duymaz.

Model mesafe tahmin etmez: mesafe_olc fonksiyonuyla lazer sensörden alır.
Bağlantı koparsa geri çekilmeli olarak yeniden bağlanır, sunucu oturumu
kapatacağını bildirirse (GoAway) kaldığı yerden devam eder. Bağlı değilken
ana program eski yola (REST + vosk + sensör) döner.

Ayarlar (.env):
  AI_MOTORU=live          # live ya da rest
  CANLI_MODEL=gemini-3.8-live
  OLAY_ARALIGI_S=5        # sokak gözcüsü aralığı, 0 = kapalı
  ESYA_ARALIGI_S=8        # ev modunda eşya gözcüsü aralığı, 0 = kapalı
"""

import asyncio
import base64
import json
import os
import threading
import time
import wave
from collections import deque

import cv2
import numpy as np

CANLI_MODEL = (os.getenv("CANLI_MODEL") or "gemini-3.8-live").strip()


def _sayi(ad, varsayilan):
    try:
        return float(os.getenv(ad) or varsayilan)
    except ValueError:
        return varsayilan


OLAY_ARALIGI_S = _sayi("OLAY_ARALIGI_S", 5)
ESYA_ARALIGI_S = _sayi("ESYA_ARALIGI_S", 8)
SAHNE_DEGISIM_ESIGI = 10.0   # küçük gri kareler arası ortalama fark (0-255)
SAHNE_EN_UZUN_S = 60.0       # sahne değişmese de bu kadar sürede bir bak

GIRIS_ORNEKLEME = 16000
CIKIS_ORNEKLEME = 24000
SON_KONUSMA_WAV = "/tmp/son_konusma.wav"

TUR_CEVAP = "cevap"   # kullanıcıya çalınır
TUR_GOZCU = "gozcu"   # sessiz: sadece fonksiyon çağrıları işe yarar

SISTEM_TALIMATI = """Sen görme engelli bir kullanıcının akıllı gözlüğüsün. Türkçe konuş, "sen" diye hitap et.

İKİ MOD VAR, her mesajda hangisinde olduğun yazar:
- Ev modu: sessiz yardımcı. Sadece sorulunca konuş.
- Sokak modu: yürüyüşte yardımcı. Sorulunca konuş; gözcü mesajlarında sadece fonksiyonla bildir.

KONUŞMA BİÇİMİ:
- Konuşma düzeyi kısa ise tek cümle, en fazla 12 kelime. Detaylı ise en fazla 3 kısa cümle.
- Selamlama, giriş cümlesi, gereksiz açıklama yok. En önemli şey önce: engel, basamak, kapı, insan, araç.
- Yönleri saat sistemiyle söyle: görüntünün ortası saat 12, sol kenarı saat 10, sağ kenarı saat 2.
- Mesafeyi asla gözle tahmin etme. Mesajda sensör mesafesi varsa onu kullan, yoksa ve gerekiyorsa
  mesafe_olc fonksiyonunu çağır. Ondalıkları virgülle söyle: "bir virgül dört metre".
- Yazı, etiket, ilaç kutusu, son kullanma tarihi ya da para sorulursa okuyup söyle.

ÖZEL İSTEKLER:
- "Ne görüyorsun", "etrafımda ne var", "her şeyi anlat" gibi isteklerde zengin anlat: önemli nesneler,
  renkler, sayılar, okunabilen yazılar, saat yönleri; en fazla 5 cümle. Bu düzey ayarından bağımsızdır.
- "Çevreyi tara" denirse cevreyi_tara fonksiyonunu çağır. Fonksiyon, kullanıcı başını sağdan sola
  çevirirken çekilen kareleri döner (ilki sağ: saat 3, sonuncusu sol: saat 9). Gördüklerini saat
  yönleriyle, önemli olanlar önce, en fazla 5 cümleyle anlat. Fonksiyon dönmeden bir şey söyleme.
- Anahtar, cüzdan, telefon ya da bilgisayarın nerede olduğu sorulursa esya_nerede çağır ve sonucu söyle.
  O eşyayı şu an görüntüde görüyorsan bunu da ekle.
- Ev modu / sokak modu istenirse mod_degistir çağır, sonra sadece "Ev modu" ya da "Sokak modu" de.
- Kısa ya da detaylı konuşmanı veya sessiz olmanı isterse ayar_degistir çağır, sonra kısa bir onay ver.
- "Daha çok anlat" / "daha az anlat" denirse anlatim_sikligi çağır, sonra kısa bir onay ver.
- Bir yere gitmek isterse navigasyon_baslat, navigasyonu bitirmek isterse navigasyon_durdur,
  nerede olduğunu sorarsa neredeyim çağır ve dönen cümleyi söyle.
- Cihazın durumunu sorarsa durum_bildir çağır.

OTOMATİK MESAJLAR (kullanıcı sormadı, sesin kullanıcıya gitmez; sadece "Tamam" de):
- "SOKAK GÖZCÜSÜ": Karede güvenlik ya da yön bulma için önemli bir şey varsa bildir fonksiyonunu çağır.
  tur="olay": aşağı inen basamak, merdiven, yaya geçidi, trafik ışığının rengi, durağa gelen otobüsün
  numarası, çukur, açık kapak, yola park etmiş araç. tur="yer": eczane, market, fırın, durak, banka,
  okunabilen tabela ya da dükkân adı. Metin tek kısa cümle, saat yönüyle, mesafe biliniyorsa onunla.
  Önemli bir şey yoksa fonksiyon çağırma. Önceki gözcü mesajlarında bildirdiğini tekrar bildirme.
- "EV GÖZCÜSÜ": Karede anahtar, cüzdan, telefon ya da bilgisayar görüyorsan her biri için esya_kaydet
  çağır. Konum kısa ve yer belirten olsun: "masanın üzerinde, sol tarafta", "kanepenin sağ köşesinde",
  "mutfak tezgâhında, ekmek kutusunun yanında". Görmüyorsan fonksiyon çağırma.
- "SÖYLE:" ile başlayan mesajda sadece verilen cümleyi aynen söyle, hiçbir şey ekleme.
- "YÖNLENDİR:" ile başlayan mesajda verilen yönlendirmeyi söyle. Görüntüde dönüş yerini belirleyen
  belirgin bir işaret (dükkân, tabela, durak) varsa kısaca ekle, örneğin "eczanenin önünden".
"""


def _arac_tanimlari(types):
    S = types.Schema
    bos = S(type="OBJECT", properties={})

    def f(ad, aciklama, ozellikler=None, gerekli=None):
        return types.FunctionDeclaration(
            name=ad, description=aciklama,
            parameters=bos if not ozellikler else S(type="OBJECT", properties=ozellikler,
                                                     required=gerekli or list(ozellikler)))

    esya = S(type="STRING", enum=["anahtar", "cüzdan", "telefon", "bilgisayar"])
    return [types.Tool(function_declarations=[
        f("mesafe_olc", "Tam öndeki (saat 12) nesneye lazer sensörle ölçülen kesin mesafe. En fazla 2 metre."),
        f("ayar_degistir", "Konuşma düzeyini değiştirir.",
          {"duzey": S(type="STRING", enum=["kisa", "detayli", "sessiz"],
                      description="kisa: tek cümle, detayli: 2-3 cümle, sessiz: sadece sorulunca konuş")}),
        f("anlatim_sikligi", "Sokak modunda kendiliğinden yer bildirimi sıklığını değiştirir.",
          {"yon": S(type="STRING", enum=["daha_cok", "daha_az"])}),
        f("mod_degistir", "Ev ya da sokak moduna geçer.", {"mod": S(type="STRING", enum=["ev", "sokak"])}),
        f("durum_bildir", "İnternet, sensör, mod, konuşma düzeyi ve navigasyon durumunu döner."),
        f("esya_kaydet", "Ev gözcüsü: görülen eşyanın yerini metin olarak kaydeder (görüntü saklanmaz).",
          {"esya": esya, "konum": S(type="STRING", description="ör. 'masanın üzerinde, sol tarafta'")}),
        f("esya_nerede", "Eşyanın en son nerede ve ne zaman görüldüğünü döner.", {"esya": esya}),
        f("bildir", "Sokak gözcüsü: önemli bir olay ya da yön bulmaya yarayan bir yer bildirir.",
          {"tur": S(type="STRING", enum=["olay", "yer"]),
           "metin": S(type="STRING", description="Tek kısa Türkçe cümle, saat yönüyle")}),
        f("cevreyi_tara", "Kullanıcı başını sağdan sola çevirirken birkaç kare toplar ve çevreyi anlattırır."),
        f("navigasyon_baslat", "Yürüyüş rotası hesaplar, söylenecek özeti döner.",
          {"hedef": S(type="STRING", description="Gidilmek istenen yerin adı")}),
        f("navigasyon_durdur", "Etkin navigasyonu bitirir."),
        f("neredeyim", "Kullanıcının bulunduğu sokak ve semti, navigasyon varsa kalan mesafeyi döner."),
    ])]


class CanliAsistan:
    """
    araclar: {fonksiyon adı: f(**args) → dict}  (thread havuzunda çalışır)
    kare_al: f() → JPEG bayt ya da None
    duzey_al: f() → "kisa" | "detayli" | "sessiz"
    mod_al: f() → "ev" | "sokak"
    son_ses_ayarla: f(yol) → çift basışta tekrarlanacak dosya
    yayinla: f(tur, veri) → jüri paneline (isteğe bağlı)
    olcum: f(ad, deger) → tez ölçümleri (isteğe bağlı)
    """

    def __init__(self, api_key, ses, araclar, kare_al, duzey_al, mod_al, son_ses_ayarla,
                 yayinla=None, olcum=None, model=CANLI_MODEL,
                 olay_araligi=OLAY_ARALIGI_S, esya_araligi=ESYA_ARALIGI_S):
        self.api_key = api_key
        self.ses = ses
        self.araclar = araclar
        self.kare_al = kare_al
        self.duzey_al = duzey_al
        self.mod_al = mod_al
        self.son_ses_ayarla = son_ses_ayarla
        self.yayinla = yayinla or (lambda tur, veri: None)
        self.olcum = olcum or (lambda ad, deger: None)
        self.model = model
        self.olay_araligi = olay_araligi
        self.esya_araligi = esya_araligi
        self.bagli = threading.Event()
        self.dinliyor = False
        self._loop = None
        self._kuyruk = None
        self._durdur = threading.Event()
        self._devam_kolu = None
        self._bekleyen_turlar = deque()  # (tür, gönderim anı), cevap sırasıyla
        self._tur = None
        self._son_etkilesim = 0.0
        self._susturulan_tur_sayisi = 0
        self._giris_yazisi = ""
        self._son_kucuk_kare = None
        self._son_gozcu = 0.0
        self.sayac = {"gozcu": 0, "bildir": 0, "esya_kaydet": 0}

    # ═════════════ dışarıdan (başka thread'lerden) çağrılanlar ═════════════
    def baslat(self):
        threading.Thread(target=self._thread, daemon=True, name="canli").start()
        return self

    @property
    def hazir(self):
        return self.bagli.is_set()

    def _gonder(self, *komut):
        if self._loop is None or not self.hazir:
            return False
        self._loop.call_soon_threadsafe(self._kuyruk.put_nowait, komut)
        return True

    def onumde_ne_var(self, sensor_m):
        return self._gonder("tarif", sensor_m)

    def dinlemeye_basla(self, onkayit_pcm, sensor_m):
        self.dinliyor = self._gonder("aktivite_bas", onkayit_pcm, sensor_m)
        return self.dinliyor

    def ses_parcasi(self, pcm):
        if self.dinliyor:
            self._gonder("ses", pcm)

    def dinlemeyi_bitir(self):
        if not self.dinliyor:
            return False
        self.dinliyor = False
        return self._gonder("aktivite_bit")

    def not_dus(self, metin):
        """Modele cevap beklemeden bilgi verir (mod ya da düzey değişti)."""
        self._gonder("not", metin)

    def soyle(self, metin):
        return self._gonder("soyle", metin)

    def yonlendir(self, metin):
        return self._gonder("yonlendir", metin)

    def konusmayi_kes(self):
        """O an çalan cevabı ve henüz gelmemiş cevapları susturur. Bir şey susturulduysa True."""
        susturuldu = False
        tur = self._tur
        if tur is not None and tur["tur"] == TUR_CEVAP and not tur["atla"]:
            tur["atla"] = True
            if tur["oynatici"] is not None:
                tur["oynatici"].kes()
            susturuldu = True
        bekleyen_cevap = sum(1 for t, _ in self._bekleyen_turlar if t == TUR_CEVAP)
        if bekleyen_cevap:
            susturuldu = True
            self._susturulan_tur_sayisi = bekleyen_cevap
        return susturuldu

    def durdur(self):
        self._durdur.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._kuyruk.put_nowait, ("kapat",))

    # ═════════════ asyncio tarafı ═════════════
    def _thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._kuyruk = asyncio.Queue()
        self._loop.run_until_complete(self._oturum_dongusu())

    async def _oturum_dongusu(self):
        from google import genai
        from google.genai import types
        self.types = types
        client = genai.Client(api_key=self.api_key)
        bekleme = 1.0
        while not self._durdur.is_set():
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(language_code="tr-TR"),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                input_audio_transcription=types.AudioTranscriptionConfig(),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
                context_window_compression=types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow()),
                session_resumption=types.SessionResumptionConfig(handle=self._devam_kolu),
                tools=_arac_tanimlari(types),
                system_instruction=SISTEM_TALIMATI,
            )
            try:
                async with client.aio.live.connect(model=self.model, config=config) as oturum:
                    print(f"🔴 Gemini Live bağlandı ({self.model})")
                    bekleme = 1.0
                    self._bekleyen_turlar.clear()
                    self._tur = None
                    while not self._kuyruk.empty():  # bağlantı kopukken kalan eski istekler
                        self._kuyruk.get_nowait()
                    self.bagli.set()
                    gorevler = [asyncio.create_task(self._gonderici(oturum)),
                                asyncio.create_task(self._alici(oturum)),
                                asyncio.create_task(self._gozcu())]
                    bitti, kalan = await asyncio.wait(gorevler, return_when=asyncio.FIRST_COMPLETED)
                    for g in kalan:
                        g.cancel()
                    for g in bitti:
                        if g.exception():
                            raise g.exception()
            except Exception as e:
                print(f"⚠️  Gemini Live bağlantısı koptu: {type(e).__name__}: {str(e)[:200]}")
            finally:
                self.bagli.clear()
                self.dinliyor = False
                if self._tur and self._tur["oynatici"]:
                    self._tur["oynatici"].bitir()
            if self._durdur.is_set():
                break
            await asyncio.sleep(bekleme)
            bekleme = min(bekleme * 2, 30.0)  # internet yokken boşuna zorlamasın

    # ---------- gönderme ----------
    def _resim(self, jpeg):
        return self.types.Part(inline_data=self.types.Blob(data=jpeg, mime_type="image/jpeg"))

    @staticmethod
    def _sensor_metni(sensor_m):
        if sensor_m is None:
            return "Sensör: tam önde 2 metre içinde engel yok."
        return f"Sensör: tam öndeki nesne {sensor_m:.1f} metre.".replace(".", ",", 1)

    def _baglam(self):
        return f"Mod: {self.mod_al()}. Konuşma düzeyi: {self.duzey_al()}."

    async def _metin_turu(self, oturum, tur, metin, jpegler=()):
        parcalar = [self._resim(j) for j in jpegler if j is not None]
        parcalar.append(self.types.Part(text=metin))
        self._bekleyen_turlar.append((tur, time.monotonic()))
        await oturum.send_client_content(turns=self.types.Content(role="user", parts=parcalar),
                                         turn_complete=True)

    async def _gonderici(self, oturum):
        t = self.types
        while True:
            komut = await self._kuyruk.get()
            tur = komut[0]
            if tur == "kapat":
                return
            if tur == "tarif":
                self._son_etkilesim = time.monotonic()
                self.yayinla("kullanici", "(kısa basış) Önümde ne var?")
                await self._metin_turu(oturum, TUR_CEVAP,
                                       f"Önümde ne var? {self._sensor_metni(komut[1])} {self._baglam()}",
                                       [self.kare_al()])
            elif tur == "aktivite_bas":
                self._son_etkilesim = time.monotonic()
                self._giris_yazisi = ""
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[t.Part(
                        text=f"[{self._sensor_metni(komut[2])} {self._baglam()}]")]),
                    turn_complete=False)
                await oturum.send_realtime_input(activity_start=t.ActivityStart())
                jpeg = self.kare_al()
                if jpeg is not None:
                    await oturum.send_realtime_input(video=t.Blob(data=jpeg, mime_type="image/jpeg"))
                if komut[1]:
                    await oturum.send_realtime_input(
                        audio=t.Blob(data=komut[1], mime_type=f"audio/pcm;rate={GIRIS_ORNEKLEME}"))
            elif tur == "ses":
                await oturum.send_realtime_input(
                    audio=t.Blob(data=komut[1], mime_type=f"audio/pcm;rate={GIRIS_ORNEKLEME}"))
            elif tur == "aktivite_bit":
                self._son_etkilesim = time.monotonic()
                self._bekleyen_turlar.append((TUR_CEVAP, time.monotonic()))
                await oturum.send_realtime_input(activity_end=t.ActivityEnd())
            elif tur == "gozcu":
                self.sayac["gozcu"] += 1
                await self._metin_turu(oturum, TUR_GOZCU, f"{komut[1]} {self._baglam()}", [komut[2]])
            elif tur == "soyle":
                await self._metin_turu(oturum, TUR_CEVAP, f"SÖYLE: {komut[1]}")
            elif tur == "yonlendir":
                await self._metin_turu(oturum, TUR_CEVAP, f"YÖNLENDİR: {komut[1]}", [self.kare_al()])
            elif tur == "not":
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[t.Part(text=komut[1])]), turn_complete=False)

    def _sahne_degisti(self, jpeg):
        """Ev gözcüsü için: küçük gri kareler arası fark yeterince büyük mü."""
        kucuk = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if kucuk is None:
            return False
        kucuk = cv2.resize(kucuk, (40, 30)).astype(np.int16)
        onceki, self._son_kucuk_kare = self._son_kucuk_kare, kucuk
        if onceki is None or time.monotonic() - self._son_gozcu > SAHNE_EN_UZUN_S:
            return True
        return float(np.abs(kucuk - onceki).mean()) > SAHNE_DEGISIM_ESIGI

    async def _gozcu(self):
        while True:
            mod = self.mod_al()
            aralik = self.olay_araligi if mod == "sokak" else self.esya_araligi
            await asyncio.sleep(aralik if aralik > 0 else 5)
            if aralik <= 0:
                continue
            self._eski_turlari_temizle()
            mesgul = (self.dinliyor or self._tur is not None or self._bekleyen_turlar
                      or self.ses.normal_caliyor or self.ses.acil_caliyor)
            if mesgul or time.monotonic() - self._son_etkilesim < aralik:
                continue
            if mod == "sokak" and self.duzey_al() == "sessiz":
                continue  # "sessiz ol": sokakta kendiliğinden bildirim yok
            jpeg = self.kare_al()
            if jpeg is None:
                continue
            if mod == "ev":
                if not self._sahne_degisti(jpeg):
                    continue
                metin = "EV GÖZCÜSÜ"
            else:
                metin = "SOKAK GÖZCÜSÜ"
            self._son_gozcu = time.monotonic()
            self._kuyruk.put_nowait(("gozcu", metin, jpeg))

    # ---------- alma ----------
    def _eski_turlari_temizle(self, en_eski_s=30.0):
        """
        Cevabı hiç gelmeyen istek kaydı kuyrukta kalırsa sonraki cevaplar yanlış türe
        eşlenir (ör. kullanıcının cevabı sessiz gözcü turu sanılır). Eski kayıtları at.
        """
        simdi = time.monotonic()
        while self._bekleyen_turlar and simdi - self._bekleyen_turlar[0][1] > en_eski_s:
            self._bekleyen_turlar.popleft()

    def _yeni_tur(self):
        self._eski_turlari_temizle()
        turu, t0 = self._bekleyen_turlar.popleft() if self._bekleyen_turlar else (TUR_CEVAP, time.monotonic())
        atla = False
        if turu == TUR_CEVAP and self._susturulan_tur_sayisi > 0:
            self._susturulan_tur_sayisi -= 1
            atla = True
        return {"tur": turu, "atla": atla, "oynatici": None, "pcm": [], "yazi": "",
                "arac": False, "t0": t0}

    def _sesi_isle(self, tur, veri):
        """Gözcü turunun sesi atılır. Cevap turunda pcm hep biriktirilir (tekrar için)."""
        if tur["tur"] == TUR_GOZCU:
            return
        tur["pcm"].append(veri)
        if tur["atla"]:
            return
        if tur["oynatici"] is None:
            tur["ilk_ses"] = time.monotonic() - tur["t0"]
            tur["oynatici"] = self.ses.akis_ac(CIKIS_ORNEKLEME)
            if tur["oynatici"] is None:
                tur["atla"] = True
                return
        if not tur["oynatici"].yaz(veri):
            tur["atla"] = True  # "Dur" ya da kısa basış kesti

    async def _arac_calistir(self, oturum, fc):
        args = dict(fc.args or {})
        print(f"   🔧 {fc.name}({args})")
        if fc.name in self.sayac:
            self.sayac[fc.name] += 1
        islev = self.araclar.get(fc.name)
        try:
            sonuc = await asyncio.get_running_loop().run_in_executor(
                None, lambda: islev(**args)) if islev else {"hata": "bilinmeyen fonksiyon"}
        except Exception as e:
            sonuc = {"hata": f"{type(e).__name__}: {e}"}
        # Fonksiyon görüntü döndürdüyse (çevre taraması) cevapla birlikte gönder
        resimler = sonuc.pop("_resimler", None) if isinstance(sonuc, dict) else None
        if resimler:
            await self._gorsel_arac_cevabi(oturum, fc, sonuc, resimler)
            return
        await oturum.send_tool_response(function_responses=[
            self.types.FunctionResponse(id=fc.id, name=fc.name, response=sonuc)])

    async def _gorsel_arac_cevabi(self, oturum, fc, sonuc, resimler):
        """
        Görüntülü fonksiyon cevabı. google-genai 2.29'un send_tool_response'u görüntü
        baytlarını base64'e çevirmeden json.dumps'a verip çöküyor (bağlantı kopuyor).
        Bu yüzden mesajı kendimiz kurup aynı WebSocket'ten gönderiyoruz. SDK'nın iç
        bağlantı nesnesi (_ws) yoksa kareler ayrı bir kullanıcı mesajıyla gider.
        """
        mesaj = {"tool_response": {"functionResponses": [{
            "id": fc.id, "name": fc.name, "response": sonuc,
            "parts": [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(j).decode("ascii"),
                                      "displayName": f"kare_{i + 1}.jpg"}} for i, j in enumerate(resimler)],
        }]}}
        ws = getattr(oturum, "_ws", None)
        if ws is not None:
            await ws.send(json.dumps(mesaj))
            return
        await oturum.send_tool_response(function_responses=[
            self.types.FunctionResponse(id=fc.id, name=fc.name, response=sonuc)])
        await self._metin_turu(oturum, TUR_CEVAP, "Bunlar çevre taramasının kareleri, anlat.", resimler)

    def _tur_bitti(self, tur):
        if tur["oynatici"] is not None:
            tur["oynatici"].bitir()
        if tur["tur"] != TUR_CEVAP:
            return
        if self._giris_yazisi.strip():
            self.yayinla("kullanici", self._giris_yazisi.strip())
            self._giris_yazisi = ""
        yazi = tur["yazi"].strip()
        ilk = tur.get("ilk_ses")
        if yazi:
            print(f"📢 {yazi}" + (f"  (ilk ses {ilk:.1f} s)" if ilk is not None else ""))
            self.yayinla("gozluk", yazi)
        if ilk is not None and not tur["atla"]:
            self.olcum("Live ilk ses", ilk)
        if tur["pcm"]:
            try:
                with wave.open(SON_KONUSMA_WAV, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(CIKIS_ORNEKLEME)
                    wf.writeframes(b"".join(tur["pcm"]))
                self.son_ses_ayarla(SON_KONUSMA_WAV)
            except Exception as e:
                print(f"⚠️  Son konuşma kaydedilemedi: {e}")

    async def _alici(self, oturum):
        while True:
            async for m in oturum.receive():
                if m.session_resumption_update and m.session_resumption_update.new_handle:
                    self._devam_kolu = m.session_resumption_update.new_handle
                if m.go_away is not None:
                    print("🔁 Gemini Live oturumu yenileniyor")
                    return  # dış döngü kaldığı yerden yeniden bağlanır
                if m.tool_call:
                    if self._tur is None:
                        self._tur = self._yeni_tur()
                    self._tur["arac"] = True
                    for fc in m.tool_call.function_calls:
                        await self._arac_calistir(oturum, fc)
                sc = m.server_content
                if m.data:
                    if self._tur is None:
                        self._tur = self._yeni_tur()
                    self._sesi_isle(self._tur, m.data)
                if sc is None:
                    continue
                if sc.input_transcription and sc.input_transcription.text:
                    self._giris_yazisi += sc.input_transcription.text
                    print(f"🎤 {sc.input_transcription.text}")
                if sc.output_transcription and sc.output_transcription.text:
                    if self._tur is None:
                        self._tur = self._yeni_tur()
                    self._tur["yazi"] += sc.output_transcription.text
                if sc.interrupted and self._tur and self._tur["oynatici"]:
                    self._tur["oynatici"].kes()
                if sc.turn_complete and self._tur is not None:
                    tur = self._tur
                    # Fonksiyon çağıran turun cevabı ayrı bir tur olarak gelir: türünü koru
                    if tur["arac"] and not tur["pcm"] and not tur["yazi"]:
                        self._bekleyen_turlar.appendleft((tur["tur"], tur["t0"]))
                        if tur["atla"]:
                            self._susturulan_tur_sayisi += 1
                    else:
                        self._tur_bitti(tur)
                    self._tur = None
