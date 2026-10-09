"""
Asistif Gözlük - Çerçeve Modeli (v2)
====================================

Tasarım:
  - Kamera sol köşedeki, sensör sağ köşedeki yuvanın içinde (akıllı gözlüklerdeki
    yerleşim). Yuvaların arkası başın tarafında içe doğru daralır, yüze değmez.
  - Kart ÖNDEN takılır, önüne ayrı basılan "ön kapak" (farklı renk basılabilir,
    örn. siyah) oturur. Kapak yuvanın ön yüzüyle aynı hizada kalır.
  - Kablolar ön çerçeveyi hiç geçmez: kamera şerit kablosu yuvanın arkasından
    doğrudan sol sapın içindeki kanala, sensör kabloları sağ sapa girer. İkisi de
    kulağın hemen önünde sapın altındaki yarıktan çıkar, ensenin arkasından iner.
  - Sap kanalı başa bakan iç yüzde, ince bir kapakla kapanır: dışarıdan kablo görünmez.
  - Kamera 12° aşağı bakar: 1-3 m ilerideki basamak ve kaldırımı görür.
    Sensör düz bakar: görevi bastonun göremediği baş ve göğüs hizası.
  - Sap, yuvanın arkasındaki yuvaya geçer ve üstten 1 adet M2 vidayla tutulur.

Parçalar (stl/ klasörü) ve baskı yönü (destek gerekmez):
  gozluk_on.stl            ön yüz tablaya bakacak şekilde               1 adet
  gozluk_on_kapak_kamera   ön yüz tablaya                                1 adet
  gozluk_on_kapak_sensor   ön yüz tablaya                                1 adet
  gozluk_sap_sol / _sag    dış yüz tablaya (kanal yukarı bakar)          1'er adet
  gozluk_kapak             düz                                           2 adet
  test_kamera_yuvasi       önce bunu bas: kart oturuyor mu dene (~15 dk)

Ölçü notu: Kamera Modülü 3 ölçüleri resmi çizimden. Sensör kartı modelden modele
değişir: SENSOR_* değerlerini kumpasla ölçüp güncelleyin, sonra yeniden üretin.

Çalıştırma:  python3 gozluk.py   → stl/ ve onizleme/ klasörlerine yazar
"""

import math
import os

from manifold3d import JoinType, Manifold, OpType

from ortak import (bilgi, cokgen, tablaya, temizle, kutu, onizleme, silindir, stl_yaz,
                   yuvarlak_dikdortgen, yuvarlak_kutu)

# ═══════════════ PARAMETRELER (mm) ═══════════════
# Kamera Modülü 3: kart 25 × 24 × 1, lens bloğu ~8,5 kare ve karttan ~10 mm yüksek
KAMERA_W, KAMERA_H, KART_T = 25.0, 24.0, 1.0
LENS_KARE, LENS_YUKSEKLIK = 8.6, 10.2
LENS_Z = 2.5            # lens merkezinin kart merkezine göre yukarı kayması
KAMERA_EGIM = 12.0      # aşağı bakış açısı (derece)
FOV_YARIM_ACI = 56.0    # 102° yatay görüş + pay: ön kapak penceresi lensi gölgelemesin

# Mesafe sensörü kartı (GY-530 / GY-53 VL53L0X). KUMPASLA ÖLÇÜN.
SENSOR_W, SENSOR_H, SENSOR_T = 25.0, 16.0, 1.6
SENSOR_ARKA_BOSLUK = 4.0   # kartın arkasındaki pin/lehim payı
SENSOR_PENCERE_W, SENSOR_PENCERE_H = 9.0, 6.0
SENSOR_CIP_X, SENSOR_CIP_Z = 0.0, 0.0  # çipin kart merkezine göre konumu

TOLERANS = 0.25
VIDA_M2 = 2.3
KAPAK_ON_T = 1.4        # ön kapağın en ince yeri

# Yuva (köşe kutusu)
YUVA_X = 60.0           # yuva merkezinin orta çizgiye uzaklığı
YUVA_W, YUVA_H = 28.0, 33.0
YUVA_Z = 8.0
YUVA_ON_DERINLIK = 19.0  # tam genişlikteki ön kısım
YUVA_ARKA_Y = -29.0      # daralan arka ucun bittiği yer

# Ön çerçeve
ON_KALINLIK = 4.5

# Sap
SAP_X = 70.5            # sap merkezi: iki sapın iç yüzleri arası ~134 mm
SAP_W, SAP_H = 7.0, 22.0
SAP_DIL = 8.0           # yuvaya giren kısım
SAP_BOY = 100.0         # yuvanın arkasından kulak kıvrımına
KULAK_BOY = 38.0
KANAL_W, KANAL_H = 4.2, 17.4   # 16 mm şerit kablo kanalda dik durur
KAPAK_T, KAPAK_PAY = 1.0, 1.0


# ═══════════════ YARDIMCI ═══════════════
def egik_konum(m, egim, merkez_y):
    """Yerel koordinatta (kart merkezi, ön yüz +y) çizilen şekli eğip yuvaya yerleştirir."""
    return m.rotate([-egim, 0, 0]).translate([0, merkez_y, YUVA_Z])


def kamera_kart_y():
    """Kart merkezinin y konumu: ön kapak en üstte bile KAPAK_ON_T kalınlıkta kalsın."""
    t = math.radians(KAMERA_EGIM)
    ust = (KAMERA_H / 2 + TOLERANS) * math.sin(t)
    return -(KAPAK_ON_T + ust + (LENS_YUKSEKLIK + 0.4) * math.cos(t))


# ═══════════════ CEPLER ═══════════════
def kamera_cebi():
    """Önden açılan cep: kart ön yüzü yerel y=0, öne doğru taşan prizma kapak yuvası."""
    w, h = KAMERA_W + 2 * TOLERANS, KAMERA_H + 2 * TOLERANS
    on_prizma = kutu(w, 30, h).translate([0, 15, 0])                 # karttan öne: kapak + lens
    kart = kutu(w, KART_T + 0.4, h).translate([0, -(KART_T + 0.4) / 2, 0])
    arka = kutu(w - 2.0, 3.0, h - 2.0).translate([0, -KART_T - 0.4 - 1.5, -1.0])  # 1 mm basamak
    alt = kutu(w - 2.0, 6.0, 5.0).translate([0, -2.5, -h / 2 - 1.0])  # şerit kablonun kıvrılma payı
    cep = Manifold.batch_boolean([on_prizma, kart, arka, alt], OpType.Add)
    return egik_konum(cep, KAMERA_EGIM, kamera_kart_y())


def kamera_kapagi_govde():
    """Ön kapak = cebin öne taşan prizmasının lens ucundan öndeki kısmı, ön yüzü düz."""
    w, h = KAMERA_W + 2 * TOLERANS - 2 * 0.15, KAMERA_H + 2 * TOLERANS - 2 * 0.15
    l = LENS_YUKSEKLIK + 0.4
    prizma = kutu(w, 30, h).translate([0, l + 15, 0])
    prizma = egik_konum(prizma, KAMERA_EGIM, kamera_kart_y())
    return prizma ^ kutu(80, 40, 80).translate([0, -20, YUVA_Z])  # y <= 0 kısmı


def kamera_penceresi():
    l = LENS_YUKSEKLIK + 0.4
    r0 = LENS_KARE / 2 + 0.6
    koni = silindir(12, r0, merkez=False, r2=r0 + 12 * math.tan(math.radians(FOV_YARIM_ACI)))
    koni = koni.rotate([-90, 0, 0]).translate([0, l - 0.5, LENS_Z])
    return egik_konum(koni, KAMERA_EGIM, kamera_kart_y())


def sensor_kart_y():
    return -(KAPAK_ON_T + 0.6 + SENSOR_T / 2)


def sensor_cebi():
    w, h = SENSOR_W + 2 * TOLERANS, SENSOR_H + 2 * TOLERANS
    on_prizma = kutu(w, 30, h).translate([0, 15, 0])
    arka = kutu(w - 2.0, SENSOR_ARKA_BOSLUK + SENSOR_T, h - 2.0).translate(
        [0, -(SENSOR_ARKA_BOSLUK + SENSOR_T) / 2, 0])
    # Kartın arkasındaki kabloların sapa giden iç boşluğa geçişi
    gecit = kutu(10.0, 14.0, 7.0).translate([0, -SENSOR_T - 7.0, -h / 2 + 5.0])
    return egik_konum(on_prizma + arka + gecit, 0, sensor_kart_y() + SENSOR_T / 2)


def sensor_kapagi_govde():
    w, h = SENSOR_W + 2 * TOLERANS - 0.3, SENSOR_H + 2 * TOLERANS - 0.3
    t = -sensor_kart_y() - SENSOR_T / 2 - 0.1
    k = kutu(w, t, h).translate([0, -t / 2, YUVA_Z])
    pencere = (yuvarlak_dikdortgen(SENSOR_PENCERE_W, SENSOR_PENCERE_H, 1.5)
               .extrude(t + 0.2, scale_top=(1.5, 1.6)).rotate([-90, 0, 0])
               .translate([SENSOR_CIP_X, -t - 0.1, YUVA_Z + SENSOR_CIP_Z]))
    return k - pencere


# ═══════════════ YUVA VE ÖN ÇERÇEVE ═══════════════
def yuva_dis(s):
    """Önü tam genişlikte, arkası başın tarafından içe daralan yuva. s=-1 sol, +1 sağ."""
    on = yuvarlak_kutu(YUVA_W, YUVA_ON_DERINLIK, YUVA_H, 5).translate(
        [s * YUVA_X, -YUVA_ON_DERINLIK / 2, YUVA_Z])
    arka = yuvarlak_kutu(SAP_W + 4, 4, SAP_H + 6, 3).translate(
        [s * SAP_X, YUVA_ARKA_Y + 2, YUVA_Z])
    return Manifold.batch_hull([on, arka])


def yuva_ic(s):
    """Kartın arkasından sap yuvasına giden, kabloyu yönlendiren iç boşluk."""
    kart_arkasi = kutu(YUVA_W - 4.0, 2.0, YUVA_H - 8.0).translate([s * YUVA_X, -YUVA_ON_DERINLIK + 4.5, YUVA_Z - 0.5])
    sap_girisi = kutu(KANAL_W + 1.0, 2.0, KANAL_H).translate([s * (SAP_X - SAP_W / 2 + KANAL_W / 2 + 0.5),
                                                             YUVA_ARKA_Y + SAP_DIL + 1, YUVA_Z])
    return Manifold.batch_hull([kart_arkasi, sap_girisi])


def sap_yuvasi(s):
    """Sapın dilinin girdiği dikdörtgen yuva + üstten M2 vida deliği."""
    yuva = kutu(SAP_W + 2 * TOLERANS, SAP_DIL + 0.5, SAP_H + 2 * TOLERANS).translate(
        [s * SAP_X, YUVA_ARKA_Y + (SAP_DIL + 0.5) / 2 - 0.5, YUVA_Z])
    return yuva + sap_vidasi(s)


def sap_vidasi(s):
    return silindir(YUVA_H + 10, VIDA_M2 / 2).translate([s * SAP_X, YUVA_ARKA_Y + SAP_DIL / 2, YUVA_Z])


def cam_sekli(sag=True):
    """Sağ cam açıklığı (x > 0), yumuşak köşeli wayfarer biçimi."""
    k = [(9, 13), (45, 15), (46, -6), (39, -17), (16, -19), (9, -10)][::-1]  # saat yönünün tersi
    cs = cokgen(k).offset(-5, JoinType.Round).offset(5, JoinType.Round)
    return cs if sag else cs.mirror([1, 0])


def on_cerceve():
    camlar = cam_sekli(True) + cam_sekli(False)
    sekil = (camlar.offset(4.5, JoinType.Round) + yuvarlak_dikdortgen(104, 12, 5).translate([0, 15])
             + yuvarlak_dikdortgen(22, 14, 4).translate([0, 8]))
    r = 1.2  # kenar yuvatlama
    plaka = (sekil.offset(-r, JoinType.Round).extrude(ON_KALINLIK - 2 * r)
             .rotate([90, 0, 0]).translate([0, -r, 0]))
    plaka = plaka.minkowski_sum(Manifold.sphere(r, 12))
    plaka = plaka - camlar.extrude(40).rotate([90, 0, 0]).translate([0, 20, 0])

    # Burun pedleri: çerçevenin iç alt kenarından çıkan yumuşak destekler
    pedler = []
    for s in (-1, 1):
        ped = yuvarlak_kutu(4.5, 3.0, 12, 1.4).rotate([15, 0, s * -30]).translate([s * 10.5, -10, -9])
        kok = Manifold.sphere(2.2, 16).translate([s * 7.0, -3.0, -3.5])
        pedler.append(Manifold.batch_hull([ped, kok]))

    on = Manifold.batch_boolean([plaka, yuva_dis(-1), yuva_dis(1)] + pedler, OpType.Add)
    on = Manifold.batch_boolean([on,
                                 kamera_cebi().translate([-YUVA_X, 0, 0]),
                                 sensor_cebi().translate([YUVA_X, 0, 0]),
                                 yuva_ic(-1), yuva_ic(1), sap_yuvasi(-1), sap_yuvasi(1)], OpType.Subtract)  # fark
    return on


def kamera_on_kapagi():
    return kamera_kapagi_govde().translate([0, 0, 0]) - kamera_penceresi()


# ═══════════════ SAP ═══════════════
def sap(s=-1):
    """s=-1 sol sap (kamera), s=+1 sağ sap (sensör). Kanal başa bakan iç yüzde."""
    x0 = s * SAP_X
    y0 = YUVA_ARKA_Y + SAP_DIL - 0.3          # dilin ön ucu (yuvanın içinde)
    boy = SAP_DIL + SAP_BOY
    duz = (yuvarlak_dikdortgen(SAP_W, SAP_H, 3).extrude(boy).rotate([90, 0, 0])
           .translate([x0, y0, YUVA_Z]))
    uc_y = y0 - boy
    kulak = Manifold.batch_hull([
        yuvarlak_kutu(SAP_W, 6, SAP_H * 0.75, 3).translate([x0, uc_y + 3, YUVA_Z]),
        yuvarlak_kutu(5.0, 5, 8, 2.4).translate([x0 + s * -3, uc_y - KULAK_BOY * 0.8, YUVA_Z - KULAK_BOY * 0.55])])
    govde = duz + kulak

    ic_yuz = x0 - s * SAP_W / 2
    kanal_x = ic_yuz + s * (KANAL_W / 2 - 0.01)
    kanal_bas = y0 + 0.5
    kanal_son = uc_y + 14
    kanal = kutu(KANAL_W, kanal_bas - kanal_son, KANAL_H).translate([kanal_x, (kanal_bas + kanal_son) / 2, YUVA_Z])
    # Kapağın oturduğu pay: yuvanın dışında kalan kısımda
    pay_bas = YUVA_ARKA_Y - 1.0
    oturma = kutu(KAPAK_T + 0.02, pay_bas - kanal_son, KANAL_H + 2 * KAPAK_PAY).translate(
        [ic_yuz + s * (KAPAK_T / 2 - 0.01), (pay_bas + kanal_son) / 2, YUVA_Z])
    # Kulağın önünde, sapın altından kablo çıkışı
    cikis = kutu(KANAL_W, 16, SAP_H).translate([kanal_x, kanal_son + 7.9, YUVA_Z - SAP_H / 2])
    govde = govde - Manifold.batch_boolean([kanal, oturma, cikis], OpType.Add) - sap_vidasi(s)
    return govde


def sap_kapagi():
    """Kanalı kapatan şerit: düz basılır, oturma payına bastırılır (gerekirse bir damla yapıştırıcı)."""
    pay_bas = YUVA_ARKA_Y - 1.0
    kanal_son = YUVA_ARKA_Y + SAP_DIL - 0.3 - SAP_DIL - SAP_BOY + 14
    boy = pay_bas - kanal_son - 16 - 2 * TOLERANS  # çıkış yarığının önüne kadar
    return yuvarlak_dikdortgen(boy, KANAL_H + 2 * KAPAK_PAY - 2 * TOLERANS, 1.0).extrude(KAPAK_T)


def test_kamera_yuvasi():
    """Sadece sol yuvanın ön kısmı: kart oturuyor mu, kapak geçiyor mu diye kısa deneme baskısı."""
    return on_cerceve() ^ kutu(YUVA_W + 2, YUVA_ON_DERINLIK + 1, YUVA_H + 2).translate(
        [-YUVA_X, -YUVA_ON_DERINLIK / 2, YUVA_Z])


def parcalar():
    p = {
        "gozluk_on": on_cerceve(),
        "gozluk_on_kapak_kamera": kamera_on_kapagi().translate([-YUVA_X, 0, 0]),
        "gozluk_on_kapak_sensor": sensor_kapagi_govde().translate([YUVA_X, 0, 0]),
        "gozluk_sap_sol": sap(-1),
        "gozluk_sap_sag": sap(+1),
        "gozluk_kapak": sap_kapagi(),
        "test_kamera_yuvasi": test_kamera_yuvasi(),
    }
    return {ad: temizle(m) for ad, m in p.items()}


# ═══════════════ ÇIKTI ═══════════════
if __name__ == "__main__":
    burasi = os.path.dirname(os.path.abspath(__file__))
    p = parcalar()
    print("Gözlük parçaları:")
    # Baskı yönleri: ön yüzler tablaya (+Y → -Z), sapların dış yüzü tablaya
    yon = {"gozluk_on": (-90, 0, 0), "gozluk_on_kapak_kamera": (-90, 0, 0),
           "gozluk_on_kapak_sensor": (-90, 0, 0), "test_kamera_yuvasi": (-90, 0, 0),
           "gozluk_sap_sol": (0, -90, 0), "gozluk_sap_sag": (0, 90, 0), "gozluk_kapak": (0, 0, 0)}
    for ad, m in p.items():
        bilgi(m, ad)
        stl_yaz(tablaya(m, yon[ad]), ad)
    os.makedirs(os.path.join(burasi, "onizleme"), exist_ok=True)
    kirmizi, koyu, siyah = "#d93a2b", "#b8301f", "#2b2b2b"
    sahne = [(p["gozluk_on"], kirmizi), (p["gozluk_sap_sol"], koyu), (p["gozluk_sap_sag"], koyu),
             (p["gozluk_on_kapak_kamera"], siyah), (p["gozluk_on_kapak_sensor"], siyah)]
    onizleme(sahne, os.path.join(burasi, "onizleme", "gozluk.png"),
             gorunumler=((20, 60), (8, 90), (25, -130), (90, -90)),
             baslik="Gözlük v2: çapraz önden | önden | arkadan | üstten")
    print("STL'ler: tasarim/stl/  |  önizleme: tasarim/onizleme/gozluk.png")
