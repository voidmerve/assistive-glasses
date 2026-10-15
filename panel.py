"""
Asistif Gözlük - Jüri Paneli ve Telefon Konum Sayfası
=====================================================

Telefon sayfası iki iş görür: konumu gönderir ve fiziksel düğme gelene kadar kumanda
olur (önümde ne var, basılı tutup konuş, tekrar, ev/sokak modu).

Görme engelli kullanıcıyı etkilemeyen, sunumda jüri ve izleyici için canlı ekran:
kamera görüntüsü + YOLO kutuları, sensör mesafesi ve çarpışma süresi grafiği,
gözlüğün duyduğu ve söylediği her cümle, eşya hafızası, navigasyon haritası,
gecikme ölçümleri.

Adresler (Pi'nin IP'si ile, ör. 172.20.10.3):
  http://<ip>:8080/          jüri paneli (dizüstünde aç)
  https://<ip>:8443/telefon  telefon konum sayfası (telefonda aç)

Telefon tarayıcıları konumu sadece HTTPS'te verir. Pi ilk açılışta kendine
imzalı bir sertifika üretir (~/.config/asistif-gozluk/). Telefonda "bağlantı
gizli değil" uyarısı çıkar: Ayrıntılar → Siteyi ziyaret et.

Ölçümler olcumler.csv dosyasına yazılır (tez için: gecikme, yanlış alarm vb.).
"""

import csv
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, request

PANEL_PORT = int(os.getenv("PANEL_PORT") or 8080)
TELEFON_PORT = int(os.getenv("TELEFON_PORT") or 8443)
SERTIFIKA_DIZINI = os.path.expanduser("~/.config/asistif-gozluk")
OLCUM_DOSYASI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "olcumler.csv")


def ip_adresleri():
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout.split()
    except Exception:
        return []


def sertifika_hazirla():
    """Kendine imzalı sertifika (yoksa üretir). (cert, key) yollarını döner."""
    os.makedirs(SERTIFIKA_DIZINI, exist_ok=True)
    cert, key = os.path.join(SERTIFIKA_DIZINI, "cert.pem"), os.path.join(SERTIFIKA_DIZINI, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        san = ",".join([f"DNS:{socket.gethostname()}", f"DNS:{socket.gethostname()}.local", "DNS:localhost"]
                       + [f"IP:{ip}" for ip in ip_adresleri() if ":" not in ip])
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-keyout", key, "-out", cert, "-subj", "/CN=asistif-gozluk",
                        "-addext", f"subjectAltName={san}"],
                       check=True, capture_output=True)
        print("🔐 Telefon sayfası için sertifika üretildi")
    return cert, key


def _yer_tutucu(metin, boyut=(320, 240)):
    """Görüntü hazır olana kadar gösterilen gri kare (tarayıcı beklerken kilitlenmesin)."""
    import cv2
    import numpy as np
    kare = np.full((boyut[1], boyut[0], 3), 40, np.uint8)
    cv2.putText(kare, metin, (12, boyut[1] // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    return cv2.imencode(".jpg", kare)[1].tobytes()


class Panel:
    """
    kare_al: f() → JPEG bayt (kutular çizilmiş) ya da None
    durum_al: f() → dict (ilk açılışta tüm durumu göndermek için)
    konum_geldi: f(lat, lon, dogruluk) → navigasyona
    """

    DUGME_OLAYLARI = {"kisa", "uzun_basladi", "uzun_bitti", "cift", "ev", "sokak", "sustur"}

    def __init__(self, kare_al, durum_al, konum_geldi, harita_al=None, dugme_olayi=None):
        self.kare_al, self.durum_al, self.konum_geldi = kare_al, durum_al, konum_geldi
        self.dugme_olayi = dugme_olayi  # telefondaki kumanda düğmeleri → f(olay)
        self._onbellek = {}             # {ad: (zaman, jpeg)}: kare her istemci için ayrı kodlanmasın
        self._onbellek_kilit = threading.Lock()
        self.harita_al = harita_al or (lambda: None)  # renkli derinlik haritası (JPEG)
        self._abonelar = []
        self._kilit = threading.Lock()
        self._csv_kilit = threading.Lock()
        self.app = self._uygulama()

    def _onbellekli(self, ad, uretici, omur_s):
        """Aynı kareyi omur_s içinde tekrar üretmez: iki tarayıcı açıkken Pi iki kat yük almasın."""
        with self._onbellek_kilit:
            zaman, jpeg = self._onbellek.get(ad, (0.0, None))
            if time.time() - zaman < omur_s:
                return jpeg
            jpeg = uretici()
            self._onbellek[ad] = (time.time(), jpeg)
            return jpeg

    # ---------- olay yayını ----------
    def yayinla(self, tur, veri):
        mesaj = json.dumps({"tur": tur, "veri": veri, "zaman": time.time()}, ensure_ascii=False, default=str)
        with self._kilit:
            for q in list(self._abonelar):
                try:
                    q.put_nowait(mesaj)
                except queue.Full:
                    pass  # yavaş tarayıcı: eski mesajlar atlanır

    def olcum(self, tur, deger, birim="s"):
        """Tez ölçümü: CSV'ye yazar ve panele gönderir."""
        with self._csv_kilit:
            yeni = not os.path.exists(OLCUM_DOSYASI)
            with open(OLCUM_DOSYASI, "a", newline="") as f:
                w = csv.writer(f)
                if yeni:
                    w.writerow(["zaman", "olcum", "deger", "birim"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), tur, round(deger, 3), birim])
        self.yayinla("olcum", {"tur": tur, "deger": round(deger, 3), "birim": birim})

    # ---------- web ----------
    def _uygulama(self):
        app = Flask(__name__)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        @app.route("/")
        def ana():
            return Response(PANEL_HTML, mimetype="text/html")

        @app.route("/telefon")
        def telefon():
            return Response(TELEFON_HTML, mimetype="text/html")

        @app.route("/durum")
        def durum():
            return jsonify(self.durum_al())

        @app.route("/video")
        def video():
            bekleme = _yer_tutucu("kamera hazirlaniyor...", (640, 480))

            def akis():
                while True:
                    jpeg = self._onbellekli("video", self.kare_al, 0.2) or bekleme
                    yield b"--kare\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    time.sleep(0.2)  # 5 kare/sn: jüri için yeterli, Pi'yi yormaz
            return Response(akis(), mimetype="multipart/x-mixed-replace; boundary=kare")

        @app.route("/derinlik")
        def derinlik():
            bekleme = _yer_tutucu("derinlik modeli yukleniyor...")

            def akis():
                while True:
                    jpeg = self._onbellekli("derinlik", self.harita_al, 0.9) or bekleme
                    yield b"--kare\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    time.sleep(1.0)
            return Response(akis(), mimetype="multipart/x-mixed-replace; boundary=kare")

        @app.route("/olaylar")
        def olaylar():
            q = queue.Queue(maxsize=300)
            with self._kilit:
                self._abonelar.append(q)

            def akis():
                try:
                    yield f"data: {json.dumps({'tur': 'durum', 'veri': self.durum_al()}, ensure_ascii=False, default=str)}\n\n"
                    while True:
                        try:
                            yield f"data: {q.get(timeout=10)}\n\n"
                        except queue.Empty:
                            yield ": canli\n\n"
                finally:
                    with self._kilit:
                        if q in self._abonelar:
                            self._abonelar.remove(q)
            return Response(akis(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        @app.route("/api/dugme", methods=["POST"])
        def dugme():
            olay = (request.get_json(force=True, silent=True) or {}).get("olay")
            if olay not in self.DUGME_OLAYLARI or self.dugme_olayi is None:
                return jsonify({"hata": "geçersiz olay"}), 400
            self.dugme_olayi(olay)
            return jsonify({"tamam": True})

        @app.route("/api/konum", methods=["POST"])
        def konum():
            v = request.get_json(force=True, silent=True) or {}
            try:
                lat, lon = float(v["lat"]), float(v["lon"])
                dogruluk = float(v["dogruluk"]) if v.get("dogruluk") is not None else None
            except (KeyError, TypeError, ValueError):
                return jsonify({"hata": "lat ve lon gerekli"}), 400
            self.konum_geldi(lat, lon, dogruluk)
            return jsonify({"tamam": True})

        return app

    def baslat(self):
        from werkzeug.serving import make_server
        sunucular = [("panel", make_server("0.0.0.0", PANEL_PORT, self.app, threaded=True))]
        try:
            cert, key = sertifika_hazirla()
            sunucular.append(("telefon", make_server("0.0.0.0", TELEFON_PORT, self.app, threaded=True,
                                                     ssl_context=(cert, key))))
        except Exception as e:
            print(f"⚠️  HTTPS açılamadı, telefon konum sayfası çalışmayacak: {e}")
        for ad, s in sunucular:
            threading.Thread(target=s.serve_forever, daemon=True, name=f"panel-{ad}").start()
        ipler = [ip for ip in ip_adresleri() if ":" not in ip] or ["<pi-ip>"]
        print(f"📊 Jüri paneli:   http://{ipler[0]}:{PANEL_PORT}/")
        print(f"📱 Telefon konumu: https://{ipler[0]}:{TELEFON_PORT}/telefon")
        return self


# ═════════════ SAYFALAR ═════════════
PANEL_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Asistif Gözlük Paneli</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:#0f1115;--kart:#181b22;--cizgi:#2a2f3a;--yazi:#e8eaf0;--soluk:#8b93a5;--kirmizi:#e5484d;--yesil:#30a46c;--sari:#f5d90a;--mavi:#3e8ef7;--mor:#8e4ec6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--yazi);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--cizgi);flex-wrap:wrap}
h1{font-size:18px;margin:0 12px 0 0}.rozet{padding:4px 10px;border-radius:999px;background:var(--kart);border:1px solid var(--cizgi);font-size:13px}
.rozet b{font-weight:600}.iyi{color:var(--yesil)}.kotu{color:var(--kirmizi)}
main{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:14px;padding:14px}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.kart{background:var(--kart);border:1px solid var(--cizgi);border-radius:12px;padding:12px;min-width:0}
.kart h2{font-size:13px;color:var(--soluk);margin:0 0 8px;text-transform:uppercase;letter-spacing:.05em}
#video{width:100%;border-radius:8px;background:#000;aspect-ratio:4/3;object-fit:contain}
.sutun{display:flex;flex-direction:column;gap:14px;min-width:0}
#mesafe{font-size:42px;font-weight:700}#seviye{font-size:16px;font-weight:600;margin-left:10px}
canvas{width:100%;height:120px}
#akis{height:300px;overflow:auto;display:flex;flex-direction:column;gap:6px}
.satir{padding:6px 9px;border-radius:8px;background:#11141a;border-left:3px solid var(--cizgi)}
.satir small{color:var(--soluk);margin-right:6px}
.k-kullanici{border-color:var(--mavi)}.k-gozluk{border-color:var(--yesil)}.k-olay{border-color:var(--sari)}
.k-nav{border-color:var(--mor)}.k-hafiza{border-color:#12a594}.k-uyari{border-color:var(--kirmizi)}
.esyalar{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.esya{background:#11141a;border-radius:8px;padding:8px}.esya b{display:block}.esya span{color:var(--soluk);font-size:12px}
#harita{height:260px;border-radius:8px}
.nesneler{display:flex;flex-wrap:wrap;gap:6px}.nesne{padding:3px 8px;border-radius:999px;background:#11141a;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:3px 0;border-bottom:1px solid var(--cizgi)}td:last-child{text-align:right}
</style></head><body>
<header><h1>Asistif Gözlük</h1>
<span class="rozet">Mod: <b id="mod">—</b></span>
<span class="rozet">Yapay zekâ: <b id="ai">—</b></span>
<span class="rozet">İnternet: <b id="net">—</b></span>
<span class="rozet">Sensör: <b id="sensor">—</b></span>
<span class="rozet">Konuşma: <b id="duzey">—</b></span>
<span class="rozet">Gözcü: <b id="gozcu">—</b></span></header>
<main>
<div class="sutun">
 <div class="kart"><h2>Gözlüğün gördüğü (YOLO, cihaz üstünde)</h2><img id="video" src="/video" alt="kamera">
  <div class="nesneler" id="nesneler" style="margin-top:8px"></div></div>
 <div class="kart"><h2>Kameradan mesafe (derinlik yapay zekâsı, cihaz üstünde)</h2>
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start">
   <img id="derinlik" src="/derinlik" alt="derinlik haritası" style="width:min(320px,100%);border-radius:8px;background:#000">
   <div style="flex:1;min-width:180px"><div style="color:var(--soluk);font-size:12px;margin-bottom:6px">Kırmızı yakın, mavi uzak</div>
    <table id="mesafeler"></table><div id="olcek" style="color:var(--soluk);font-size:12px;margin-top:6px"></div></div>
  </div></div>
 <div class="kart"><h2>Konuşma ve olaylar</h2><div id="akis"></div></div>
</div>
<div class="sutun">
 <div class="kart"><h2 id="sensorbaslik">Lazer mesafe sensörü</h2>
  <div><span id="mesafe">—</span><span id="seviye"></span></div>
  <div style="color:var(--soluk)">Yaklaşma hızı <b id="hiz">—</b> · Çarpışmaya <b id="ttc">—</b></div>
  <canvas id="grafik" width="600" height="120"></canvas></div>
 <div class="kart"><h2>Eşya hafızası (sadece metin, görüntü saklanmaz)</h2><div class="esyalar" id="esyalar"></div></div>
 <div class="kart"><h2>Navigasyon</h2><div id="navyazi" style="margin-bottom:8px;color:var(--soluk)">Rota yok</div><div id="harita"></div></div>
 <div class="kart"><h2>Ölçümler</h2><table id="olcumler"></table></div>
</div></main>
<script>
const $=id=>document.getElementById(id);
const ESYALAR=["anahtar","cüzdan","telefon","bilgisayar"];
let hafiza={}, olcum={}, seri=[];
function rozet(id,metin,iyi){const e=$(id);e.textContent=metin;e.className=iyi===undefined?"":(iyi?"iyi":"kotu")}
function akisa(tur,metin){const d=document.createElement("div");d.className="satir k-"+tur;
 const s=new Date().toLocaleTimeString("tr-TR");d.innerHTML=`<small>${s}</small>`;d.append(metin);
 const a=$("akis");a.append(d);while(a.children.length>200)a.firstChild.remove();a.scrollTop=a.scrollHeight}
function esyaCiz(){$("esyalar").innerHTML=ESYALAR.map(a=>{const k=hafiza[a];
 return `<div class="esya"><b>${a[0].toUpperCase()+a.slice(1)}</b>${k?`${k.konum}<br><span>${k.once||"az önce"} · ${k.kaynak}</span>`:"<span>henüz görülmedi</span>"}</div>`}).join("")}
function olcumCiz(){$("olcumler").innerHTML=Object.entries(olcum).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join("")}
const harita=L.map("harita").setView([37.7383,29.1072],16);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"}).addTo(harita);
let isaret=L.circleMarker([0,0],{radius:8,color:"#3e8ef7",fillOpacity:.9}),cizgi=null,hedef=null;
function navCiz(n){if(!n)return;const k=n.konum||n.yedek_konum;if(k){isaret.setLatLng(k).addTo(harita)}
 if(cizgi){cizgi.remove();cizgi=null}if(hedef){hedef.remove();hedef=null}
 if(n.rota){cizgi=L.polyline(n.rota.cizgi,{color:"#8e4ec6",weight:5}).addTo(harita);hedef=L.marker(n.rota.hedef).addTo(harita);
  $("navyazi").textContent=`${n.rota.hedef_adi} · ${Math.round(n.rota.toplam_m)} m · sonraki: ${n.rota.sonraki} · ${n.rota.kaynak}`;
  if(!harita._ilk){harita.fitBounds(cizgi.getBounds(),{padding:[20,20]});harita._ilk=1}}
 else{$("navyazi").textContent=(n.gps_var?"GPS: telefon":"GPS yok (test konumu)")+" · rota yok";harita._ilk=0;if(k)harita.panTo(k)}}
function grafikCiz(){const c=$("grafik"),g=c.getContext("2d"),W=c.width,H=c.height;g.clearRect(0,0,W,H);
 g.strokeStyle="#2a2f3a";for(let m=0.5;m<=2;m+=0.5){const y=H-m/2.2*H;g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();g.fillStyle="#8b93a5";g.fillText(m+" m",4,y-2)}
 const simdi=Date.now()/1000;g.lineWidth=2;g.beginPath();let ilk=true;
 for(const [t,d,s] of seri){if(d==null){ilk=true;continue}const x=W-(simdi-t)/30*W,y=H-Math.min(d,2.2)/2.2*H;
  g.strokeStyle=s==2?"#e5484d":s==1?"#f5d90a":"#30a46c";if(ilk){g.moveTo(x,y);ilk=false}else g.lineTo(x,y)}g.stroke()}
function isle(m){const v=m.veri;switch(m.tur){
 case"durum":rozet("mod",v.mod==="home"?"Ev":"Sokak");rozet("ai",v.ai,v.ai&&v.ai.startsWith("Live"));
  rozet("net",v.internet?"var":"yok",v.internet);rozet("duzey",v.duzey+(v.mod!=="home"?` · anlatıcı ${v.siklik}`:""));
  if(v.hafiza){hafiza=v.hafiza;esyaCiz()}if(v.nav)navCiz(v.nav);
  if(v.gozcu)rozet("gozcu",`${v.gozcu.gozcu} kare · ${v.gozcu.bildir} bildirim · ${v.gozcu.esya_kaydet} eşya`);break;
 case"sensor":$("mesafe").textContent=v.mesafe==null?"> 2 m":v.mesafe.toFixed(2)+" m";
  $("seviye").textContent=["","YAKLAŞIYOR","DUR"][v.seviye];$("seviye").style.color=["","#f5d90a","#e5484d"][v.seviye];
  $("sensorbaslik").textContent=v.kaynak&&v.kaynak.startsWith("kamera")?"Mesafe (sensör yok: kameradan)":"Lazer mesafe sensörü";
  $("hiz").textContent=v.hiz.toFixed(2)+" m/s";$("ttc").textContent=v.ttc==null?"—":v.ttc.toFixed(1)+" s";
  rozet("sensor",v.arizali?"arızalı":v.kaynak,!v.arizali);seri.push([m.zaman,v.mesafe,v.seviye]);
  seri=seri.filter(s=>m.zaman-s[0]<30);grafikCiz();break;
 case"nesneler":$("nesneler").innerHTML=v.nesneler.map(n=>`<span class="nesne">${n.ad} ${Math.round(n.guven*100)}%</span>`).join("");
  olcum["YOLO süresi"]=v.sure_ms+" ms";olcumCiz();break;
 case"kullanici":akisa("kullanici","🎤 "+v);break;
 case"gozluk":akisa("gozluk","📢 "+v);break;
 case"olay":akisa("olay","👁️ "+v);break;
 case"nav":navCiz(v);break;case"nav_duyuru":akisa("nav","🧭 "+v);break;
 case"hafiza":hafiza[v.esya]=v.kayit;esyaCiz();akisa("hafiza",`🧠 ${v.esya}: ${v.kayit.konum}`);break;
 case"uyari":akisa("uyari","🛑 "+v);break;
 case"olcum":olcum[v.tur]=v.deger+" "+v.birim;olcumCiz();break
 case"mesafe":$("mesafeler").innerHTML=`<tr><td><b>Tam önde</b></td><td><b>${v.tam_on.toFixed(1)} m</b></td></tr>`+
  v.nesneler.slice(0,6).map(n=>`<tr><td>${n.ad} · saat ${n.saat}</td><td>${n.mesafe.toFixed(1)} m${n.kaynak.includes("boy")?" ✓":""}</td></tr>`).join("");
  $("olcek").textContent=`${v.model==="ev"?"İç mekân":"Dış mekân"} modeli · kalibrasyon ×${v.olcek} · ${v.sure_ms} ms`;
  olcum["Derinlik süresi"]=v.sure_ms+" ms";olcumCiz();break}}
esyaCiz();const es=new EventSource("/olaylar");es.onmessage=e=>isle(JSON.parse(e.data));
setInterval(()=>fetch("/durum").then(r=>r.json()).then(v=>isle({tur:"durum",veri:v})).catch(()=>{}),5000);
</script></body></html>"""

TELEFON_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Gözlük Kumanda</title>
<style>
:root{--bg:#0f1115;--kart:#181b22;--yazi:#e8eaf0;--soluk:#8b93a5;--yesil:#30a46c;--kirmizi:#e5484d;--mavi:#3e8ef7;--mor:#8e4ec6;--gri:#3a3f4b}
*{box-sizing:border-box;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--yazi);font:17px/1.4 system-ui,-apple-system,sans-serif;padding:14px 16px 24px;text-align:center}
h1{font-size:19px;margin:2px 0 10px}
button{font:inherit;font-weight:700;border:0;border-radius:16px;color:#fff;width:100%;touch-action:manipulation}
button:active{filter:brightness(1.25)}
.izgara{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
.genis{grid-column:1/3}
.buyuk{padding:24px;font-size:21px}
.orta{padding:18px}
#onumde{background:var(--yesil)}#konus{background:var(--mavi);touch-action:none}#konus.basili{background:var(--kirmizi)}
#tekrar,#sustur{background:var(--gri)}#ev,#sokak{background:var(--mor)}#ev.secili,#sokak.secili{outline:3px solid #fff}
#konum{background:var(--gri);padding:16px}#konum.acik{background:var(--yesil)}
.bilgi{font-size:14px;color:var(--soluk);min-height:20px;margin-top:6px}
#soylenen{background:var(--kart);border-radius:12px;padding:12px;margin-top:10px;text-align:left;min-height:48px;font-size:16px;-webkit-user-select:text;user-select:text}
</style></head><body>
<h1>Gözlük kumandası</h1>
<div class="izgara">
 <button id="onumde" class="buyuk genis">Önümde ne var?</button>
 <button id="konus" class="buyuk genis">Basılı tut ve konuş</button>
 <button id="tekrar" class="orta">Tekrar</button>
 <button id="sustur" class="orta">Sustur</button>
 <button id="ev" class="orta">Ev modu</button>
 <button id="sokak" class="orta">Sokak modu</button>
</div>
<div class="bilgi" id="durum">&nbsp;</div>
<div id="soylenen">Gözlüğün söyledikleri burada görünecek.</div>
<button id="konum" style="margin-top:14px">Konum paylaşımını aç (navigasyon için)</button>
<div class="bilgi" id="gps"></div>
<script>
const $=id=>document.getElementById(id);
// Her istek en fazla 4 sn bekler: takılan bir istek diğer düğmeleri kilitlemesin
function istek(yol,veri){const ctl=new AbortController();const z=setTimeout(()=>ctl.abort(),4000);
 return fetch(yol,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(veri),signal:ctl.signal}).finally(()=>clearTimeout(z))}
function gonder(olay,etiket){$("durum").textContent="… "+etiket;
 istek("/api/dugme",{olay}).then(r=>{$("durum").textContent=r.ok?"✓ "+etiket:"Gözlük cevap vermedi"})
  .catch(()=>{$("durum").textContent="Gözlüğe ulaşılamadı"})}
function titret(ms){if(navigator.vibrate)navigator.vibrate(ms)}
$("onumde").onclick=()=>{titret(30);gonder("kisa","Önümde ne var")};
$("tekrar").onclick=()=>{titret(30);gonder("cift","Tekrar")};
$("sustur").onclick=()=>{titret(30);gonder("sustur","Sustur")};
$("ev").onclick=()=>{titret(30);gonder("ev","Ev modu");modGoster("home")};
$("sokak").onclick=()=>{titret(30);gonder("sokak","Sokak modu");modGoster("street")};
function modGoster(m){$("ev").classList.toggle("secili",m==="home");$("sokak").classList.toggle("secili",m==="street")}
// Basılı tut ve konuş: parmak kayınca kesilmesin diye işaretçi yakalanır
let basili=false;const k=$("konus");
function bas(e){e.preventDefault();if(basili)return;basili=true;try{k.setPointerCapture(e.pointerId)}catch(_){}
 k.classList.add("basili");k.textContent="Dinliyorum… bırakınca biter";titret(40);gonder("uzun_basladi","Dinliyor")}
function birak(e){if(e)e.preventDefault();if(!basili)return;basili=false;k.classList.remove("basili");
 k.textContent="Basılı tut ve konuş";titret(20);gonder("uzun_bitti","Soru gönderildi")}
k.addEventListener("pointerdown",bas);["pointerup","pointercancel"].forEach(o=>k.addEventListener(o,birak));
k.addEventListener("contextmenu",e=>e.preventDefault());
document.addEventListener("visibilitychange",()=>{if(document.hidden)birak()});
// Gözlüğün söyledikleri (canlı)
try{const es=new EventSource("/olaylar");es.onmessage=e=>{const m=JSON.parse(e.data);
 if(m.tur==="gozluk"||m.tur==="uyari"||m.tur==="nav_duyuru")$("soylenen").textContent=(m.tur==="uyari"?"🚧 ":m.tur==="nav_duyuru"?"🧭 ":"📢 ")+m.veri;
 if(m.tur==="durum")modGoster(m.veri.mod)}}catch(_){}
// Konum paylaşımı: aç/kapat; aynı anda tek istek, saniyede en fazla bir
let izleyici=null,kilit=null,sayac=0,gonderiliyor=false,sonGonderim=0;
async function ekranAcik(){try{kilit=await navigator.wakeLock.request("screen")}catch(_){}}
function konumGonder(c){const simdi=Date.now();if(gonderiliyor||simdi-sonGonderim<1000)return;
 gonderiliyor=true;sonGonderim=simdi;
 istek("/api/konum",{lat:c.latitude,lon:c.longitude,dogruluk:c.accuracy}).then(()=>{sayac++})
  .catch(()=>{}).finally(()=>{gonderiliyor=false;$("gps").textContent=`Konum gidiyor · ±${Math.round(c.accuracy)} m · ${sayac}`})}
$("konum").onclick=()=>{
 if(izleyici!==null){navigator.geolocation.clearWatch(izleyici);izleyici=null;
  if(kilit){kilit.release().catch(()=>{});kilit=null}
  $("konum").classList.remove("acik");$("konum").textContent="Konum paylaşımını aç (navigasyon için)";$("gps").textContent="Konum kapalı";return}
 if(!navigator.geolocation){$("gps").textContent="Bu tarayıcı konum vermiyor";return}
 ekranAcik();$("gps").textContent="Konum izni bekleniyor…";
 izleyici=navigator.geolocation.watchPosition(p=>konumGonder(p.coords),
  e=>{$("gps").textContent="Konum alınamadı: "+e.message},{enableHighAccuracy:true,maximumAge:1000,timeout:15000});
 $("konum").classList.add("acik");$("konum").textContent="Konum paylaşılıyor · kapatmak için dokun"};
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible"&&izleyici!==null)ekranAcik()});
</script></body></html>"""
