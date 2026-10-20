# Asistif Gözlük

Görme engelli kullanıcılar için Raspberry Pi 4 üzerinde çalışan, Türkçe konuşan akıllı gözlük
prototipi. Kamera görüntüsünü yapay zekâyla yorumlar; önündeki engelleri, çevresini, yazıları ve
eşyalarının yerini sesle anlatır. İki modu vardır: evde sessiz bir yardımcı, sokakta yürüyüş rehberi.

> Lisans tezi kapsamında geliştirilmiş bir prototiptir; tıbbi ya da güvenlik onaylı bir cihaz değildir,
> beyaz bastonun yerini tutmaz.

## Ne yapıyor?

### Ev modu (sessiz yardımcı)
Sadece sorulunca konuşur. Arka planda sessiz bir "gözcü" kareye bakıp şunları hatırlar:

- **Eşya hafızası:** Anahtar, cüzdan, telefon ve bilgisayarın yerini metin olarak kaydeder.
  "Anahtarım nerede?" → *"Anahtarını en son 10 dakika önce mutfak tezgâhında, ekmek kutusunun yanında gördüm."*
- **Değişimi fark eden ev hafızası:** Her odanın normal hâlini günlük kullanım sırasında kendiliğinden
  öğrenir. Yürüme yolunda yeni bir şey belirince söyler:
  *"Koridorda, her zamankinden farklı olarak sandalye var."*
- **Çıkış kontrolü:** "Çıkıyorum" denince ocak, musluk ve pencerenin son görülen durumunu ve eşyaların
  son yerini özetler.

### Sokak modu (yürüyüş rehberi)
- 30 saniyede bir önündeki en önemli şeyi, dakikada bir genel çevreyi anlatır.
- Yaya geçidi, trafik ışığı, aşağı inen basamak gibi olayları hemen söyler.
- 1,5 m'den yakın engelde *"Önünde sandalye var, sağdan geç"*, çok yakında bip + *"Dur"*.
  Bu uyarılar cihazda, internetsiz ve önceden kaydedilmiş seslerle çalar.
- **Navigasyon:** "Eczaneye götür" → telefonun GPS'iyle adım adım yürüyüş tarifi
  (Google Maps anahtarı yoksa OpenStreetMap).
- **Yer notları:** "Buraya not al: durağın önünde üç basamak var" → o noktaya yaklaşınca okunur.

### Her iki modda
- **"Olunca haber ver" nöbetçisi:** "34 gelince söyle", "annem gelince haber ver", "su kaynayınca söyle".
- **Sosyal ipuçları:** *"Saat 12 yönündeki kişi elini uzattı."*
- **Tanıdık yüzler:** "Bu Ayşe" ile tanıtılan kişiyi sonra görünce söyler: *"Saat 1 yönünde Ayşe var."*
- **Geçmiş:** "Az önce geçtiğimiz eczane neredeydi?" → *"6 dakika önce, yaklaşık 300 metre geride."*
- Soru-cevap, yazı / etiket / son kullanma tarihi okuma, "çevreyi tara".
- Yönler saat sistemiyle söylenir (tam önü saat 12).

## Nasıl çalışıyor?

```
Kamera (1280×960, 90° döndürülmüş)
   │
   ├─► Görü hattı (Pi'de, internetsiz) ─ YOLOv8n nesne tespiti
   │                                    ─ Depth Anything V2 derinlik + kalibrasyon eğrisi
   │                                    ─ YuNet + SFace yüz tanıma
   │       └─► engel uyarısı (hazır ses klipleri, anında)
   │
   └─► Gemini Live (iki oturum)
          ├─ kullanıcı oturumu: sorular, anlatım, fonksiyon çağrıları
          └─ sessiz gözcü: birkaç saniyede bir kareye bakar, sadece fonksiyonla bildirir
                (eşya, oda gözlemi, sokak olayı, nöbet, sosyal ipucu)
```

- **İki katman:** Güvenlik uyarıları cihazda ve anında çalışır. Anlama işi (ne var, ne yazıyor) bulutta.
  İnternet giderse cihaz mesafe uyarılarına ve çevrimdışı komutlara (Vosk) döner.
- **İki Live oturumu:** Gözcü kareleri kullanıcının oturumunda birikince cevaplar 1–2 sn'den
  5–15 sn'ye çıkıyordu. Gözcü ayrı oturumda çalışıyor ve sesi hiç çalınmıyor.
- **Mesafe:** Lazer sensör yerine tek kameradan derinlik tahmini. Model ham hâliyle her mesafeyi
  yaklaşık 2 m fazla söylüyordu. Ölçümle çıkarılan düzeltme eğrisi ve kişi kutusu boyutundan
  geometrik ölçüm birleştiriliyor (aşağıdaki tablo). Kullanıcı yürürken telefonun adım sayarı ve
  GPS'iyle kalibrasyon kendiliğinden denetleniyor.

### Mesafe kalibrasyonu sonuçları (ayakta duran insan, iç mekân)

| Gerçek | Model (ham) | Kalibrasyonlu |
|---|---|---|
| 1,0 m | 2,70 m (+%170) | 0,86 m (−%14) |
| 1,5 m | 3,18 m (+%112) | 1,52 m (+%1) |
| 2,0 m | 3,62 m (+%81) | 2,20 m (+%10) |
| 2,5 m | 3,81 m (+%53) | 2,25 m (−%10) |
| 3,0 m | 4,39 m (+%46) | 2,88 m (−%4) |

Kalibrasyonlu hata, her nokta dışarıda bırakılarak çıkarılan eğriyle hesaplanmıştır
(birini dışarıda bırak doğrulaması). Ortalama mutlak hata 1–3 m arasında yaklaşık %8.

## Gizlilik

- Eşya ve ev hafızası **sadece metin** saklar ("masanın üzerinde"), görüntü saklanmaz.
- Yüz tanıma fotoğraf saklamaz; kişi başına sadece yüz vektörü (128 sayı) tutulur, her şey Pi'de çalışır,
  internete gitmez. Sadece izin veren kişiler tanıtılmalı; "Ayşe'yi unut" ile silinir.
- "Az önce ne gördük" geçmişi sadece bellekte, 2 saat tutulur; diske yazılmaz.
- Kişisel veriler (`hafiza.json`, `ev_hafizasi.json`, `yuzler.json`, `yer_notlari.json`) ve `.env`
  git'e gönderilmez.

## Donanım

- Raspberry Pi 4 (4 GB) — 5,1 V 3 A güç kaynağı şart (düşük voltaj Bluetooth ve Wi-Fi kopmalarına yol açıyor)
- Raspberry Pi Camera Module 3 Wide (gözlüğe yan takılı; kare yazılımda döndürülüyor)
- Bluetooth kulaklık (mikrofonlu)
- Telefon: kumanda sayfası, GPS ve adım sayarı (ayrı uygulama gerekmez, tarayıcıdan açılır)

## Kurulum

```bash
git clone https://github.com/voidmerve/assistive-glasses.git ~/Desktop/Gemını
cd ~/Desktop/Gemını
pip install google-genai ultralytics transformers torch opencv-python flask python-dotenv \
            sounddevice vosk edge-tts requests      # picamera2 Raspberry Pi OS'ta hazır gelir
cp .env.example .env    # GEMINI_API_KEY ve KULAKLIK_MAC'i doldurun; sensör yoksa SENSOR_MODU=kapali
```

Repoda olmayan model dosyaları:

```bash
# YOLOv8n: ilk çalıştırmada ultralytics indirir ya da yolov8n.pt'yi klasöre koyun
# Türkçe çevrimdışı komutlar: https://alphacephei.com/vosk/models → küçük Türkçe modeli vosk_model/ olarak açın
# Yüz tanıma (OpenCV model deposu, ~38 MB):
mkdir -p modeller && cd modeller
curl -LO https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
curl -LO https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
# Depth Anything V2 Metric Small ilk çalıştırmada Hugging Face'ten iner (~95 MB)
```

Mesafe kalibrasyonu (bir kez, ~10 dakika, iki kişi):

```bash
python3 kalibrasyon.py --mesafeler 0.5 1 1.5 2 2.5 3
```

## Çalıştırma

```bash
sh servis/kur.sh                       # kullanıcı servisi olarak kur (--otomatik: açılışta başlar)
systemctl --user start asistif-gozluk  # başlat   (stop: durdur, restart: yeniden başlat)
tail -f gozluk.log                     # canlı çıktı
```

Açılınca adresler log'da yazar:

- **Jüri paneli:** `http://<pi-ip>:8080` — canlı görüntü, YOLO kutuları, derinlik haritası, olaylar, ölçümler
- **Telefon kumandası:** `https://<pi-ip>:8443/telefon` — düğmeler, konum ve adım paylaşımı
  (kendinden imzalı sertifika: tarayıcı uyarısını geçin)

## Kullanım

| Girdi | İşlev |
|---|---|
| Kısa basış | Önümde ne var? |
| Basılı tut + konuş | Soru ya da komut |
| Çift basış | Son söyleneni tekrarla |
| Telefon: Sustur / Ev / Sokak | Konuşmayı kes, mod değiştir |

Sesle örnek komutlar: *ev modu, sokak modu, ne görüyorsun, çevreyi tara, anahtarım nerede,
eczaneye götür, neredeyim, daha çok / daha az anlat, kısa konuş, detaylı anlat, sessiz ol,
çıkıyorum, evde ne değişti, 34 gelince haber ver, buraya not al, az önce ne gördük,
bu Ayşe, karşımda kim var, ne kadar uzak.*

SSH klavyesi (programı terminalde çalıştırınca): `d` kısa, `SPACE` uzun başlat/bitir, `r` tekrar,
`x` sustur, `h` ev, `s` sokak, `q` çıkış.

## Dosyalar

| Dosya | Görevi |
|---|---|
| `son_mapli.py` | Ana program: kamera, düğme olayları, fonksiyonlar, modlar |
| `canli.py` | Gemini Live: kullanıcı ve gözcü oturumları, sistem talimatı, fonksiyon tanımları |
| `mesafe.py` | Derinlik modeli, kalibrasyon eğrisi, geometrik ölçüm, engel uyarısı |
| `goru.py` | YOLOv8n nesne tespiti |
| `yuz.py` | Tanıdık yüz tanıma (YuNet + SFace) |
| `hafiza.py` | Eşya hafızası |
| `ev_hafizasi.py` | Değişimi fark eden ev hafızası, çıkış kontrolü |
| `gecmis.py` | "Az önce ne gördük" geçmişi, GPS yer notları |
| `yurume.py` | Yürürken kendiliğinden mesafe kalibrasyonu |
| `anlatici.py` | Sokak modunda neyin ne zaman söyleneceği |
| `navigasyon.py` | Google / OpenStreetMap yürüyüş navigasyonu |
| `panel.py` | Jüri paneli ve telefon kumandası (Flask) |
| `guvenlik.py` | Ses çalma, uyarı öncelikleri, sensör katmanı |
| `kulaklik.py` | Bluetooth kulaklık bekçisi (koparsa yeniden bağlar) |
| `sesli_komut.py` | Çevrimdışı Türkçe komutlar (Vosk) |
| `kalibrasyon.py` | Mesafe kalibrasyon aracı |
| `kontrol.py` | Fiziksel düğme / klavye |
| `sesler/` | İnternetsiz çalan hazır ses klipleri |

## Durum

- [x] Ev / sokak modu, eşya hafızası, navigasyon, telefon kumandası, jüri paneli
- [x] Kameradan mesafe + ölçümle kalibrasyon, internetsiz engel uyarısı
- [x] Değişimi fark eden ev hafızası, çıkış kontrolü, nöbetçi, yer notları, sosyal ipuçları,
      yüz tanıma, geçmiş, yürürken kalibrasyon — **saha testi bekliyor**
- [ ] Fiziksel düğme (şimdilik telefon kumandası)
- [ ] Oturan kişi ve iç mekân dışı nesneler için ayrı kalibrasyon
