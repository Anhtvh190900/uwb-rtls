"""Ve map tu graph.hml.

Pipeline:
  1. Gom canh thanh chuoi lane, lam muot: doan thang giu 2 node dau/cuoi,
     doan cong fit cung tron R qua 5 diem (dau, 1/4, giua, 3/4, cuoi)  -> smooth.py
  2. Do be rong: co centerline song song cach ~0.38 m ben canh => lan doi 38cm,
     khong co => lan don 60cm.
  3. Offset tam lane ra 2 ben lay duong bien; bo doan de len long duong khac
     hoac trung doan da ve.
  4. Noi cac doan con lai thanh chuoi: goc lom cat tai giao diem, goc loi tram
     khe bang clothoid  -> clothoid.py
"""
import io
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
import networkx as nx
from scipy.spatial import cKDTree

import clothoid
import smooth

SRC = sys.argv[1] if len(sys.argv) > 1 else "graph.hml"
OUT = sys.argv[2] if len(sys.argv) > 2 else "map.png"
ROT = int(sys.argv[sys.argv.index("--rot") + 1]) if "--rot" in sys.argv else 90
FLIP = sys.argv[sys.argv.index("--flip") + 1] if "--flip" in sys.argv else "h"
PREVIEW = "--preview" in sys.argv      # xuat them anh centerline da lam muot
JSON_OUT = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
CTRL = sys.argv[sys.argv.index("--ctrl") + 1] if "--ctrl" in sys.argv else None
NO_BORDER = "--no-border" in sys.argv   # chi ve centerline, bo qua duong bien

W_DOUBLE = 0.38                    # be rong 1 lan khi la lan doi
W_SINGLE = 0.60                    # be rong khi la lan don
PAIR_MIN, PAIR_MAX = 0.30, 0.46    # khoang cach 2 centerline cua lan doi
COS_PARALLEL = 0.94
STEP = 0.04                        # buoc lay mau doc theo doan
CLEAR = 0.16                       # bien cach centerline khac < nguong => de len duong
GRID = 0.05                        # o luoi de bo doan trung nhau
JOIN_GAP = 0.60                    # khe toi da con noi lai
JOIN_ANG = np.deg2rad(75)          # goc re toi da con coi la lien tuc

# ================= 1. Doc graph va lam muot centerline =================
with open(SRC, "r", encoding="utf-8") as f:
    cleaned = "".join(l for l in f if not l.lstrip().startswith("@"))
G = nx.read_graphml(io.StringIO(cleaned))
pos = {n: np.array([float(d["x"]), float(d["y"])]) for n, d in G.nodes(data=True)}

if CTRL:
    ctrl = {t.strip() for t in open(CTRL).read().replace("\n", ",").split(",") if t.strip()}
    lanes, n_line, radii, n_ref, n_auto, auto_len, detail = smooth.smooth_graph_ctrl(G, pos, ctrl)
    print(f"node dieu khien: {len(ctrl)}  |  cung dung node tham chieu: {n_ref}  |  "
          f"doan cong thieu tham chieu, phai lam muot tu dong: {n_auto} ({auto_len:.1f} m)")
    miss = [d for d in detail if not d[5]]
    if miss:
        print("  can them node tham chieu o cac doan cong:")
        for a, b, k, _, L, _ in sorted(miss, key=lambda d: -d[4]):
            print(f"    {a:>4} -> {b:>4}  dai {L:5.2f} m  moc hien co: {k}")
else:
    lanes, n_line, radii = smooth.smooth_graph(G, pos)
print(f"chuoi lane: {len(lanes)}  |  doan thang: {n_line}  |  cung tron: {len(radii)}"
      + (f"  (R {min(radii):.2f}..{max(radii):.2f} m)" if radii else ""))

A, B, lane_id, order = [], [], [], []
for li, P in enumerate(lanes):
    for k in range(len(P) - 1):
        A.append(P[k]); B.append(P[k + 1]); lane_id.append(li); order.append(k)
A = np.array(A); B = np.array(B)
lane_id = np.array(lane_id); order = np.array(order)
D = B - A
Lg = np.linalg.norm(D, axis=1)
T = D / Lg[:, None]
N = np.stack([-T[:, 1], T[:, 0]], axis=1)      # phap tuyen trai
E = len(A)

polylines = []
if not NO_BORDER:
    # ================= 2. Be rong lan =================
    pa = np.einsum("ijk,ik->ij", A[None] - A[:, None], T)
    pb = np.einsum("ijk,ik->ij", B[None] - A[:, None], T)
    ovl = np.minimum(np.maximum(pa, pb), Lg[:, None]) - np.maximum(np.minimum(pa, pb), 0.0)
    qa = np.einsum("ijk,ik->ij", A[None] - A[:, None], N)
    qb = np.einsum("ijk,ik->ij", B[None] - A[:, None], N)
    q = (qa + qb) / 2.0

    ok = (np.abs(T @ T.T) > COS_PARALLEL) & (np.abs(qa - qb) < 0.10) & (ovl > 0.02)
    ok &= (np.abs(q) > PAIR_MIN) & (np.abs(q) < PAIR_MAX)
    np.fill_diagonal(ok, False)

    side = np.zeros(E)
    for i in np.where(ok.any(axis=1))[0]:
        j = np.where(ok[i])[0]
        side[i] = np.sign(q[i, j[np.argmin(np.abs(q[i, j]))]])     # phia co lan ke ben

    for li in range(len(lanes)):                   # loc nhieu: bo phieu theo cua so 5 doan
        m = np.where(lane_id == li)[0]
        if len(m) >= 5:
            s = side[m]
            pad = np.pad(s, 2, mode="edge")
            side[m] = [np.sign(np.sum(np.sign(pad[k:k + 5]))) or s[k] for k in range(len(s))]
    width = np.where(side != 0, W_DOUBLE, W_SINGLE)
    print(f"doan lan doi: {int((side != 0).sum())}  |  lan don: {int((side == 0).sum())} / {E}")

    def sample(a, b):
        n = max(2, int(np.ceil(np.linalg.norm(b - a) / STEP)) + 1)
        return a + (b - a) * np.linspace(0, 1, n)[:, None]

    # ================= 3. Offset ra bien, bo doan de len long duong khac =================
    center_tree = cKDTree(np.vstack([sample(A[i], B[i]) for i in range(E)]))

    kept = {}
    drop_overlap = 0
    for i in range(E):
        off = N[i] * width[i] / 2.0
        for sg in (+1, -1):
            a, b = A[i] + sg * off, B[i] + sg * off
            if center_tree.query(sample(a, b))[0].min() < CLEAR:
                drop_overlap += 1                      # nam trong long duong khac
                continue
            kept[(i, sg)] = (a, b, side[i] == sg)
    print(f"doan offset: {2 * E}  |  bo do de len duong: {drop_overlap}  |  giu: {len(kept)}")

    # ================= 4. Noi thanh chuoi, bo chuoi trung lap =================
    def join(run, sg):
        """Noi cac doan lien tiep: goc lom cat tai giao diem, goc loi tram clothoid."""
        global n_arc, n_miter
        pts = [kept[(run[0], sg)][0], kept[(run[0], sg)][1]]
        for p, j in zip(run, run[1:]):
            b0, t0 = kept[(p, sg)][1], T[p]
            a1, t1 = kept[(j, sg)][0], T[j]
            v = a1 - b0
            if np.linalg.norm(v) < 1e-9:
                pass
            elif np.dot(v, t0) < 0.0:                  # goc lom -> cat tai giao diem
                den = t0[0] * t1[1] - t0[1] * t1[0]
                if abs(den) > 1e-9:
                    x = b0 + (v[0] * t1[1] - v[1] * t1[0]) / den * t0
                    if max(np.linalg.norm(x - b0), np.linalg.norm(x - a1)) < JOIN_GAP:
                        pts[-1] = x; a1 = x; n_miter += 1
            else:                                      # goc loi -> tram bang clothoid
                pts.extend(clothoid.fit(b0, t0, a1, t1)[1:-1]); n_arc += 1
                gaps.append(float(np.linalg.norm(v)))
            pts.extend([a1, kept[(j, sg)][1]])
        return np.array(pts)

    runs = []
    n_arc = n_miter = 0
    gaps = []
    for li in range(len(lanes)):
        idx = list(np.where(lane_id == li)[0])
        for sg in (+1, -1):
            cur = []
            for i in idx + [None]:
                if i is not None and (i, sg) in kept:
                    cur.append(i)
                    continue
                if cur:
                    runs.append((join(cur, sg), kept[(cur[0], sg)][2]))
                cur = []

    # chuoi dai ve truoc; chuoi nao bi chuoi khac phu gan het thi bo
    length = lambda p: float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    runs.sort(key=lambda r: -length(r[0]))
    polylines, taken, drop_dup = [], set(), 0
    for pts, is_div in runs:
        dense = np.vstack([sample(a, b) for a, b in zip(pts[:-1], pts[1:])])
        cells = {(int(np.floor(x / GRID)), int(np.floor(y / GRID))) for x, y in dense}
        if len(cells - taken) < 0.35 * len(cells):
            drop_dup += 1
            continue
        taken |= cells
        polylines.append((pts, is_div))
    print(f"chuoi bien: {len(polylines)} (bo trung lap {drop_dup})  |  goc lom cat giao diem: "
          f"{n_miter}  |  khe tram clothoid: {n_arc}"
          + (f"  (lon nhat {max(gaps):.3f} m)" if gaps else ""))


# ================= 5. Ve =================
P = np.array(list(pos.values()))
xmax, ymax = P[:, 0].max(), P[:, 1].max()
def rot(arr, k, xmax=xmax, ymax=ymax):
    for _ in range(k):
        arr = np.stack([ymax - arr[..., 1], arr[..., 0]], axis=-1)
        xmax, ymax = ymax, xmax
    return arr
k = ROT // 90
border = [rot(p, k) for p, d in polylines if not d]
divider = [rot(p, k) for p, d in polylines if d]
lanes_r = [rot(p, k) for p in lanes]
Pr = rot(P, k)

def canvas(pts):
    w, h = np.ptp(pts[:, 0]), np.ptp(pts[:, 1])
    s = 15.0 / max(w, h)
    fig, ax = plt.subplots(figsize=(w * s + 1.2, h * s + 1.2), dpi=150)
    ax.set_aspect("equal"); ax.invert_yaxis()
    if FLIP == "h":
        ax.invert_xaxis()
    elif FLIP == "v":
        ax.invert_yaxis()
    # sau khi xoay, hoanh do ve la (ymax - y); doi nhan tick ve gia tri that
    if ROT == 90:
        ax.xaxis.set_major_formatter(lambda v, _: f"{ymax - v:g}")
    ax.set_xlabel("y [m]" if ROT in (90, 270) else "x [m]")
    ax.set_ylabel("x [m]" if ROT in (90, 270) else "y [m]")
    ax.grid(True, linewidth=.3, alpha=.35)
    return fig, ax

fig, ax = canvas(Pr)
if not NO_BORDER:
    ax.add_collection(LineCollection(border, colors="#111111", linewidths=2.0,
                                     capstyle="round", joinstyle="round",
                                     label=f"bien duong ({len(border)})"))
    ax.add_collection(LineCollection(divider, colors="#e0a800", linewidths=1.6,
                                     capstyle="round", joinstyle="round",
                                     label=f"vach giua ({len(divider)})"))
if NO_BORDER:
    ax.add_collection(LineCollection(lanes_r, colors="#c0392b", linewidths=1.4,
                                     label=f"tam lane ({len(lanes_r)} chuoi)"))
    ax.scatter(Pr[:, 0], Pr[:, 1], s=4, c="#9aa5a3", zorder=3, label=f"node ({len(Pr)})")
    if CTRL:
        C = rot(np.array([pos[n] for n in ctrl if n in pos]), k)
        ax.scatter(C[:, 0], C[:, 1], s=26, facecolor="none", edgecolor="#0d7a86",
                   linewidths=1.3, zorder=4, label=f"node dieu khien ({len(C)})")
else:
    ax.add_collection(LineCollection(lanes_r, colors="#4a90d9", linewidths=.7, alpha=.45,
                                     linestyles=(0, (5, 5)), label="tam lane"))
ax.autoscale_view()
ax.set_title(f"{SRC}  -  {n_line} doan thang + {len(radii)} cung tron"
             if NO_BORDER else
             f"{SRC}  -  lan doi {W_DOUBLE*100:.0f}cm/lan, lan don {W_SINGLE*100:.0f}cm")
ax.legend(loc="upper right", fontsize=8)
fig.tight_layout(); fig.savefig(OUT); print("Saved", OUT)

if PREVIEW:
    fig, ax = canvas(Pr)
    ax.scatter(Pr[:, 0], Pr[:, 1], s=5, c="#bbbbbb", zorder=2, label="node goc")
    ax.add_collection(LineCollection(lanes_r, colors="#d62728", linewidths=1.3,
                                     label=f"centerline da lam muot ({len(lanes_r)} chuoi)"))
    ax.autoscale_view()
    ax.set_title(f"{SRC}  -  {n_line} doan thang + {len(radii)} cung tron")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout(); pv = "graph_ctrl.png" if CTRL else "graph_smooth.png"
    fig.savefig(pv); print("Saved", pv)

def rdp(P, eps=0.005):
    """Giam so diem polyline (Douglas-Peucker) truoc khi xuat JSON."""
    P = np.asarray(P, float)
    if len(P) < 3:
        return P
    d = P[-1] - P[0]
    n = np.linalg.norm(d)
    if n < 1e-12:
        dist = np.linalg.norm(P - P[0], axis=1)
    else:
        dist = np.abs(np.cross(d / n, P - P[0]))
    i = int(np.argmax(dist))
    if dist[i] <= eps:
        return P[[0, -1]]
    return np.vstack([rdp(P[:i + 1], eps)[:-1], rdp(P[i:], eps)])


if JSON_OUT:
    import json
    ids = list(pos)
    nd = rot(np.array([pos[n] for n in ids]), k)
    data = {
        "nodes": [{"id": n, "x": float(pos[n][0]), "y": float(pos[n][1]),
                   "dx": float(a), "dy": float(b)} for n, (a, b) in zip(ids, nd)],
        "edges": [[u, v] for u, v in G.edges()],
        "border": [np.round(rdp(p), 4).tolist() for p in border],
        "divider": [np.round(rdp(p), 4).tolist() for p in divider],
        "lanes": [np.round(rdp(p), 4).tolist() for p in lanes_r],
    }
    with open(JSON_OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print("Saved", JSON_OUT)
