"""Noi 2 diem co huong cho truoc bang 1 doan clothoid (G1 Hermite).

Thuat toan Bertolazzi & Frego: tim A sao cho Y(2A, delta-A, theta0) = 0,
tu do ra chieu dai L, do cong k va toc do doi do cong dk.
"""
import numpy as np
from scipy.optimize import brentq

MAX_LEN_RATIO = 2.5    # chieu dai cung toi da so voi day cung, chan nghiem cuon vong


def _XY(a, b, c, n=64):
    t = np.linspace(0.0, 1.0, n + 1)
    f = 0.5 * a * t * t + b * t + c
    # Simpson
    w = np.ones(n + 1); w[1:-1:2] = 4.0; w[2:-1:2] = 2.0
    h = 1.0 / (3.0 * n)
    return float(h * (w * np.cos(f)).sum()), float(h * (w * np.sin(f)).sum())


def _wrap(a):
    return (a + np.pi) % (2.0 * np.pi) - np.pi


def fit(p0, t0, p1, t1, n=24):
    """Tra ve (n+1) diem tren clothoid noi p0->p1 voi huong t0, t1.

    Rot ve doan thang neu bai toan suy bien / khong hoi tu.
    """
    p0 = np.asarray(p0, float); p1 = np.asarray(p1, float)
    d = p1 - p0
    r = float(np.hypot(*d))
    line = np.stack([p0, p1])
    if r < 1e-9:
        return line

    phi = np.arctan2(d[1], d[0])
    th0 = _wrap(np.arctan2(t0[1], t0[0]) - phi)
    th1 = _wrap(np.arctan2(t1[1], t1[0]) - phi)
    delta = th1 - th0

    g = lambda A: _XY(2.0 * A, delta - A, th0)[1]
    # Phuong trinh co the co nhieu nghiem: nghiem |A| lon cho duong cuon vong.
    # Lay tat ca nghiem, bo cai qua dai so voi day cung, giu cai it uon nhat.
    grid = np.linspace(-40.0, 40.0, 801)
    vals = np.array([g(a) for a in grid])
    roots = [float(a) for a, v in zip(grid, vals) if abs(v) < 1e-12]
    for i in np.where(vals[:-1] * vals[1:] < 0)[0]:
        roots.append(brentq(g, grid[i], grid[i + 1], xtol=1e-12))

    best = None
    for A in roots:
        X = _XY(2.0 * A, delta - A, th0)[0]
        if abs(X) < 1e-9:
            continue
        L = r / X
        if not np.isfinite(L) or L <= 0.0 or L > MAX_LEN_RATIO * r:
            continue                       # nghiem cuon vong / di lui
        k = (delta - A) / L
        dk = 2.0 * A / (L * L)
        bend = abs(k) * L + 0.5 * abs(dk) * L * L      # tong goc quay
        if best is None or bend < best[0]:
            best = (bend, A, L, k, dk)
    if best is None:
        return line
    _, A, L, k, dk = best

    th_abs = np.arctan2(t0[1], t0[0])

    s = np.linspace(0.0, L, n + 1)
    th = th_abs + k * s + 0.5 * dk * s * s
    cx = np.concatenate([[0.0], np.cumsum(np.diff(s) * (np.cos(th[:-1]) + np.cos(th[1:])) / 2.0)])
    cy = np.concatenate([[0.0], np.cumsum(np.diff(s) * (np.sin(th[:-1]) + np.sin(th[1:])) / 2.0)])
    pts = p0 + np.stack([cx, cy], axis=1)
    if np.linalg.norm(pts[-1] - p1) > 0.05 * max(r, 0.05):
        return line               # sai so cuoi qua lon -> bo
    pts[-1] = p1
    return pts
