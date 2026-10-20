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
  OLAY_ARALIGI_S=8        # sokak gözcüsü aralığı, 0 = kapalı
  CANLI_BAGLAM_TOKEN=12000  # oturum geçmişi sınırı: aşınca eski kareler atılır (cevaplar hızlı kalsın)
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


OLAY_ARALIGI_S = _sayi("OLAY_ARALIGI_S", 8)
BAGLAM_SINIRI = int(_sayi("CANLI_BAGLAM_TOKEN", 12000))  # bunu aşınca eski mesajlar (kareler) atılır
ESYA_ARALIGI_S = _sayi("ESYA_ARALIGI_S", 8)
SAHNE_DEGISIM_ESIGI = 10.0   # küçük gri kareler arası ortalama fark (0-255)
SAHNE_EN_UZUN_S = 60.0       # sahne değişmese de bu kadar sürede bir bak
HIZLI_GOZCU_S = 3.0          # nöbet varken ya da yakında biri varken gözcü bu sıklıkta bakar

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
- Mesafeyi asla gözle tahmin etme. Sadece mesajdaki mesafe bilgisini, oradaki talimata uyarak kullan:
  metre verilmişse "yaklaşık" diyerek, ondalığı virgülle ("yaklaşık bir virgül dört metre"); sadece
  yakınlık verilmişse ("çok yakında", "yakında", "uzakta") metre söyleme. Biçim: "saat 1 yönünde,
  yaklaşık iki metrede sandalye var" ya da "saat 1 yönünde, yakında sandalye var".
- Kullanıcı "ne kadar uzak", "kaç metre" diye sorarsa mesafe_olc çağır, sonucu "yaklaşık" diyerek söyle.
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
- "Çıkıyorum", "evden çıkıyorum", "dışarı çıkacağım" derse cikis_kontrolu çağır. En fazla 3 kısa cümle:
  önce dikkat gereken cihazlar ("dikkat": true, ör. ocak en son açıktı), sonra yanında olmayabilecek
  eşyalar (anahtar, cüzdan, telefon) ve en son yerleri. Bilgi eskiyse "en son ... önce" de.
- "Evde ne değişti", "bir şey değişmiş mi" derse ev_degisiklikleri çağır ve kısaca söyle.
- "... olunca / gelince / yanınca haber ver" derse nobet_kur çağır, sonra kısa onay ver ("Tamam, 34
  gelince söyleyeceğim."). "Beklemeyi bırak", "haber vermene gerek yok" derse nobet_iptal çağır.
- "Buraya not al", "buraya not bırak: ..." derse yer_notu_ekle çağır. "Buradaki notu sil" / "son notu
  sil" / "bütün notları sil" derse yer_notu_sil çağır. Dönen cümleyi söyle.
- "Az önce ne gördük", "geçtiğimiz eczane neredeydi", "bugün nereleri gördük" gibi sorularda
  gecmis_sorgula çağır; cevabı ne zaman ve ne kadar geride olduğuyla söyle. Listede yoksa uydurma.
- "Bu Ayşe", "bunu Ayşe olarak tanı" derse yuz_tanit çağır (kişinin izni olduğu varsayılır), dönen
  cümleyi söyle. "Karşımda kim var", "kim geldi" derse kim_var çağır. "Ayşe'yi unut" derse yuz_unut.

OTOMATİK MESAJLAR (kullanıcı sormadı, sesin kullanıcıya gitmez; sadece "Tamam" de):
- "SOKAK GÖZCÜSÜ": Karede güvenlik ya da yön bulma için önemli bir şey varsa bildir fonksiyonunu çağır.
  tur="olay": aşağı inen basamak, merdiven, yaya geçidi, trafik ışığının rengi, durağa gelen otobüsün
  numarası, çukur, açık kapak. tur="engel": yürüme yolunda ya da yakınında duran engeller: park etmiş
  bisiklet ya da motosiklet, kaldırıma park etmiş araç, yoldaki insan grubu, direk, çöp kutusu, tabela
  ayağı, sandalye, masa. tur="yer": eczane, market, fırın, durak, banka, okunabilen tabela ya da dükkân
  adı. Metin tek kısa cümle, saat yönüyle, mesajda mesafe bilgisi varsa onunla. Örnek: "Saat 1
  yönünde, park etmiş bisiklet." Önemli bir şey yoksa fonksiyon çağırma.
  Önceki gözcü mesajlarında bildirdiğini tekrar bildirme.
- SOSYAL İPUÇLARI (iki gözcüde de): Biri kullanıcıya elini uzatıyorsa (tokalaşma), el sallıyorsa,
  kullanıcıya doğru gelip konuşmak istiyor gibiyse ya da konuşulan kişi uzaklaşıp gittiyse
  bildir(tur="sosyal") çağır. Tek kısa cümle: "Saat 12 yönündeki kişi elini uzattı." Sadece açıkça
  görüyorsan; yoldan geçenleri bildirme.
- Mesajda "NÖBET:" varsa: kullanıcı orada yazan şeyi bekliyor. Karede bu AÇIKÇA gerçekleştiyse
  nobet_tetiklendi çağır (metin: ne oldu, saat yönüyle). Emin değilsen çağırma. Bu diğer görevlerine ektir.
- "EV GÖZCÜSÜ": Kareye dikkatle bak; küçük eşyaları da ara. Anahtar: tek anahtar, anahtar demeti,
  anahtarlık, kapı kartı. Cüzdan: deri ya da kumaş cüzdan, kartlık, para çantası. Ayrıca telefon ve
  bilgisayar (dizüstü, tablet değil). Gördüğün her biri için esya_kaydet çağır.
  KONUM KURALI: Konumu odadaki sabit bir şeye göre tarif et, çünkü kullanıcı sonra başka yöne bakarken
  soracak: "koltuğun üzerinde, sağ kolçağın yanında", "yemek masasında, meyve kasesinin yanında",
  "kapının yanındaki dolabın üstünde". ASLA saat yönü, "önünde", "solunda", "karşında" gibi bakış
  yönüne bağlı ifadeler kullanma. Emin değilsen ya da görmüyorsan fonksiyon çağırma.
  Ayrıca HER ev gözcüsü mesajında bolge_gozlemi çağır: bölge (salon, mutfak, koridor, yatak odası,
  banyo, antre...), kalıcı mobilya ve yerdeki şeyler (her seferinde AYNI sade adlarla: "sandalye",
  "sehpa", "ayakkabılık"; renk ve sayı ekleme), yürüme yolunda ya da yerdeyse yolda=true; görünen
  cihazların durumu (ocakta alev ya da açık düğme = açık, akan musluk = açık, açık pencere ya da
  kapı). Kare bulanık ya da bölge belirsizse çağırma.
- "OTOMATİK TARİF" (sokak modunda 30 sn'de bir, kullanıcı sormadı): Önündeki en önemli şeyi tek kısa
  cümleyle söyle, saat yönü ve mesajdaki mesafe bilgisiyle. Örnek: "Saat 12 yönünde bank
  var." Önünde dikkat çeken bir şey yoksa sadece "Önün açık." de. Bir önceki otomatik tarifle aynı şeyi
  görüyorsan yine kısa söyle ama aynı cümleyi kelimesi kelimesine tekrarlama.
- "OTOMATİK ÇEVRE ÖZETİ" (sokak modunda dakikada bir): Çevreyi 2-3 kısa cümleyle anlat: nerede
  olduğun (kaldırım, cadde, park, bina girişi, iç mekân), önemli nesneler ve saat yönleri, okunabilen
  tabela ve yazılar. Selamlama yok.
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
            parameters=bos if not ozellikler else S(
                type="OBJECT", properties=ozellikler,
                required=list(ozellikler) if gerekli is None else gerekli))

    esya = S(type="STRING", enum=["anahtar", "cüzdan", "telefon", "bilgisayar"])
    return [types.Tool(function_declarations=[
        f("mesafe_olc", "Tam öndeki mesafe ve görüntüdeki nesnelerin metre cinsinden mesafeleri: lazer sensör "
                        "(kesin, 2 m'ye kadar) ya da kamera derinlik yapay zekâsı (yaklaşık). Kullanıcı mesafeyi "
                        "sorarsa çağır."),
        f("ayar_degistir", "Konuşma düzeyini değiştirir.",
          {"duzey": S(type="STRING", enum=["kisa", "detayli", "sessiz"],
                      description="kisa: tek cümle, detayli: 2-3 cümle, sessiz: sadece sorulunca konuş")}),
        f("anlatim_sikligi", "Sokak modunda kendiliğinden yer bildirimi sıklığını değiştirir.",
          {"yon": S(type="STRING", enum=["daha_cok", "daha_az"])}),
        f("mod_degistir", "Ev ya da sokak moduna geçer.", {"mod": S(type="STRING", enum=["ev", "sokak"])}),
        f("durum_bildir", "İnternet, sensör, mod, konuşma düzeyi ve navigasyon durumunu döner."),
        f("esya_kaydet", "Ev gözcüsü: görülen eşyanın yerini metin olarak kaydeder (görüntü saklanmaz).",
          {"esya": esya, "konum": S(type="STRING", description="Odadaki sabit bir şeye göre, ör. 'koltuğun "
                                     "üzerinde, sağ kolçağın yanında'. Saat yönü ya da 'önünde' YOK.")}),
        f("esya_nerede", "Eşyanın en son nerede ve ne zaman görüldüğünü döner.", {"esya": esya}),
        f("bildir", "Gözcü: önemli bir olay, yoldaki bir engel, yön bulmaya yarayan bir yer ya da sosyal bir "
                    "ipucu (biri el uzatıyor, el sallıyor) bildirir.",
          {"tur": S(type="STRING", enum=["olay", "engel", "yer", "sosyal"]),
           "metin": S(type="STRING", description="Tek kısa Türkçe cümle, saat yönüyle")}),
        f("bolge_gozlemi", "Ev gözcüsü: karedeki bölgeyi, gördüğün mobilya ve eşyaları ve cihaz durumlarını "
                           "metin olarak bildirir (değişim fark etme ve çıkış kontrolü için).",
          {"bolge": S(type="STRING", description="Odanın ya da yerin kısa adı: salon, mutfak, koridor, "
                                                  "yatak odası, banyo, antre, çalışma odası, balkon"),
           "nesneler": S(type="ARRAY", description="Kalıcı ve yerdeki şeyler, en fazla 12, sade tekil adlar "
                                                    "(sandalye, sehpa, ayakkabılık, çanta, kutu)",
                         items=S(type="OBJECT", properties={
                             "ad": S(type="STRING"),
                             "yolda": S(type="BOOLEAN", description="Yürüme yolunda ya da yerde, takılınabilir")},
                             required=["ad", "yolda"])),
           "durumlar": S(type="ARRAY", description="Görünen cihazların durumu; görünmüyorsa boş liste",
                         items=S(type="OBJECT", properties={
                             "cihaz": S(type="STRING", enum=["ocak", "fırın", "musluk", "pencere", "kapı",
                                                             "ışık", "ütü", "klima", "televizyon"]),
                             "durum": S(type="STRING", enum=["açık", "kapalı"])},
                             required=["cihaz", "durum"]))}),
        f("ev_degisiklikleri", "Evde neyin her zamankinden farklı olduğunu döner.",
          {"bolge": S(type="STRING", description="İsteğe bağlı: sadece bu bölge")}, gerekli=[]),
        f("cikis_kontrolu", "Kullanıcı evden çıkarken: eşyaların son yerleri ve ocak, musluk, pencere gibi "
                            "cihazların en son görülen durumu."),
        f("nobet_kur", "Kullanıcının beklediği bir şey olunca haber vermek için gözcüyü görevlendirir "
                       "(\"34 gelince söyle\", \"annem gelince haber ver\", \"su kaynayınca söyle\").",
          {"kosul": S(type="STRING", description="Beklenen şeyin net tarifi, ör. '34 numaralı otobüs durağa geldi'"),
           "sure_dk": S(type="NUMBER", description="En fazla kaç dakika beklensin (varsayılan 30)")},
          gerekli=["kosul"]),
        f("nobet_iptal", "Etkin bekleme görevini iptal eder."),
        f("nobet_tetiklendi", "Gözcü: NÖBET koşulu karede gerçekleşti.",
          {"metin": S(type="STRING", description="Kullanıcıya söylenecek tek kısa cümle, ör. '34 numaralı otobüs geldi, saat 1 yönünde.'")}),
        f("yer_notu_ekle", "Bulunulan yere (telefon GPS'i) sesli not bırakır; oraya yaklaşınca okunur.",
          {"metin": S(type="STRING", description="Notun kendisi, ör. 'durağın önünde üç basamak var'")}),
        f("yer_notu_sil", "Yer notunu siler.",
          {"hangisi": S(type="STRING", enum=["buradaki", "son", "hepsi"])}),
        f("gecmis_sorgula", "Son dakikalarda söylenen ve gözcünün gördüğü her şeyi (söylenmeyenler dahil) "
                            "zamanı ve şu anki konuma uzaklığıyla döner. 'Az önce ne gördük', 'geçtiğimiz eczane "
                            "neredeydi' gibi sorular için.",
          {"dakika": S(type="NUMBER", description="Kaç dakika geriye bakılsın (varsayılan 30)")}, gerekli=[]),
        f("yuz_tanit", "Karşıdaki kişinin yüzünü isimle kaydeder (sadece kişi izin verdiyse). Kişi yaklaşık bir "
                       "metrede, yüzü kameraya dönük durmalı; fonksiyon birkaç saniye sürer.",
          {"isim": S(type="STRING")}),
        f("yuz_unut", "Tanıtılmış bir kişinin yüz kaydını siler.", {"isim": S(type="STRING")}),
        f("kim_var", "Karşıda yüzü görünen tanıdık kişileri ve saat yönlerini döner. Tanıtılmış kişileri de listeler."),
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
                 yayinla=None, olcum=None, mesafe_bilgisi=None, gozcu=True, ad="kullanıcı",
                 mesgul_al=None, model=CANLI_MODEL,
                 olay_araligi=OLAY_ARALIGI_S, esya_araligi=ESYA_ARALIGI_S, hizli_al=None):
        self.api_key = api_key
        self.ses = ses
        self.araclar = araclar
        self.kare_al = kare_al
        self.duzey_al = duzey_al
        self.mod_al = mod_al
        self.son_ses_ayarla = son_ses_ayarla
        self.yayinla = yayinla or (lambda tur, veri: None)
        self.olcum = olcum or (lambda ad, deger: None)
        # f(sensor_m) → mesafe metni (sensör + kamera derinlik tahmini). Verilmezse sadece sensör.
        self.mesafe_bilgisi = mesafe_bilgisi or self._sensor_metni
        # İki oturum kullanılır: gözcü kareleri kullanıcı oturumunun geçmişinde birikince
        # cevaplar 1-2 sn'den 5-15 sn'ye çıkıyordu. gozcu=False: sadece kullanıcıyla konuşur.
        # gozcu=True ve kullanıcı akışları hiç çağrılmazsa: sadece arka planda bakar, sesi çalınmaz.
        self.gozcu_acik = gozcu
        self.ad = ad
        self.mesgul_al = mesgul_al or (lambda: False)  # diğer oturum konuşurken gözcü beklesin
        # f() → True: yakında biri var (el uzatma gibi sosyal ipuçları kaçmasın), gözcü sık baksın
        self.hizli_al = hizli_al or (lambda: False)
        self._nobet = None  # {"kosul", "bitis"}: kullanıcının beklediği olay (gözcü oturumunda)
        self.model = model
        self.olay_araligi = olay_araligi
        self.esya_araligi = esya_araligi
        self.bagli = threading.Event()
        self.dinliyor = False
        self._loop = None
        self._kuyruk = None
        self._durdur = threading.Event()
        self._devam_kolu = None
        # Gönderilen isteklerin kayıtları, cevap sırasıyla: {"tur", "t0", "atla", "omur"}
        # omur: bu kadar saniyede cevap başlamazsa kayıt atılır (cevapsız kalan istek sırayı kaydırmasın)
        self._bekleyen_turlar = deque()
        self._tur = None
        self._son_etkilesim = 0.0
        self._giris_yazisi = ""
        self._son_kucuk_kare = None
        self._son_gozcu = 0.0
        self.sayac = {"gozcu": 0, "bildir": 0, "esya_kaydet": 0}
        self._son_soylenenler = deque(maxlen=6)  # (zaman, metin): otomatik anlatım aynı şeyi tekrarlamasın

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

    def otomatik_tarif(self, sensor_m=None):
        """Sokak modunda 30 sn'de bir: önündeki en önemli şey (kullanıcı sormadan, sesli)."""
        return self._gonder("otomatik", "OTOMATİK TARİF", sensor_m)

    def cevre_ozeti(self, sensor_m=None):
        """Sokak modunda dakikada bir: genel çevre (kullanıcı sormadan, sesli)."""
        return self._gonder("otomatik", "OTOMATİK ÇEVRE ÖZETİ", sensor_m)

    @property
    def mesgul(self):
        """Kullanıcı konuşuyor, bir cevap çalıyor ya da bekleniyor."""
        return self.dinliyor or self._tur is not None or bool(self._bekleyen_turlar)

    @property
    def son_etkilesim(self):
        return self._son_etkilesim

    def yonlendir(self, metin):
        return self._gonder("yonlendir", metin)

    # ---------- nöbet: "34 gelince söyle" (gözcü oturumunda) ----------
    def nobet_kur(self, kosul, sure_s=1800):
        self._nobet = {"kosul": kosul.strip(), "bitis": time.monotonic() + sure_s}

    def nobet_iptal(self):
        eski, self._nobet = self._nobet, None
        return eski

    @property
    def nobet(self):
        """Etkin nöbet ya da None. Süresi dolanı da döner ("suresi_doldu": True) ve kapatır."""
        n = self._nobet
        if n is not None and time.monotonic() > n["bitis"]:
            self._nobet = None
            return {**n, "suresi_doldu": True}
        return n

    def konusmayi_kes(self):
        """O an çalan cevabı ve henüz gelmemiş cevapları susturur. Bir şey susturulduysa True."""
        susturuldu = False
        tur = self._tur
        if tur is not None and tur["tur"] == TUR_CEVAP and not tur["atla"]:
            tur["atla"] = True
            if tur["oynatici"] is not None:
                tur["oynatici"].kes()
            susturuldu = True
        # Sadece ŞU AN bekleyen cevapları işaretle: bundan sonra gönderilecek sorunun cevabı etkilenmez
        for k in self._bekleyen_turlar:
            if k["tur"] == TUR_CEVAP and not k["atla"]:
                k["atla"] = True
                susturuldu = True
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
                # Gözcü kareleri oturum geçmişinde birikince model her soruda hepsini yeniden
                # işliyor, cevaplar 1-2 sn'den 10 sn'ye çıkıyordu: geçmişi küçük tut
                context_window_compression=types.ContextWindowCompressionConfig(
                    trigger_tokens=BAGLAM_SINIRI, sliding_window=types.SlidingWindow(target_tokens=BAGLAM_SINIRI // 2)),
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                session_resumption=types.SessionResumptionConfig(handle=self._devam_kolu),
                tools=_arac_tanimlari(types),
                system_instruction=SISTEM_TALIMATI,
            )
            try:
                async with client.aio.live.connect(model=self.model, config=config) as oturum:
                    print(f"🔴 Gemini Live bağlandı ({self.model}, {self.ad})")
                    bekleme = 1.0
                    self._bekleyen_turlar.clear()
                    self._tur = None
                    while not self._kuyruk.empty():  # bağlantı kopukken kalan eski istekler
                        self._kuyruk.get_nowait()
                    self.bagli.set()
                    gorevler = [asyncio.create_task(self._gonderici(oturum)),
                                asyncio.create_task(self._alici(oturum))]
                    if self.gozcu_acik:
                        gorevler.append(asyncio.create_task(self._gozcu()))
                    bitti, kalan = await asyncio.wait(gorevler, return_when=asyncio.FIRST_COMPLETED)
                    for g in kalan:
                        g.cancel()
                    for g in bitti:
                        if g.exception():
                            raise g.exception()
            except Exception as e:
                print(f"⚠️  Gemini Live ({self.ad}) bağlantısı koptu: {type(e).__name__}: {str(e)[:200]}")
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
        self._kayit_ekle(tur)
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
                                       f"Önümde ne var? {self.mesafe_bilgisi(komut[1])} {self._baglam()}",
                                       [self.kare_al()])
            elif tur == "aktivite_bas":
                self._son_etkilesim = time.monotonic()
                self._giris_yazisi = ""
                await oturum.send_client_content(
                    turns=t.Content(role="user", parts=[t.Part(
                        text=f"[{self.mesafe_bilgisi(komut[2])} {self._baglam()}]")]),
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
                self._kayit_ekle(TUR_CEVAP)
                await oturum.send_realtime_input(activity_end=t.ActivityEnd())
            elif tur == "gozcu":
                self.sayac["gozcu"] += 1
                await self._metin_turu(oturum, TUR_GOZCU,
                                       f"{komut[1]} {self.mesafe_bilgisi(None)} {self._baglam()}", [komut[2]])
            elif tur == "otomatik":
                # Kullanıcı etkileşimi sayılmaz (_son_etkilesim değişmez): kendi zamanlayıcısı var
                simdi = time.monotonic()
                yakin = [m for z, m in self._son_soylenenler if simdi - z < 75]
                gecmis = (" Son bir dakikada söylediklerin: " + " | ".join(yakin) +
                          ". Sahne değişmediyse bunları tekrarlama: çok kısa söyle ya da yeni bir ayrıntı ver."
                          ) if yakin else ""
                await self._metin_turu(oturum, TUR_CEVAP,
                                       f"{komut[1]}: {self.mesafe_bilgisi(komut[2])} {self._baglam()}{gecmis}",
                                       [self.kare_al()])
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
            nobet = self._nobet
            hizli = nobet is not None or self.hizli_al()
            aralik = self.olay_araligi if mod == "sokak" else self.esya_araligi
            if hizli:
                aralik = HIZLI_GOZCU_S if aralik <= 0 else min(aralik, HIZLI_GOZCU_S)
            await asyncio.sleep(aralik if aralik > 0 else 5)
            if aralik <= 0:
                continue
            self._eski_turlari_temizle()
            mesgul = (self.dinliyor or self._tur is not None or self._bekleyen_turlar
                      or self.ses.normal_caliyor or self.ses.acil_caliyor or self.mesgul_al())
            if mesgul or time.monotonic() - self._son_etkilesim < aralik:
                continue
            if mod == "sokak" and self.duzey_al() == "sessiz" and nobet is None:
                continue  # "sessiz ol": sokakta kendiliğinden bildirim yok (istenen nöbet hariç)
            jpeg = self.kare_al()
            if jpeg is None:
                continue
            if mod == "ev":
                # Sahne değişmediyse bakma; ama nöbet varken ya da yakında biri varken her zaman bak
                if not self._sahne_degisti(jpeg) and not hizli:
                    continue
                metin = "EV GÖZCÜSÜ"
            else:
                metin = "SOKAK GÖZCÜSÜ"
            if nobet is not None:
                metin += f". NÖBET: {nobet['kosul']}"
            self._son_gozcu = time.monotonic()
            self._kuyruk.put_nowait(("gozcu", metin, jpeg))

    # ---------- alma ----------
    def _kayit_ekle(self, tur, omur=30.0, basa=False, atla=False, t0=None):
        k = {"tur": tur, "t0": t0 or time.monotonic(), "eklendi": time.monotonic(), "atla": atla, "omur": omur}
        (self._bekleyen_turlar.appendleft if basa else self._bekleyen_turlar.append)(k)

    def _eski_turlari_temizle(self):
        """
        Cevabı hiç gelmeyen istek kaydı kuyrukta kalırsa sonraki cevaplar yanlış türe
        eşlenir (ör. kullanıcının cevabı sessiz gözcü turu sanılır). Ömrü dolanları at.
        """
        simdi = time.monotonic()
        self._bekleyen_turlar = deque(k for k in self._bekleyen_turlar if simdi - k["eklendi"] <= k["omur"])

    def _yeni_tur(self):
        self._eski_turlari_temizle()
        k = self._bekleyen_turlar.popleft() if self._bekleyen_turlar else \
            {"tur": TUR_CEVAP, "t0": time.monotonic(), "atla": False}
        return {"tur": k["tur"], "atla": k["atla"], "oynatici": None, "pcm": [], "yazi": "",
                "arac": False, "t0": k["t0"]}

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
            self._son_soylenenler.append((time.monotonic(), yazi))
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
                if sc.turn_complete and self._tur is None:
                    # İçeriksiz tur (model hiçbir şey üretmedi): yine de isteğin kaydını tüket
                    self._eski_turlari_temizle()
                    if self._bekleyen_turlar:
                        self._bekleyen_turlar.popleft()
                elif sc.turn_complete:
                    tur = self._tur
                    # Fonksiyon çağıran turun cevabı ayrı bir tur olarak gelir: türünü koru.
                    # Gözcü turunun devamı gelmeyebilir: kaydı 3 sn yaşar (sonraki cevabı yutmasın)
                    if tur["arac"] and not tur["pcm"] and not tur["yazi"]:
                        self._kayit_ekle(tur["tur"], omur=3.0 if tur["tur"] == TUR_GOZCU else 30.0,
                                         basa=True, atla=tur["atla"], t0=tur["t0"])
                    else:
                        self._tur_bitti(tur)
                    self._tur = None
