"""
Asistif Gözlük - Gemini Live Bağlantısı
=======================================

gemini-3.8-live ile sürekli açık WebSocket oturumu. Cevaplar doğrudan ses
olarak gelir, edge-tts gerekmez.

Akışlar:
  onumde_ne_var()          → kamera karesi + sensör mesafesi + "Önümde ne var?"
  dinlemeye_basla()        → uzun basış: mikrofon sesi canlı akar
  ses_parcasi(pcm)         → mikrofon parçası (16 kHz, 16 bit, mono)
  dinlemeyi_bitir()        → bırakınca model cevaplar
  konusmayi_kes()          → kısa basış: o anki ve beklenen cevabı susturur

Olay gözcüsü: konuşma düzeyi "sessiz" değilse OLAY_ARALIGI_S saniyede bir
kamera karesi gönderilir. Model sadece önemli bir şey (aşağı basamak, yaya
geçidi, trafik ışığı, çukur ...) görürse konuşur. Bu modelde proaktif sessizlik
ayarı yok, bu yüzden model önemli bir şey yoksa "Yok" der ve bu turun sesi
çalınmadan atılır. Bu yüzden olay turlarının sesi, ilk kelimeler gelene kadar
tamponda bekletilir.

Model mesafe tahmin etmez: mesafe_olc fonksiyonuyla lazer sensörden alır.
Bağlantı koparsa geri çekilmeli olarak yeniden bağlanır, sunucu oturumu
kapatacağını bildirirse (GoAway) kaldığı yerden devam eder. Bağlı değilken
ana program eski yola (REST + vosk + sensör) döner.

Ayarlar (.env):
  AI_MOTORU=live          # live ya da rest
  CANLI_MODEL=gemini-3.8-live
  OLAY_ARALIGI_S=5        # olay gözcüsü aralığı, 0 = kapalı
"""

import os
import time
import wave
import asyncio
import threading
from collections import deque

CANLI_MODEL = (os.getenv("CANLI_MODEL") or "gemini-3.8-live").strip()
try:
    OLAY_ARALIGI_S = float(os.getenv("OLAY_ARALIGI_S") or 5)
except ValueError:
    OLAY_ARALIGI_S = 5.0

GIRIS_ORNEKLEME = 16000
CIKIS_ORNEKLEME = 24000
SON_KONUSMA_WAV = "/tmp/son_konusma.wav"

# Tur türleri
TUR_CEVAP = "cevap"
TUR_OLAY = "olay"

SISTEM_TALIMATI = (
    "Sen görme engelli bir kullanıcının gözlüğüsün. Türkçe konuş. "
    "Varsayılan konuşma düzeyi kısa: tek cümle, en fazla 12 kelime. Kullanıcı detaylı "
    "isterse en fazla 3 kısa cümle. Selamlama, giriş cümlesi, gereksiz açıklama yapma. "
    "En önemli şeyi önce söyle: yoldaki engel, basamak, kapı, insan, araç. Önemsiz eşyaları sayma. "
    "Yönleri saat sistemiyle söyle: görüntünün ortası saat 12, sol kenarı saat 10, sağ kenarı saat 2. "
    "Mesafeyi asla gözle tahmin etme. Mesajda sensör mesafesi verilmişse onu kullan, verilmemişse ve "
    "mesafe gerekiyorsa mesafe_olc fonksiyonunu çağır. Ondalıkları virgülle söyle: 'bir virgül dört metre'. "
    "Yazı, etiket, ilaç kutusu ya da para sorulursa okuyup söyle. "
    "Kullanıcı bir yere gitmek isterse navigasyon_baslat fonksiyonunu çağır. "
    "Kısa ya da detaylı konuşmanı veya sessiz olmanı isterse ayar_degistir fonksiyonunu bir kez çağır, "
    "sonra sadece kısa bir onay ver, örneğin 'Tamam, detaylı anlatacağım.' Sahneyi anlatma. "
    "Cihazın durumunu sorarsa durum_bildir fonksiyonunu çağır. "
    "OLAY KONTROLÜ ile başlayan mesajlar otomatik gönderilir, kullanıcı sormadı: sadece aşağı inen "
    "basamak, merdiven, yaya geçidi, trafik ışığının rengi, durağa gelen otobüsün numarası, çukur, "
    "yolda açık kapak gibi güvenlik ya da yön bulma için önemli bir şey varsa onu tek kısa cümleyle "
    "söyle. Aynı şeyi daha önce söylediysen tekrar etme. Önemli bir şey yoksa sadece 'Yok.' de."
)


def _arac_tanimlari(types):
    bos = types.Schema(type="OBJECT", properties={})
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="mesafe_olc",
            description="Tam öndeki (saat 12) nesneye lazer sensörle ölçülen kesin mesafeyi döner. "
                        "Sensör sadece 2 metreye kadar ölçer.",
            parameters=bos),
        types.FunctionDeclaration(
            name="ayar_degistir",
            description="Konuşma düzeyini değiştirir.",
            parameters=types.Schema(type="OBJECT", properties={
                "duzey": types.Schema(type="STRING", enum=["kisa", "detayli", "sessiz"],
                                      description="kisa: tek cümle, detayli: 2-3 cümle, "
                                                  "sessiz: sadece sorulunca konuş")},
                required=["duzey"])),
        types.FunctionDeclaration(
            name="durum_bildir",
            description="İnternet, mesafe sensörü ve konuşma düzeyi durumunu döner.",
            parameters=bos),
        types.FunctionDeclaration(
            name="navigasyon_baslat",
            description="Yürüyüş rotası hesaplar ve ilk adımı döner.",
            parameters=types.Schema(type="OBJECT", properties={
                "hedef": types.Schema(type="STRING", description="Gidilmek istenen yerin adı")},
                required=["hedef"])),
    ])]


class CanliAsistan:
    """
    araclar: {"mesafe_olc": f(), "ayar_degistir": f(duzey), "durum_bildir": f(),
              "navigasyon_baslat": f(hedef)} → her biri dict döner (thread'de çalışır)
    kare_al: f() → JPEG bayt ya da None
    duzey_al: f() → "kisa" | "detayli" | "sessiz"
    son_ses_ayarla: f(yol) → çift basışta tekrarlanacak dosyayı bildirir
    """

    def __init__(self, api_key, ses, araclar, kare_al, duzey_al, son_ses_ayarla,
                 model=CANLI_MODEL, olay_araligi=OLAY_ARALIGI_S):
        self.api_key = api_key
        self.ses = ses
        self.araclar = araclar
        self.kare_al = kare_al
        self.duzey_al = duzey_al
        self.son_ses_ayarla = son_ses_ayarla
        self.model = model
        self.olay_araligi = olay_araligi
        self.bagli = threading.Event()
        self.dinliyor = False
        self._loop = None
        self._kuyruk = None
        self._durdur = threading.Event()
        self._devam_kolu = None          # oturumu kaldığı yerden sürdürmek için
        self._bekleyen_turlar = deque()  # gönderilen isteklerin türleri, cevap sırasıyla
        self._tur = None                 # şu an gelen cevabın durumu
        self._son_etkilesim = 0.0
        self._susturulan_tur_sayisi = 0
        self.sayac = {"olay_gonderildi": 0, "olay_yok": 0, "olay_soylendi": 0}

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

    def ayar_bildir(self, duzey):
        """Konuşma düzeyi değişti (sesli komutla ya da başka yoldan): modele haber ver."""
        self._gonder("not", f"Bundan sonra konuşma düzeyi: {duzey}.")

    def konusmayi_kes(self):
        """O an çalan cevabı ve henüz gelmemiş cevapları susturur. Bir şey susturulduysa True."""
        susturuldu = False
        tur = self._tur
        if tur is not None and not tur["atla"]:
            tur["atla"] = True
            if tur["oynatici"] is not None:
                tur["oynatici"].kes()
            susturuldu = True
        if self._bekleyen_turlar:
            susturuldu = True
            self._susturulan_tur_sayisi = len(self._bekleyen_turlar)
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
                    # Kuyrukta bağlantı kopukken kalmış eski istekleri at
                    while not self._kuyruk.empty():
                        self._kuyruk.get_nowait()
                    self.bagli.set()
                    gorevler = [asyncio.create_task(self._gonderici(oturum)),
                                asyncio.create_task(self._alici(oturum)),
                                asyncio.create_task(self._olay_gozcusu())]
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
    def _kare_parcasi(self):
        jpeg = self.kare_al()
        if jpeg is None:
            return None
        return self.types.Part(inline_data=self.types.Blob(data=jpeg, mime_type="image/jpeg"))

    @staticmethod
    def _sensor_metni(sensor_m):
        if sensor_m is None:
            return "Sensör: tam önde 2 metre içinde engel yok."
        return f"Sensör: tam öndeki nesne {sensor_m:.1f} metre.".replace(".", ",", 1)

    async def _gonderici(self, oturum):
        t = self.types
        while True:
            komut = await self._kuyruk.get()
            tur = komut[0]
            if tur == "kapat":
                return
            if tur == "tarif":
                self._son_etkilesim = time.monotonic()
                parcalar = [p for p in [self._kare_parcasi()] if p is not None]
                parcalar.append(t.Part(text=f"Önümde ne var? {self._sensor_metni(komut[1])} "
                                            f"Konuşma düzeyi: {self.duzey_al()}."))
                self._bekleyen_turlar.append((TUR_CEVAP, time.monotonic()))
                await oturum.send_client_content(turns=t.Content(role="user", parts=parcalar),
                                                 turn_complete=True)
            elif tur == "aktivite_bas":
                self._son_etkilesim = time.monotonic()
                # Bağlam: sensör ve düzey (cevap üretmeden), sonra ses akışı
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[t.Part(
                        text=f"[{self._sensor_metni(komut[2])} Konuşma düzeyi: {self.duzey_al()}.]")]),
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
            elif tur == "olay":
                parca = self._kare_parcasi()
                if parca is None:
                    continue
                self._bekleyen_turlar.append((TUR_OLAY, time.monotonic()))
                self.sayac["olay_gonderildi"] += 1
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[parca, t.Part(text="OLAY KONTROLÜ")]),
                    turn_complete=True)
            elif tur == "not":
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[t.Part(text=komut[1])]), turn_complete=False)

    async def _olay_gozcusu(self):
        if self.olay_araligi <= 0:
            await asyncio.Event().wait()  # kapalı: sonsuza kadar bekle
        while True:
            await asyncio.sleep(self.olay_araligi)
            mesgul = (self.dinliyor or self._tur is not None or self._bekleyen_turlar
                      or self.ses.normal_caliyor or self.ses.acil_caliyor)
            yakin_zamanda = time.monotonic() - self._son_etkilesim < self.olay_araligi
            if self.duzey_al() == "sessiz" or mesgul or yakin_zamanda:
                continue
            self._kuyruk.put_nowait(("olay",))

    # ---------- alma ----------
    def _yeni_tur(self):
        # Bekleyen kayıt: (tür, isteğin gönderildiği an). Gecikme bu andan ölçülür.
        turu, t0 = self._bekleyen_turlar.popleft() if self._bekleyen_turlar else (TUR_CEVAP, time.monotonic())
        atla = False
        if self._susturulan_tur_sayisi > 0:
            self._susturulan_tur_sayisi -= 1
            atla = True
        return {"tur": turu, "atla": atla, "yok": False, "oynatici": None, "tampon": [], "pcm": [],
                "yazi": "", "karar": turu != TUR_OLAY, "arac": False, "t0": t0}

    def _sesi_isle(self, tur, veri):
        """
        pcm her zaman biriktirilir (çift basışta tekrar için, kesilmiş olsa da tamamı).
        atla: çalma (kullanıcı susturdu ya da "Dur" kesti). yok: olay turunda önemli bir şey yok.
        """
        if tur["yok"]:
            return
        if not tur["karar"]:
            tur["tampon"].append(veri)  # olay turu: "Yok" mu değil mi, yazı gelene kadar bekle
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

    def _olay_karari(self, tur, zorla=False):
        """Olay turunda yazı yeterince geldiyse sesi çal ya da at."""
        yazi = tur["yazi"].strip().lower().strip(".! ")
        if not zorla and len(yazi) < 4 and not yazi.startswith("yok"):
            return
        tur["karar"] = True
        if yazi.startswith("yok") or not yazi:
            tur["yok"] = True
            self.sayac["olay_yok"] += 1
            tur["tampon"].clear()
            return
        self.sayac["olay_soylendi"] += 1
        print("\n👁️  Olay: ", end="")
        for parca in tur["tampon"]:
            self._sesi_isle(tur, parca)
        tur["tampon"].clear()

    async def _arac_calistir(self, oturum, fc):
        t = self.types
        print(f"   🔧 {fc.name}({dict(fc.args or {})})")
        islev = self.araclar.get(fc.name)
        try:
            sonuc = await asyncio.get_running_loop().run_in_executor(
                None, lambda: islev(**dict(fc.args or {}))) if islev else {"hata": "bilinmeyen fonksiyon"}
        except Exception as e:
            sonuc = {"hata": f"{type(e).__name__}: {e}"}
        await oturum.send_tool_response(function_responses=[
            t.FunctionResponse(id=fc.id, name=fc.name, response=sonuc)])

    def _tur_bitti(self, tur):
        if tur["oynatici"] is not None:
            tur["oynatici"].bitir()
        yazi = tur["yazi"].strip()
        if yazi and tur["tur"] == TUR_CEVAP:
            ilk = tur.get("ilk_ses")
            print(f"📢 {yazi}  (ilk ses {ilk:.1f} s)" if ilk is not None else f"📢 {yazi}")
        elif yazi and tur["tur"] == TUR_OLAY and not tur["yok"]:
            print(yazi)
        # Çift basışta tekrar için sakla ("Yok" olay turları hariç)
        if tur["pcm"] and not tur["yok"]:
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
                    print(f"🎤 {sc.input_transcription.text}")
                if sc.output_transcription and sc.output_transcription.text:
                    if self._tur is None:
                        self._tur = self._yeni_tur()
                    self._tur["yazi"] += sc.output_transcription.text
                    if not self._tur["karar"]:
                        self._olay_karari(self._tur)
                if sc.interrupted and self._tur and self._tur["oynatici"]:
                    self._tur["oynatici"].kes()
                if sc.turn_complete and self._tur is not None:
                    tur = self._tur
                    if not tur["karar"]:
                        self._olay_karari(tur, zorla=True)
                    # Fonksiyon çağıran turun cevabı ayrı bir tur olarak gelir: türünü koru
                    if tur["arac"] and not tur["pcm"] and not tur["yazi"]:
                        self._bekleyen_turlar.appendleft((tur["tur"], tur["t0"]))
                        if tur["atla"]:
                            self._susturulan_tur_sayisi += 1
                    else:
                        self._tur_bitti(tur)
                    self._tur = None
