"""
3D model yardımcıları (manifold3d).

Kurulum (modelleri yeniden üretmek için, Windows'ta da çalışır):
    pip install manifold3d numpy matplotlib

Birimler milimetre. Eksenler: +X sağ, +Y ileri (gözlükte bakış yönü), +Z yukarı.
"""

import os
import struct

import numpy as np
from manifold3d import CrossSection, JoinType, Manifold, set_circular_segments

set_circular_segments(48)

CIKTI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stl")


def kutu(x, y, z, merkez=True):
    return Manifold.cube([x, y, z], center=merkez)


def yuvarlak_kutu(x, y, z, r):
    """Köşeleri r yarıçapıyla yuvarlatılmış kutu (merkezli)."""
    r = min(r, x / 2 - 0.01, y / 2 - 0.01, z / 2 - 0.01)
    kure = Manifold.sphere(r, 24)
    noktalar = [kure.translate([sx * (x / 2 - r), sy * (y / 2 - r), sz * (z / 2 - r)])
                for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    return Manifold.batch_hull(noktalar)


def silindir(h, r, merkez=True, r2=None):
    return Manifold.cylinder(h, r, r if r2 is None else r2, 0, merkez)


def yuvarlak_dikdortgen(w, h, r):
    """2D yuvarlatılmış dikdörtgen (merkezli)."""
    return CrossSection.square([w - 2 * r, h - 2 * r], center=True).offset(r, JoinType.Round)


def cokgen(noktalar):
    return CrossSection([noktalar])


def temizle(model, en_az_hacim=1.0):
    """Boolean işlemlerinden kalan hacimsiz kırıntıları atar."""
    parcalar = [p for p in model.decompose() if p.volume() >= en_az_hacim]
    return Manifold.compose(parcalar) if len(parcalar) != 1 else parcalar[0]


def tablaya(model, donus=(0, 0, 0)):
    """Baskı yönüne çevirir ve tablaya oturtur: en alt nokta z=0, xy merkezde."""
    m = model.rotate(list(donus))
    x0, y0, z0, x1, y1, z1 = m.bounding_box()
    return m.translate([-(x0 + x1) / 2, -(y0 + y1) / 2, -z0])


def stl_yaz(model, ad):
    """İkili STL yazar, dosya yolunu döner."""
    os.makedirs(CIKTI, exist_ok=True)
    yol = os.path.join(CIKTI, ad + ".stl")
    mesh = model.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    with open(yol, "wb") as f:
        f.write(ad.encode()[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(t)))
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        n = np.cross(b - a, c - a)
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        kayit = np.zeros(len(t), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3),
                                         ("c", "<f4", 3), ("attr", "<u2")])
        kayit["n"], kayit["a"], kayit["b"], kayit["c"] = n, a, b, c
        f.write(kayit.tobytes())
    return yol


def bilgi(model, ad):
    (x0, y0, z0, x1, y1, z1) = model.bounding_box()
    durum = "tek parça, kapalı" if model.status().name == "NoError" and model.genus() >= 0 else str(model.status())
    parca = len(model.decompose())
    print(f"  {ad:28} {x1 - x0:6.1f} × {y1 - y0:6.1f} × {z1 - z0:6.1f} mm | "
          f"hacim {model.volume() / 1000:6.1f} cm³ | parça sayısı {parca} | {durum}")


def onizleme(modeller, yol, gorunumler=((20, -60), (15, 30), (0, -90), (90, -90)), baslik=""):
    """Birkaç açıdan gölgeli önizleme PNG'si. modeller: [(Manifold, renk), ...]"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    isik = np.array([0.3, -0.5, 0.8])
    isik /= np.linalg.norm(isik)
    fig = plt.figure(figsize=(5 * len(gorunumler), 5))
    tum = np.concatenate([np.asarray(m.to_mesh().vert_properties)[:, :3] for m, _ in modeller])
    orta = (tum.max(0) + tum.min(0)) / 2
    yari = (tum.max(0) - tum.min(0)).max() / 2
    for i, (elev, azim) in enumerate(gorunumler):
        ax = fig.add_subplot(1, len(gorunumler), i + 1, projection="3d")
        for m, renk in modeller:
            mesh = m.to_mesh()
            v = np.asarray(mesh.vert_properties)[:, :3]
            t = np.asarray(mesh.tri_verts)
            ucgen = v[t]
            n = np.cross(ucgen[:, 1] - ucgen[:, 0], ucgen[:, 2] - ucgen[:, 0])
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
            parlak = 0.35 + 0.65 * np.clip(np.abs(n @ isik), 0, 1)
            temel = np.array(matplotlib.colors.to_rgb(renk))
            ax.add_collection3d(Poly3DCollection(ucgen, facecolors=parlak[:, None] * temel,
                                                 edgecolors="none"))
        for eksen, o in zip((ax.set_xlim, ax.set_ylim, ax.set_zlim), orta):
            eksen(o - yari, o + yari)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev, azim)
        ax.set_axis_off()
    if baslik:
        fig.suptitle(baslik)
    plt.tight_layout()
    fig.savefig(yol, dpi=90)
    plt.close(fig)
    return yol
