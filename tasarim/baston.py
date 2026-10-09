"""
Asistif Gözlük - Akıllı Baston Sapı (v1)
========================================

Hazır, katlanabilir beyaz bastonun en üst borusuna takılan 3D baskı sap.
Bastonun lastik sapı çıkarılır, boru bu sapın altındaki yuvaya girer ve iki
yarımı birleştiren M3 vidalar boruyu kelepçe gibi sıkıştırır.

İçindekiler (ölçüler parametre, kendi parçalarınıza göre güncelleyin):
  - Seeed XIAO ESP32C3 (21 × 17,5 mm): Bluetooth LE + pil şarjı kartın üstünde.
    USB-C şarj girişi sapın tepesinden erişilir.
  - LiPo pil ~1000 mAh, ~10 × 20 × 50 mm (örn. 102050)
  - 2 × 10 mm düz (coin) titreşim motoru: sapın sol ve sağ iç duvarında, avuç altında
  - 12 × 12 mm tactile düğme: üst yüzde, başparmak altında; kabartma noktalı başlık
  - MPU-6050 (GY-521, 21 × 16 mm): yürüme ve düşme algılama
  - 12 mm pasif buzzer: "bastonum nerede" ve düşme sesi; alt yüzde ses delikleri
  - Mini sürgü anahtar: ana güç

Parçalar (stl/ klasörü), baskı yönü: yarımlar birleşme yüzü tablaya, destek gerekmez:
  baston_sap_sol.stl, baston_sap_sag.stl   (1'er adet, PETG önerilir: ter ve darbe)
  baston_dugme_basligi.stl                 (1 adet, kabartma nokta yukarı)

Eksenler: +Z bastonun yukarısı, +Y sapın üst yüzü (başparmak), X sol-sağ.

Çalıştırma:  python3 baston.py
"""

import os

from manifold3d import Manifold, OpType

from ortak import bilgi, kutu, onizleme, silindir, stl_yaz, tablaya, temizle, yuvarlak_kutu

# ═══════════════ PARAMETRELER (mm) ═══════════════
BORU_CAP = 12.7          # baston borusu dış çapı (çoğu katlanır beyaz baston 12,7 mm). ÖLÇÜN.
BORU_DERINLIK = 42.0
TOLERANS = 0.25
SAP_BOY = 168.0
SOKET_BOY = 46.0

PIL = (20.5, 10.5, 51.0)       # x, y, z (102050 LiPo + pay)
XIAO = (18.0, 7.0, 22.0)       # kart + yukarı bakan USB-C, dik durur
MPU = (16.5, 4.0, 21.5)
MOTOR_CAP, MOTOR_T = 10.4, 3.4
BUZZER_CAP, BUZZER_H = 12.4, 9.8
DUGME_KARE, DUGME_YUKSEKLIK = 12.3, 4.0
DUGME_Z = 128.0                # başparmağın doğal durduğu yer (sapın tepesinden ~40 mm)
ANAHTAR = (8.8, 4.0, 3.8)      # SS12D00 sürgü anahtar gövdesi
VIDA_M3, SOMUN_M3 = 3.3, 6.7   # somun: köşeden köşeye + tolerans
USB_C = (9.6, 3.8)


# ═══════════════ GÖVDE ═══════════════
def elips_disk(gx, gy, z, kalinlik=1.0):
    return silindir(kalinlik, 0.5, merkez=True).scale([gx, gy, 1]).translate([0, 0, z])


def govde():
    """Ergonomik sap: altta boruya inen boyun, ortada avuç dolduran şişkinlik, tepede yuvarlak uç."""
    kesitler = [
        (26, 26, 0.5),     # boru girişi: vidaların etrafında yeterli et kalsın
        (28, 28, 30),
        (29, 28, SOKET_BOY),
        (33, 29, 75),      # avuç
        (34, 30, 105),
        (32, 29, 135),     # başparmak bölgesi
        (30, 27, 158),
    ]
    disk = [elips_disk(gx, gy, z) for gx, gy, z in kesitler]
    tepe = yuvarlak_kutu(29, 26, 14, 9).translate([0, 0, SAP_BOY - 7])
    return Manifold.batch_hull(disk + [tepe])


def ic_bosluklar():
    b = []
    # Baston borusu yuvası (alttan)
    b.append(silindir(BORU_DERINLIK, BORU_CAP / 2 + TOLERANS, merkez=False).translate([0, 0, -0.1]))
    # Elektronik boşluğu: pil altta, kartlar üstte
    pil_z0 = SOKET_BOY + 6
    b.append(kutu(PIL[0], PIL[1], PIL[2]).translate([0, -2.0, pil_z0 + PIL[2] / 2]))
    kart_z0 = pil_z0 + PIL[2] + 2
    ust_boy = SAP_BOY - 12 - kart_z0   # tepede vida ve USB için 12 mm dolu bölge
    b.append(yuvarlak_kutu(22.0, 16.0, ust_boy, 2).translate([0, 0, kart_z0 + ust_boy / 2]))
    # Pil ile kart bölmesi arasında kablo geçidi
    b.append(kutu(10, 6, 6).translate([0, -2.0, kart_z0 - 1]))
    # Titreşim motorları: avucun altında, sol ve sağ duvarın iç yüzünde
    for s in (-1, 1):
        b.append(silindir(MOTOR_T + 3.0, MOTOR_CAP / 2).rotate([0, 90, 0])
                 .translate([s * (PIL[0] / 2 + (MOTOR_T + 3.0) / 2 - 1.5), -2.0, 95]))
    # Düğme: üst yüzde başlık oturağı, ince duvarda delik, altında anahtar cebi
    ust = 14.5                            # sapın bu yükseklikteki üst yüzü (yaklaşık)
    b.append(silindir(10, 16.6 / 2, merkez=False).rotate([-90, 0, 0]).translate([0, ust - 2.0, DUGME_Z]))
    b.append(silindir(6, 8.0 / 2, merkez=False).rotate([-90, 0, 0]).translate([0, ust - 4.0, DUGME_Z]))
    b.append(kutu(DUGME_KARE, DUGME_YUKSEKLIK + 0.5, DUGME_KARE).translate(
        [0, ust - 3.2 - (DUGME_YUKSEKLIK + 0.5) / 2, DUGME_Z]))
    b.append(kutu(DUGME_KARE, 8, DUGME_KARE).translate([0, 4, DUGME_Z]))  # bacaklar boşluğa
    # Buzzer: alt bölmede, ses delikleri alt-ön yüzde
    buz_z = SOKET_BOY + 1
    b.append(silindir(BUZZER_H, BUZZER_CAP / 2).rotate([90, 0, 0]).translate([0, -6, buz_z + 2]))
    for dx in (-3, 0, 3):
        b.append(silindir(10, 0.9).rotate([90, 0, 0]).translate([dx, -14, buz_z + 2]))
    b.append(kutu(8, 8, 6).translate([0, -4, buz_z + 6]))  # buzzer kabloları pile
    # USB-C şarj girişi: tepeden
    b.append(yuvarlak_kutu(USB_C[0] + 0.6, USB_C[1] + 0.6, 24, 1.4).translate([0, 1.5, SAP_BOY - 6]))
    # Güç anahtarı: tepeye yakın, alt yüzde (yanlışlıkla kapanmasın)
    b.append(kutu(ANAHTAR[0] + 0.4, 12, ANAHTAR[2] + 0.4).translate([0, -10, SAP_BOY - 22]))
    b.append(kutu(4.5, 20, 2.6).translate([0, -14, SAP_BOY - 22]))  # sürgü penceresi
    # Bilek askısı deliği
    b.append(silindir(40, 2.2).rotate([0, 90, 0]).translate([0, 8.0, SAP_BOY - 7]))
    return Manifold.batch_boolean(b, OpType.Add)


def vidalar():
    """Yarımları birleştiren ve boruyu sıkıştıran M3 vidalar: x yönünde, başı solda, somun sağda."""
    y = BORU_CAP / 2 + 3.6
    konumlar = [(y, 10), (-y, 10), (y, 32), (-y, 32),   # boru kelepçesi
                (-8.0, SAP_BOY - 7)]                     # tepe: USB girişinin arkası
    d = []
    for y, z in konumlar:
        d.append(silindir(60, VIDA_M3 / 2).rotate([0, 90, 0]).translate([0, y, z]))
        d.append(silindir(20, 3.1).rotate([0, 90, 0]).translate([-20 - 6.5, y, z]))          # vida başı havşası
        # Altıgen somun yuvası: somun sıkarken dönmesin (M3 somun köşeden köşeye ~6,35 mm)
        d.append(Manifold.cylinder(20, SOMUN_M3 / 2, SOMUN_M3 / 2, 6).rotate([0, 90, 0]).translate([6.5, y, z]))
    return Manifold.batch_boolean(d, OpType.Add)


def sap_tam():
    return govde() - ic_bosluklar() - vidalar()


def yarim(s):
    """s=-1 sol yarım (x<0), s=+1 sağ yarım. Birleşme yüzü x=0."""
    kes = kutu(100, 100, 400).translate([s * 50, 0, 150])
    return sap_tam() ^ kes


def dugme_basligi():
    """
    Düğme başlığı: oturağa giren disk + duvardaki delikten anahtara uzanan pim.
    Üstü hafif içbükey, ortada kabartma nokta: parmakla dokunarak bulunur.
    """
    disk = silindir(3.2, 15.8 / 2, merkez=False)
    cukur = Manifold.sphere(16, 48).translate([0, 0, 3.2 + 15.4])
    nokta = Manifold.sphere(1.2, 16).translate([0, 0, 2.7])
    pim = silindir(2.2, 7.4 / 2, merkez=False).translate([0, 0, -2.2])
    return disk - cukur + nokta + pim


def parcalar():
    p = {
        "baston_sap_sol": yarim(-1),
        "baston_sap_sag": yarim(+1),
        "baston_dugme_basligi": dugme_basligi(),
    }
    return {ad: temizle(m) for ad, m in p.items()}


if __name__ == "__main__":
    burasi = os.path.dirname(os.path.abspath(__file__))
    p = parcalar()
    print("Baston parçaları:")
    # Baskı yönü: yarımların birleşme yüzü (x=0) tablaya
    yon = {"baston_sap_sol": (0, 90, 0), "baston_sap_sag": (0, -90, 0), "baston_dugme_basligi": (0, 0, 0)}
    for ad, m in p.items():
        bilgi(m, ad)
        stl_yaz(tablaya(m, yon[ad]), ad)
    boru = silindir(160, BORU_CAP / 2, merkez=False).translate([0, 0, -150])
    baslik = p["baston_dugme_basligi"].rotate([-90, 0, 0]).translate([0, 14.2, DUGME_Z])
    acik = [(p["baston_sap_sol"].translate([-14, 0, 0]), "#d93a2b"),
            (p["baston_sap_sag"].translate([14, 0, 0]), "#c9c9c9"), (boru, "#eeeeee")]
    kapali = [(p["baston_sap_sol"], "#d93a2b"), (p["baston_sap_sag"], "#d93a2b"),
              (baslik, "#2b2b2b"), (boru, "#eeeeee")]
    os.makedirs(os.path.join(burasi, "onizleme"), exist_ok=True)
    onizleme(kapali, os.path.join(burasi, "onizleme", "baston.png"),
             gorunumler=((15, 30), (15, 120), (0, 0)), baslik="Baston sapı: kapalı")
    onizleme(acik, os.path.join(burasi, "onizleme", "baston_ic.png"),
             gorunumler=((25, 60), (0, 90)), baslik="Baston sapı: yarımlar ayrık, iç bölmeler")
    print("STL'ler: tasarim/stl/  |  önizleme: tasarim/onizleme/baston*.png")
