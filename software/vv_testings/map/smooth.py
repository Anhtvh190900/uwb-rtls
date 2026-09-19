"""Lam muot centerline cua graph: doan thang giu 2 dau, doan cong fit cung tron.

- Gom cac canh thanh chuoi lane (tach tai nga re / nut giao).
- Trong 1 chuoi, cho nao goc quay ~0 la doan thang -> chi lay node dau & cuoi.
- Cho nao cong -> lay 5 diem (dau, 1/4, giua, 3/4, cuoi) fit ra tam & ban kinh R,
  roi sinh lai cung tron. Fit khong dat thi chia doi doan cong va fit lai.
"""
from collections import defaultdict

import numpy as np

ANG_STRAIGHT = np.deg2rad(4.0)   # goc quay duoi nguong nay coi la thang
ARC_TOL = 0.02                   # sai so toi da cua cung so voi node goc [m]
ARC_STEP = 0.04                  # buoc lay mau tren cung [m]
MAX_DEPTH = 4


def lane_chains(G, pos):
    """Gom canh thanh chuoi node; cat tai nut co nhieu hon 1 duong vao/ra."""
    succ = defaultdict(list); pred = defaultdict(list)
    for u, v in G.edges():
        succ[u].append(v); pred[v].append(u)
    flow = lambda n: len(succ[n]) == 1 and len(pred[n]) == 1

    seen, chains = set(), []
    def walk(u, v):
        chain = [u, v]; seen.add((u, v))
        while flow(v):
            w = succ[v][0]
            if (v, w) in seen:
                break
            chain.append(w); seen.add((v, w)); v = w
        return chain

    for u, v in G.edges():                    # bat dau tu cac nut khong phai giua chuoi
        if (u, v) not in seen and not flow(u):
            chains.append(walk(u, v))
    for u, v in G.edges():                    # phan con lai la vong kin
        if (u, v) not in seen:
            chains.append(walk(u, v))
    return [np.array([pos[n] for n in c]) for c in chains]


def _runs(P):
    """Chia chuoi diem thanh cac doan thang / cong lien tiep."""
    d = np.diff(P, axis=0)
    t = d / np.linalg.norm(d, axis=1, keepdims=True)
    if len(t) == 1:
        return [(0, 1, False)]
    turn = np.abs(np.arctan2(np.cross(t[:-1], t[1:]), np.einsum("ij,ij->i", t[:-1], t[1:])))
    curved = np.zeros(len(t), bool)            # canh i cong neu 1 trong 2 dau no gap
    curved[:-1] |= turn > ANG_STRAIGHT
    curved[1:] |= turn > ANG_STRAIGHT

    runs, i = [], 0
    while i < len(curved):
        j = i
        while j < len(curved) and curved[j] == curved[i]:
            j += 1
        runs.append((i, j, bool(curved[i])))   # canh [i, j) cung loai
        i = j
    return runs


def fit_circle(p):
    """Fit tam + ban kinh kieu Kasa (binh phuong toi thieu tuyen tinh)."""
    a = np.column_stack([2 * p[:, 0], 2 * p[:, 1], np.ones(len(p))])
    sol, *_ = np.linalg.lstsq(a, (p ** 2).sum(axis=1), rcond=None)
    c = sol[:2]
    r2 = sol[2] + c @ c
    return (c, np.sqrt(r2)) if r2 > 0 else (c, np.inf)


def _arc(P, depth=0):
    """Sinh cung tron cho mot doan cong; khong dat thi chia doi."""
    n = len(P) - 1
    five = P[[0, n // 4, n // 2, (3 * n) // 4, n]]   # dau, 1/4, giua, 3/4, cuoi
    c, R = fit_circle(five)

    if np.isfinite(R) and R < 1e3:
        err = np.abs(np.linalg.norm(P - c, axis=1) - R).max()
        if err <= ARC_TOL:
            a0 = np.arctan2(*(P[0] - c)[::-1])
            a1 = np.arctan2(*(P[-1] - c)[::-1])
            am = np.arctan2(*(P[n // 2] - c)[::-1])
            sweep = (a1 - a0) % (2 * np.pi)
            if not ((am - a0) % (2 * np.pi) < sweep):
                sweep -= 2 * np.pi              # di theo chieu qua diem giua
            m = max(2, int(abs(sweep) * R / ARC_STEP) + 1)
            ang = a0 + sweep * np.linspace(0, 1, m)
            pts = c + R * np.stack([np.cos(ang), np.sin(ang)], axis=1)
            pts[0], pts[-1] = P[0], P[-1]
            return pts, [R]

    if depth >= MAX_DEPTH or n < 4:
        return P, []                            # bo cuoc: giu nguyen node goc
    h = n // 2
    left, r1 = _arc(P[:h + 1], depth + 1)
    right, r2 = _arc(P[h:], depth + 1)
    return np.vstack([left, right[1:]]), r1 + r2


def smooth_chain(P):
    """Tra ve (diem da lam muot, so doan thang, danh sach R cua cac cung)."""
    out, n_line, radii = [P[0]], 0, []
    for i, j, curved in _runs(P):
        part = P[i:j + 1]
        if curved:
            pts, rr = _arc(part)
            radii += rr
        else:
            pts, n_line = part[[0, -1]], n_line + 1   # doan thang: chi 2 dau
        out.extend(pts[1:])
    return np.array(out), n_line, radii


def smooth_graph(G, pos):
    lanes, n_line, radii = [], 0, []
    for P in lane_chains(G, pos):
        s, nl, rr = smooth_chain(P)
        lanes.append(s); n_line += nl; radii += rr
    return lanes, n_line, radii


# ===================== Dung lai tu node dieu khien cho san =====================
SAG_TOL = 0.02       # do vong toi da van coi la doan thang [m]
ARC_ACCEPT = 0.05    # cung fit lech node goc qua nguong nay thi bo, quay ve tu dong


def _sagitta(P):
    """Do vong lon nhat cua chuoi diem so voi day cung."""
    d = P[-1] - P[0]
    n = np.linalg.norm(d)
    if n < 1e-12:
        return np.linalg.norm(P - P[0], axis=1).max()
    return np.abs(np.cross(d / n, P - P[0])).max()


def _arc_through(anchors, P):
    """Cung tron qua cac diem tham chieu; None neu lech node goc qua nhieu."""
    c, R = fit_circle(anchors)
    if not np.isfinite(R) or R > 1e3:
        return None
    if np.abs(np.linalg.norm(P - c, axis=1) - R).max() > ARC_ACCEPT:
        return None
    ang = lambda p: np.arctan2(*(p - c)[::-1])
    a0, a1, am = ang(anchors[0]), ang(anchors[-1]), ang(anchors[len(anchors) // 2])
    sweep = (a1 - a0) % (2 * np.pi)
    if not ((am - a0) % (2 * np.pi) < sweep):
        sweep -= 2 * np.pi
    m = max(2, int(abs(sweep) * R / ARC_STEP) + 1)
    t = a0 + sweep * np.linspace(0, 1, m)
    pts = c + R * np.stack([np.cos(t), np.sin(t)], axis=1)
    pts[0], pts[-1] = anchors[0], anchors[-1]
    return pts, R


def smooth_chain_ctrl(P, ids, ctrl):
    """Dung lai 1 chuoi tu node dieu khien.

    Ranh gioi thang/cong lay theo goc quay cua node goc; trong moi doan cong,
    2 dau doan cong cong voi cac node dieu khien nam trong do la diem tham chieu
    de fit ban kinh. Doan cong co duoi 3 diem tham chieu thi lam muot tu dong.

    Tra ve (diem, so doan thang, danh sach R, so cung dung node tham chieu,
            so doan phai lam muot tu dong, chieu dai cac doan do, chi tiet).
    """
    anchors = {k for k, n in enumerate(ids) if n in ctrl}
    out = [P[0]]
    n_line = n_ref = n_auto = 0
    radii, auto_len, detail = [], 0.0, []

    for a, b, curved in _runs(P):
        if not curved:
            out.append(P[b]); n_line += 1
            continue
        ks = [a] + sorted(k for k in anchors if a < k < b) + [b]
        L = float(np.linalg.norm(np.diff(P[a:b + 1], axis=0), axis=1).sum())
        res = _arc_through(P[ks], P[a:b + 1]) if len(ks) >= 3 else None
        if res is not None:
            pts, R = res
            radii.append(R); n_ref += 1
            detail.append((ids[a], ids[b], len(ks), R, L, True))
        else:
            pts, nl, rr = _arc(P[a:b + 1]), 0, []
            pts, nl, rr = smooth_chain(P[a:b + 1])
            n_line += nl; radii += rr; n_auto += 1; auto_len += L
            detail.append((ids[a], ids[b], len(ks), None, L, False))
        out.extend(pts[1:])
    return np.array(out), n_line, radii, n_ref, n_auto, auto_len, detail


def smooth_graph_ctrl(G, pos, ctrl):
    lanes, n_line, radii = [], 0, []
    n_ref = n_auto = 0
    auto_len = 0.0
    chains_nodes = []
    succ = defaultdict(list); pred = defaultdict(list)
    for u, v in G.edges():
        succ[u].append(v); pred[v].append(u)
    flow = lambda n: len(succ[n]) == 1 and len(pred[n]) == 1
    seen = set()
    def walk(u, v):
        ch = [u, v]; seen.add((u, v))
        while flow(v):
            w = succ[v][0]
            if (v, w) in seen:
                break
            ch.append(w); seen.add((v, w)); v = w
        return ch
    for u, v in G.edges():
        if (u, v) not in seen and not flow(u):
            chains_nodes.append(walk(u, v))
    for u, v in G.edges():
        if (u, v) not in seen:
            chains_nodes.append(walk(u, v))

    detail = []
    for ids in chains_nodes:
        P = np.array([pos[n] for n in ids])
        s, nl, rr, nr, na, al, det = smooth_chain_ctrl(P, ids, ctrl)
        lanes.append(s); n_line += nl; radii += rr
        n_ref += nr; n_auto += na; auto_len += al; detail += det
    return lanes, n_line, radii, n_ref, n_auto, auto_len, detail
