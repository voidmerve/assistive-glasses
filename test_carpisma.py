"""
Çarpışma mantığı testi: sahte saat, saniyede 15 okuma, gürültülü simülasyon.
Dururken, kalabalıkta ve baş çevirirken uyarı çıkmamalı; duvara yürürken her seferinde "Dur" gelmeli.

  python3 test_carpisma.py
  GURULTULER=0.06 TABAN=0.06 python3 test_carpisma.py   # gürültülü sensör + ayarlı taban
"""
import sys, random, math
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import guvenlik as g

def kos(senaryo, gurultu, tohum, sure=None):
    random.seed(tohum)
    sim = g.SimulasyonSensoru(senaryo=senaryo, hata_orani=0)
    t=[0.0]; m=g.CarpismaMantigi(saat=lambda:t[0]); m.GURULTU_TABANI_M=TABAN
    sure = sure or sim.toplam_sure
    olaylar=[]; med=g.deque(maxlen=3)
    for i in range(int(sure*15)):
        t[0]=i/15
        d=sim.gercek_mesafe(t[0])+random.gauss(0,gurultu)
        d=None if d>=2.0 else max(0.03,d)
        med.append(math.inf if d is None else d); f=float(g.np.median(med)); d=None if math.isinf(f) else f
        for o in m.guncelle(d): olaylar.append((round(t[0],2), o[0], round(o[1],2), None if d is None else round(d,2)))
    return olaylar

sonuc_ok=True
import os
TABAN=float(os.getenv("TABAN","0.025"))
for gurultu in [float(x) for x in os.getenv("GURULTULER","0.02,0.04").split(",")]:
    print(f"\n=== gürültü σ={gurultu*100:.0f} cm, 20 tekrar ===")
    for sen in ["duvar_yaninda","kalabalik","bas_cevirme","yaklasma"]:
        tik=dur=0; ilk_tik=[]; dur_mesafe=[]; durunca_tik=0
        for tohum in range(20):
            ol=kos(sen,gurultu,tohum)
            tik+=sum(o[1]=="tik" for o in ol); dur+=sum(o[1]=="dur" for o in ol)
            if sen=="yaklasma":
                # duvar 2 m'de görünür: t=2.0+1.0=3.0 s. Durma 4.5 s'de başlar.
                tl=[o for o in ol if o[0]>=2.0]
                if tl: ilk_tik.append(tl[0][0]-3.0)
                dur_mesafe += [o[3] for o in ol if o[1]=="dur"]
                durunca_tik += sum(1 for o in ol if 5.0 <= o[0] < 7.5)  # dururken (0,5 sn tolerans)
        print(f"{sen:14} toplam tık {tik:4}  Dur {dur:3}", end="")
        if sen=="yaklasma":
            print(f" | duvar görününce ilk uyarı ort. {sum(ilk_tik)/len(ilk_tik):.2f} s sonra"
                  f" | Dur mesafeleri {min(dur_mesafe):.2f}-{max(dur_mesafe):.2f} m | dururken olay {durunca_tik}")
            sonuc_ok &= dur==20 and durunca_tik==0
        else:
            print(); sonuc_ok &= dur==0 and tik<=2
print("\nSONUÇ:", "GEÇTİ" if sonuc_ok else "KALDI")
