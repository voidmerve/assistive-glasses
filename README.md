# Asistif Gözlük

Görme engelli kullanıcılar için Raspberry Pi üzerinde geliştirilmiş, sesle kontrol edilen
akıllı gözlük prototipi. Sistem; kullanıcının çevresini sesli olarak tarif eder, engellere
karşı uyarır ve yürüyüş navigasyonu sağlar — tamamen Türkçe sesli giriş ve çıkış ile.

## Özellikler

**Ev modu** — Tuşa basıp sesli soru sorulur; sistem kamera görüntüsünü ve soruyu Gemini'ye
gönderir, cevabı Türkçe olarak sesli okur.

**Sokak modu** — Arka planda sürekli engel ve yön bildirimi yapar. Kullanıcı sesli olarak
bir hedef söyleyebilir ("Beşiktaş çarşısına gitmek istiyorum"); sistem hedefi Gemini ile
çıkarır, Google Maps'ten yürüyüş rotası alır ve adım adım sesli olarak bildirir.

## Donanım

- Raspberry Pi [model 4B 4GB]
- Kamera modülü - örn. Pi Camera v3
- VL53L0X mesafe sensörü (time-of-flight)
- Bluetooth kulaklık (mikrofon + hoparlör)

## Kullanılan Teknolojiler

- **Python 3**
- **Gemini API** (multimodal: görüntü + ses girişi)
- **Depth Anything V2** — tek kameradan derinlik tahmini
- **YOLOv8** — nesne tespiti
- **Vosk** — çevrimdışı Türkçe konuşma tanıma
- **edge-tts** — Türkçe metin okuma
- **Google Maps Directions API** — yürüyüş navigasyonu

## Kurulum


1. Gemini API key ve Google Maps API key alın
2. `son_mapli.py` dosyasını açın, CONFIG bölümündeki `API_KEY` ve `GOOGLE_MAPS_API_KEY`
   değerlerini kendi key'lerinizle değiştirin
3. Bluetooth kulaklığı eşleştirin ve varsayılan ses giriş/çıkışı yapın
4. Çalıştırın:

```bash
python son_mapli.py
```

## Kontroller (SSH üzerinden)

| Tuş | İşlev |
|-----|-------|
| `h` | Ev modu |
| `s` | Sokak modu |
| `SPACE` | Soru sor / hedef söyle |
| `q` | Çıkış |

## Durum / Yol Haritası

- [x] Çevre hakkında sesli soru-cevap (ev modu)
- [x] Derinlik tahmini + mesafe sensörü ile engel uyarısı
- [x] Sesle yürüyüş navigasyonu
- [ ] Telefon GPS entegrasyonu (konum şu an sabit test koordinatı)
