#!/usr/bin/env python3
"""Danh gia bo do UWB: doc CSV cua test_position.py, ve len graph + bao cao so lieu.

CSV do lenh nay sinh ra:
    py test_position.py --csv run1.csv --seconds 60

Roi cham diem:
    py eval_run.py run1.csv                  # -> eval_run.png + bao cao tren man hinh
    py eval_run.py run1.csv --out run1.png
    py eval_run.py run1.csv --field tril     # cham diem tril thay vi ukf
    py eval_run.py run1.csv --ids            # hien them ten node tren map

Thuoc do chinh la sai so ngang (cross-track): khoang cach tu diem do toi canh
gan nhat cua graph.hml. Xe chay bam lan nen canh graph dong vai tro tham chieu;
sai so ngang lon = bo do lech, nhieu, hoac anchor kem.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                  # Orin chay headless
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ doc file
def load_graph(path: Path):
    """Doc graph.hml -> (pos, canh lien, canh dut), y het draw_graph.py."""
    import networkx as nx

    cleaned = "".join(l for l in path.open(encoding="utf-8")
                      if not l.lstrip().startswith("@"))
    G = nx.read_graphml(io.StringIO(cleaned))
    pos = {n: (float(d["x"]), float(d["y"])) for n, d in G.nodes(data=True)}
    solid, dotted = [], []
    for u, v, d in G.edges(data=True):
        seg = (pos[u], pos[v])
        (dotted if str(d.get("dotted", "")).lower() == "true" else solid).append(seg)
    return pos, solid, dotted


def load_csv(path: Path) -> dict:
    """CSV cua test_position.py -> cac cot numpy."""
    reader = csv.DictReader(path.open())
    rows = list(reader)
    if not rows:
        raise SystemExit(f"{path} rong")

    # live_server.py ghi CSV rut gon (6 cot) - khong du de cham diem.
    need = {"timestamp_ms", "ukf_x_m", "ukf_y_m", "tril_x_m", "tril_y_m"}
    missing = need - set(reader.fieldnames or [])
    if missing:
        raise SystemExit(
            f"{path} thieu cot: {', '.join(sorted(missing))}\n"
            "       Can CSV day du cua test_position.py --csv, khong phai\n"
            "       CSV rut gon cua live_server.py --csv.")

    def col(name, cast=float, default=0.0):
        out = []
        for r in rows:
            v = r.get(name, "")
            try:
                out.append(cast(v))
            except (TypeError, ValueError):
                out.append(default)
        return np.array(out)

    n_anchors = col("n_anchors")
    if not n_anchors.any():            # CSV cu chua co cot -> dem tu chuoi anchors
        n_anchors = np.array([len([a for a in (r.get("anchors") or "").split(";") if a])
                              for r in rows], dtype=float)

    per_anchor: dict[str, list] = {}
    for i, r in enumerate(rows):
        for item in (r.get("anchors") or "").split(";"):
            if not item:
                continue
            f = item.split(":")
            if len(f) < 2:
                continue
            try:
                per_anchor.setdefault(f[0], []).append(
                    (i, int(f[1]) / 1000.0, int(f[2]) if len(f) > 2 else 0))
            except ValueError:
                continue

    return {
        "n": len(rows),
        "per_anchor": per_anchor,
        "host_time": col("host_time"),
        "t_ms": col("timestamp_ms"),
        "ukf": np.column_stack([col("ukf_x_m"), col("ukf_y_m")]),
        "tril": np.column_stack([col("tril_x_m"), col("tril_y_m")]),
        "yaw": col("ukf_yaw_deg"),
        "zone": col("zone_id"),
        "n_anchors": n_anchors,
        "rng_err": col("ranging_error_count"),
        "prefilter": col("prefilter_reject_count"),
        "cov_xx": col("cov_xx_m2"),
        "cov_yy": col("cov_yy_m2"),
        "cov_valid": np.array([str(r.get("cov_valid", "")).strip().lower()
                               in ("1", "true", "yes") for r in rows]),
    }


# --------------------------------------------------------------- tinh sai so
def dist_to_edges(pts: np.ndarray, segs: list) -> np.ndarray:
    """Khoang cach tu moi diem toi canh graph gan nhat [m]."""
    a = np.array([s[0] for s in segs], dtype=float)      # (M,2)
    b = np.array([s[1] for s in segs], dtype=float)
    d = b - a
    den = np.maximum((d * d).sum(1), 1e-12)              # (M,)
    v = pts[:, None, :] - a[None, :, :]                  # (N,M,2)
    t = np.clip((v * d[None]).sum(2) / den, 0.0, 1.0)    # (N,M)
    proj = a[None] + t[..., None] * d[None]              # (N,M,2)
    return np.linalg.norm(pts[:, None, :] - proj, axis=2).min(1)


def scatter_stats(pts: np.ndarray) -> dict:
    """Do tan quanh trung vi - xe nam yen nen day la nhieu cua bo do."""
    c = np.median(pts, axis=0)
    rad = np.linalg.norm(pts - c, axis=1)
    k = max(1, len(pts) // 10)
    drift = float(np.linalg.norm(pts[-k:].mean(0) - pts[:k].mean(0)))
    return {
        "center": c,
        "rad": rad,
        "cep50": float(np.percentile(rad, 50)),
        "cep95": float(np.percentile(rad, 95)),
        "drms2": float(2.0 * np.sqrt(((pts - c) ** 2).sum(1).mean())),
        "max": float(rad.max()),
        "drift": drift,
    }


def anchor_table(data: dict) -> list:
    """Moi anchor: ti le ve duoc + do on dinh cua khoang cach."""
    out = []
    for aid, items in sorted(data["per_anchor"].items(), key=lambda kv: int(kv[0])):
        d = np.array([it[1] for it in items])
        w = np.array([it[2] for it in items], dtype=float)
        out.append({
            "id": aid, "n": len(d), "hit": 100.0 * len(d) / data["n"],
            "mean": float(d.mean()), "std": float(d.std()),
            "min": float(d.min()), "max": float(d.max()),
            "weight": float(w.mean()), "idx": np.array([it[0] for it in items]),
            "d": d,
        })
    return out


def stats(v: np.ndarray) -> dict:
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {k: float("nan") for k in ("mean", "p50", "p95", "max")}
    return {"mean": float(v.mean()), "p50": float(np.percentile(v, 50)),
            "p95": float(np.percentile(v, 95)), "max": float(v.max())}


def time_base(data: dict) -> tuple[np.ndarray, str]:
    """Moc thoi gian: uu tien timestamp_ms cua MCU.

    host_time duoc ghi theo lo nen nhieu mau trung gia tri (dt = 0), dung no
    tinh chu ky se sai; timestamp_ms la thoi diem MCU sinh mau, min hon.
    """
    t = data["t_ms"] / 1000.0
    if t.size > 1 and np.all(np.diff(t) >= 0) and t[-1] > t[0]:
        return t - t[0], "timestamp_ms (MCU)"
    t = data["host_time"]
    return t - t[0], "host_time (Orin)"


def analyse(data: dict, segs: list, field: str, bounds: tuple,
            static: bool = False) -> dict:
    pts = data[field]
    t, clock = time_base(data)

    dt = np.diff(t)
    dt_ok = dt[dt >= 0]
    step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    speed = np.divide(step, dt, out=np.zeros_like(step), where=dt > 0)

    sigma = np.sqrt(np.maximum(data["cov_xx"] + data["cov_yy"], 0.0))
    sigma[~data["cov_valid"]] = np.nan

    xmin, xmax, ymin, ymax = bounds
    inside = ((pts[:, 0] >= xmin - 1.0) & (pts[:, 0] <= xmax + 1.0) &
              (pts[:, 1] >= ymin - 1.0) & (pts[:, 1] <= ymax + 1.0))

    gap_lim = 3.0 * np.median(dt_ok) if dt_ok.size else 0.0
    return {
        "t": t,
        "clock": clock,
        "static": static,
        "scat": scatter_stats(pts),
        "scat_other": scatter_stats(data["tril" if field == "ukf" else "ukf"]),
        "pts": pts,
        "cross": dist_to_edges(pts, segs),
        "ukf_tril": np.linalg.norm(data["ukf"] - data["tril"], axis=1),
        "step": step,
        "speed": speed,
        "sigma": sigma,
        "dur": float(t[-1]) if t.size else 0.0,
        "hz": float(len(t) / t[-1]) if t.size and t[-1] > 0 else 0.0,
        "dt_p50": float(np.median(dt_ok)) if dt_ok.size else float("nan"),
        "dt_max": float(dt_ok.max()) if dt_ok.size else float("nan"),
        "gaps": int((dt > gap_lim).sum()) if gap_lim > 0 else 0,
        "gap_lim": gap_lim,
        "outside": int((~inside).sum()),
        "rng_err": float(data["rng_err"].max() - data["rng_err"].min()),
        "prefilter": float(data["prefilter"].max() - data["prefilter"].min()),
        "cov_valid_pct": 100.0 * data["cov_valid"].mean() if data["n"] else 0.0,
    }


# ------------------------------------------------------------------ bao cao
def report_static(data: dict, a: dict, field: str) -> None:
    """Xe nam yen: vi tri that khong doi nen moi dao dong deu la nhieu."""
    sc, so = a["scat"], a["scat_other"]
    other = "tril" if field == "ukf" else "ukf"
    print(f"  -- do tinh (xe nam yen) --")
    print(f"  vi tri trung vi : x={sc['center'][0]:.3f}  y={sc['center'][1]:.3f} m")
    print(f"  do tan {field:<9}: CEP50 {sc['cep50']:.3f}  CEP95 {sc['cep95']:.3f}  "
          f"2DRMS {sc['drms2']:.3f}  max {sc['max']:.3f} m")
    print(f"  do tan {other:<9}: CEP50 {so['cep50']:.3f}  CEP95 {so['cep95']:.3f}  "
          f"2DRMS {so['drms2']:.3f}  max {so['max']:.3f} m")
    print(f"  troi dau -> cuoi : {sc['drift']:.3f} m   (nam yen thi phai ~0)")

    rows = anchor_table(data)
    if not rows:
        print("  anchor          : khong mau nao co du lieu ranging")
        return
    print("  -- tung anchor (khoang cach phai la hang so) --")
    print("     id    n    ti le      d trung binh   do lech   min     max     weight")
    for r in rows:
        print(f"     A{r['id']:<3} {r['n']:5d}  {r['hit']:5.1f}%   "
              f"{r['mean']:8.3f} m   {r['std'] * 1000:6.0f} mm  "
              f"{r['min']:6.3f}  {r['max']:6.3f}   {r['weight']:6.0f}")


def report(path: Path, data: dict, a: dict, field: str) -> None:
    cross, sigma = stats(a["cross"]), stats(a["sigma"])
    ut, sp = stats(a["ukf_tril"]), stats(a["speed"])
    anc = data["n_anchors"]

    print(f"\n=== {path.name}   (cham diem cot '{field}') ===")
    print(f"  mau            : {data['n']}   thoi luong {a['dur']:.1f}s   "
          f"{a['hz']:.1f} Hz   (dong ho: {a['clock']})")
    print(f"  chu ky          : p50 {a['dt_p50'] * 1000:.0f} ms   "
          f"max {a['dt_max'] * 1000:.0f} ms   "
          f"gap >{a['gap_lim'] * 1000:.0f}ms: {a['gaps']}")
    if not a["static"]:
        print(f"  sai so ngang    : p50 {cross['p50']:.3f}  p95 {cross['p95']:.3f}  "
              f"max {cross['max']:.3f} m   (toi canh graph gan nhat)")
    print(f"  sigma (cov)     : p50 {sigma['p50']:.3f}  p95 {sigma['p95']:.3f} m   "
          f"cov_valid {a['cov_valid_pct']:.0f}%")
    print(f"  |ukf - tril|    : p50 {ut['p50']:.3f}  p95 {ut['p95']:.3f}  "
          f"max {ut['max']:.3f} m")
    print(f"  buoc nhay       : p50 {stats(a['step'])['p50']:.3f}  "
          f"max {stats(a['step'])['max']:.3f} m   "
          f"toc do p95 {sp['p95']:.2f} m/s")
    print(f"  anchor/mau      : p50 {np.median(anc):.0f}  min {anc.min():.0f}  "
          f"<3 anchor: {100.0 * (anc < 3).mean():.0f}% so mau")
    print(f"  loi ranging     : +{a['rng_err']:.0f}   prefilter loai "
          f"+{a['prefilter']:.0f}   diem ra ngoai map: {a['outside']}")

    if a["static"]:
        report_static(data, a, field)

    # ket luan tho, de nhin nhanh khi do nhieu lan
    bad = []
    if a["static"]:
        if a["scat"]["cep95"] > 0.3:
            bad.append(f"do tan CEP95 {a['scat']['cep95']:.2f} m > 0.30 m")
        if a["scat"]["drift"] > 0.2:
            bad.append(f"troi {a['scat']['drift']:.2f} m du nam yen")
    elif cross["p95"] > 0.5:
        bad.append(f"sai so ngang p95 {cross['p95']:.2f} m > 0.50 m")
    if a["gaps"]:
        bad.append(f"{a['gaps']} lan mat mau")
    if (anc < 3).mean() > 0.05:
        bad.append("nhieu mau duoi 3 anchor")
    if ut["p95"] > 0.5:
        bad.append(f"ukf lech tril p95 {ut['p95']:.2f} m")
    print("  ket luan        : " + ("DAT" if not bad else "CAN XEM LAI - "
                                    + "; ".join(bad)))


# --------------------------------------------------------------------- ve
def to_disp(p: np.ndarray) -> np.ndarray:
    """Khung hien thi cua graph.png: ngang = y, doc = x (huong xuong)."""
    return p[:, ::-1]


def draw_static(fig, gs, data, a, field: str, args) -> None:
    """3 khung ben phai cho phep do tinh: do tan, khoang cach anchor, sigma."""
    t, sc = a["t"], a["scat"]
    cx, cy = sc["center"][1], sc["center"][0]        # khung hien thi: ngang = y

    ax1 = fig.add_subplot(gs[0, 1])
    other = to_disp(data["tril" if field == "ukf" else "ukf"])
    ax1.plot(other[:, 0], other[:, 1], ".", ms=2, color="#bbbbbb", zorder=2,
             label="tril" if field == "ukf" else "ukf")
    trk = to_disp(a["pts"])
    ax1.plot(trk[:, 0], trk[:, 1], ".", ms=2.5, color="#ff7f0e", zorder=3, label=field)
    for r, ls, lab in ((sc["cep50"], "--", "CEP50"), (sc["cep95"], ":", "CEP95")):
        ax1.add_patch(plt.Circle((cx, cy), r, fill=False, ls=ls, lw=1.0,
                                 color="#1f77b4", zorder=4,
                                 label=f"{lab} {r:.2f} m"))
    ax1.plot(cx, cy, "+", ms=11, mew=1.6, color="#000000", zorder=5)
    ax1.set_aspect("equal")
    ax1.set_xlabel("y [m]")
    ax1.set_ylabel("x [m]")
    ax1.invert_yaxis()
    ax1.grid(True, lw=0.3, alpha=0.4)
    ax1.legend(fontsize=7, loc="upper right")
    ax1.set_title("do tan quanh trung vi (xe nam yen)", fontsize=10)

    ax2 = fig.add_subplot(gs[1, 1])
    rows = anchor_table(data)
    cols = ["#1f77b4", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2"]
    for k, r in enumerate(rows):
        c = cols[k % len(cols)]
        ax2.plot(t[r["idx"]], r["d"], ".", ms=2.5, color=c,
                 label=f"A{r['id']}  {r['hit']:.0f}%  sd {r['std'] * 1000:.0f}mm")
        ax2.axhline(r["mean"], lw=0.6, color=c, alpha=0.5)
    ax2.set_xlabel("t [s]")
    ax2.set_ylabel("khoang cach [m]")
    ax2.grid(True, lw=0.3, alpha=0.4)
    ax2.legend(fontsize=7, ncol=2)
    ax2.set_title("khoang cach tung anchor (phai la hang so)", fontsize=10)

    ax3 = fig.add_subplot(gs[2, 1])
    fin = np.isfinite(a["sigma"])
    if fin.any():
        ax3.plot(t[fin], a["sigma"][fin], lw=0.8, color="#1f77b4", label="sigma (cov)")
    ax3.set_xlabel("t [s]")
    ax3.set_ylabel("sigma [m]", color="#1f77b4")
    ax3.grid(True, lw=0.3, alpha=0.4)
    ax3.legend(fontsize=7, loc="upper left")
    ax4 = ax3.twinx()
    ax4.step(t, data["n_anchors"], lw=0.8, color="#8c564b", where="post")
    ax4.set_ylabel("so anchor", color="#8c564b")
    ax4.set_ylim(0, max(4.5, data["n_anchors"].max() + 0.5))
    ax3.set_title("sigma phong len moi khi thieu anchor", fontsize=10)

    fig.savefig(args.out, bbox_inches="tight")
    print(f"\nDa ghi {args.out}")


def draw(args, pos, solid, dotted, data, a, field: str) -> None:
    from matplotlib.collections import LineCollection

    node = to_disp(np.array(list(pos.values())))
    fig = plt.figure(figsize=(17, 9.5), dpi=130)
    gs = fig.add_gridspec(3, 2, width_ratios=[1.25, 1.0], hspace=0.45, wspace=0.18)
    axm = fig.add_subplot(gs[:, 0])

    axm.add_collection(LineCollection([[p[::-1] for p in s] for s in solid],
                                      colors="#1f77b4", linewidths=0.9, alpha=0.7))
    axm.add_collection(LineCollection([[p[::-1] for p in s] for s in dotted],
                                      colors="#d62728", linewidths=0.8, alpha=0.6,
                                      linestyles=(0, (3, 3))))
    axm.scatter(node[:, 0], node[:, 1], s=3, c="#333333", zorder=3)
    if args.ids:
        for n, p in pos.items():
            axm.annotate(n, (p[1], p[0]), fontsize=2, color="#888800", zorder=4)

    if args.show_other:
        other = to_disp(data["tril" if field == "ukf" else "ukf"])
        axm.plot(other[:, 0], other[:, 1], ".", ms=1.6, color="#999999", zorder=4,
                 label="tril" if field == "ukf" else "ukf")

    trk = to_disp(a["pts"])
    axm.plot(trk[:, 0], trk[:, 1], "-", lw=0.8, color="#ff7f0e", alpha=0.5, zorder=5)
    cval = a["scat"]["rad"] if a["static"] else a["cross"]
    clab = "lech khoi trung vi [m]" if a["static"] else "sai so ngang [m]"
    sc = axm.scatter(trk[:, 0], trk[:, 1], c=cval, s=7, cmap="viridis",
                     vmin=0.0, vmax=max(args.vmax, 1e-3), zorder=6)
    axm.plot(trk[0, 0], trk[0, 1], "o", ms=7, mfc="none", mec="#006400", mew=1.6,
             zorder=7, label="bat dau")
    axm.plot(trk[-1, 0], trk[-1, 1], "s", ms=7, mfc="none", mec="#8b0000", mew=1.6,
             zorder=7, label="ket thuc")
    fig.colorbar(sc, ax=axm, shrink=0.55, pad=0.01, label=clab)

    m = 0.3
    axm.set_aspect("equal")
    axm.set_xlim(0, node[:, 0].max() + m)
    axm.set_ylim(node[:, 1].max() + m, 0)       # (0,0) o tren trai, x tang xuong duoi
    axm.plot(0, 0, marker="+", ms=10, mew=1.4, color="#333333", clip_on=False)
    axm.set_xlabel("y [m]")
    axm.set_ylabel("x [m]")
    axm.grid(True, linewidth=0.3, alpha=0.4)
    axm.legend(loc="upper right", fontsize=8)
    axm.set_title(f"{args.csv.name} - quy dao '{field}' tren {args.graph.name}",
                  fontsize=11)

    t = a["t"]
    if a["static"]:
        return draw_static(fig, gs, data, a, field, args)

    ax1 = fig.add_subplot(gs[0, 1])
    ax1.plot(t, a["cross"], lw=0.8, color="#ff7f0e", label="sai so ngang")
    finite = np.isfinite(a["sigma"])
    if finite.any():
        ax1.plot(t[finite], a["sigma"][finite], lw=0.8, color="#1f77b4",
                 alpha=0.8, label="sigma (cov)")
    ax1.axhline(np.percentile(a["cross"], 95), ls="--", lw=0.8, color="#888888",
                label=f"p95 = {np.percentile(a['cross'], 95):.2f} m")
    ax1.set_ylabel("[m]")
    ax1.set_xlabel("t [s]")
    ax1.legend(fontsize=7)
    ax1.grid(True, lw=0.3, alpha=0.4)
    ax1.set_title("sai so theo thoi gian", fontsize=10)

    ax2 = fig.add_subplot(gs[1, 1])
    s = np.sort(a["cross"])
    ax2.plot(s, np.linspace(0, 100, s.size), lw=1.2, color="#2ca02c")
    for q in (50, 95):
        v = np.percentile(a["cross"], q)
        ax2.axvline(v, ls="--", lw=0.8, color="#888888")
        ax2.annotate(f"p{q} {v:.2f}", (v, q), fontsize=7,
                     xytext=(4, -10), textcoords="offset points")
    ax2.set_xlabel("sai so ngang [m]")
    ax2.set_ylabel("% mau <= x")
    ax2.grid(True, lw=0.3, alpha=0.4)
    ax2.set_title("phan bo tich luy", fontsize=10)

    ax3 = fig.add_subplot(gs[2, 1])
    ax3.plot(t[1:], a["speed"], lw=0.7, color="#9467bd", label="toc do [m/s]")
    ax3.set_xlabel("t [s]")
    ax3.set_ylabel("m/s", color="#9467bd")
    ax3.grid(True, lw=0.3, alpha=0.4)
    ax4 = ax3.twinx()
    ax4.step(t, data["n_anchors"], lw=0.8, color="#8c564b", where="post")
    ax4.set_ylabel("so anchor", color="#8c564b")
    ax4.set_ylim(0, max(4.5, data["n_anchors"].max() + 0.5))
    ax3.set_title("toc do suy ra + so anchor", fontsize=10)

    fig.savefig(args.out, bbox_inches="tight")
    print(f"\nDa ghi {args.out}")


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", type=Path, help="CSV do test_position.py --csv ghi ra")
    ap.add_argument("--graph", type=Path, default=HERE / "graph.hml")
    ap.add_argument("--out", type=Path, help="anh ket qua (mac dinh: <csv>.png)")
    ap.add_argument("--field", choices=("ukf", "tril"), default="ukf",
                    help="cot vi tri dem cham diem (mac dinh ukf)")
    ap.add_argument("--show-other", action="store_true",
                    help="ve them cot con lai (tril/ukf) mau xam")
    ap.add_argument("--vmax", type=float, default=0.5,
                    help="tran thang mau sai so ngang [m]")
    ap.add_argument("--static", action="store_true",
                    help="xe nam yen: cham do tan + do on dinh tung anchor")
    ap.add_argument("--ids", action="store_true", help="hien ten node")
    args = ap.parse_args()

    if not args.csv.is_file():
        print(f"Thieu {args.csv}")
        return 1
    if not args.graph.is_file():
        print(f"Thieu {args.graph}")
        return 1
    args.out = args.out or args.csv.with_suffix(".png")

    pos, solid, dotted = load_graph(args.graph)
    segs = solid + dotted
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]

    data = load_csv(args.csv)
    a = analyse(data, segs, args.field, (min(xs), max(xs), min(ys), max(ys)),
                static=args.static)
    report(args.csv, data, a, args.field)
    draw(args, pos, solid, dotted, data, a, args.field)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
