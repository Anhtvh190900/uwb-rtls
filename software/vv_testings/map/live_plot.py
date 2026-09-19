#!/usr/bin/env python3
"""Hien vi tri Tag truc tiep tren graph, khong can webserver.

1. Doc du lieu bang chinh test_position.py  ->  lay ukf_x_m, ukf_y_m, ukf_yaw_deg
2. Ve graph.hml giong graph.png (goc toa do o tren trai: ngang = y, doc = x
   huong xuong), roi dat mot tam giac do chi vi tri va huong hien tai.

Vi du
-----
    py live_plot.py                                  # doc Tag that
    py live_plot.py --replay demo_run.csv --loop     # khong can phan cung
    py live_plot.py --save live.png                  # headless: ghi PNG
    py live_plot.py --ids                            # hien them ten node
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys
import threading
import time
from pathlib import Path

import matplotlib

HERE = Path(__file__).resolve().parent
TP_DIR = HERE.parent / "vehicle_testings"

# trang thai dung chung giua thread doc va vong ve
live = {"pose": None, "status": "khoi dong", "count": 0}
trail: list[tuple[float, float]] = []
lock = threading.Lock()
stop = threading.Event()


def set_status(text: str) -> None:
    with lock:
        live["status"] = text
    print(f"[status] {text}", flush=True)


def on_sample(s: dict) -> None:
    """Chi giu 3 gia tri can de ve: x, y, yaw."""
    with lock:
        live["pose"] = (s["ukf_x_m"], s["ukf_y_m"], s["ukf_yaw_deg"])
        live["count"] += 1
        trail.append((s["ukf_x_m"], s["ukf_y_m"]))
        del trail[:-4000]


# ------------------------------------------------- doc du lieu (test_position)
def read_tag(args) -> None:
    """Vong doc Tag, dung nguyen protocol + decode cua test_position.py."""
    sys.path.insert(0, str(TP_DIR))
    import test_position as tp

    proto, factory = tp.VvProtocol(), tp.CommandFactory()
    src, dst = int(tp.VvAddress.VEHICLE), int(tp.VvAddress.MCU)

    while not stop.is_set():
        port = args.port or tp.find_port()
        if port is None:
            set_status(f"khong thay cong Tag ({tp.VCP_VID:04X}:{tp.VCP_PID:04X})")
            stop.wait(2.0)
            continue
        try:
            with tp.serial.Serial(port, args.baud, timeout=0.2) as link:
                set_status(f"da mo {port}")
                tp.send(link, proto, factory.ranging_start(
                    src, dst, proto.next_seq(),
                    yaw_deg=args.yaw, is_ukf_reinit=args.reinit))
                set_status("da gui ranging_start, dang cho sensor_fusion_result")

                while not stop.is_set():
                    chunk = link.read(4096)
                    if not chunk:
                        continue
                    for packet in proto.decode_from_frames(chunk):
                        if packet.WhichOneof("params") == "sensor_fusion_result":
                            on_sample(tp.decode(packet.sensor_fusion_result))

                tp.send(link, proto, factory.ranging_stop(src, dst, proto.next_seq()))
        except tp.serial.SerialException as exc:
            set_status(f"loi cong: {exc}")
            stop.wait(2.0)


def read_csv(args) -> None:
    """Phat lai CSV do test_position.py --csv ghi ra (de test khong can Tag)."""
    rows = list(csv.DictReader(args.replay.open()))
    if not rows:
        set_status(f"{args.replay} rong")
        return
    while not stop.is_set():
        set_status(f"phat lai {args.replay.name} ({len(rows)} mau)")
        with lock:
            trail.clear()
        prev = None
        for r in rows:
            if stop.is_set():
                return
            t = float(r["host_time"]) if r.get("host_time") else None
            if args.rate:
                stop.wait(1.0 / args.rate)
            elif prev is not None and t is not None:
                stop.wait(min(max(t - prev, 0.0), 1.0))
            prev = t
            on_sample({k: float(r.get(k) or 0.0)
                       for k in ("ukf_x_m", "ukf_y_m", "ukf_yaw_deg")})
        if not args.loop:
            set_status("het file phat lai")
            return


# ------------------------------------------------------------- ve graph + xe
def load_graph(path: Path):
    """Doc graph.hml -> vi tri node va 2 nhom canh, y het draw_graph.py."""
    import networkx as nx

    cleaned = "".join(l for l in path.open(encoding="utf-8")
                      if not l.lstrip().startswith("@"))
    G = nx.read_graphml(io.StringIO(cleaned))
    pos = {n: (float(d["x"]), float(d["y"])) for n, d in G.nodes(data=True)}
    solid, dotted = [], []
    for u, v, d in G.edges(data=True):
        seg = (to_disp(*pos[u]), to_disp(*pos[v]))
        (dotted if str(d.get("dotted", "")).lower() == "true" else solid).append(seg)
    return pos, solid, dotted


def to_disp(x: float, y: float) -> tuple[float, float]:
    """Khung hien thi cua graph.png: ngang = y, doc = x (huong xuong)."""
    return y, x


def triangle(x: float, y: float, yaw_deg: float, size: float):
    """3 dinh tam giac chi huong, tinh trong khung hien thi."""
    cx, cy = to_disp(x, y)
    th = math.radians(yaw_deg)
    hx, hy = math.sin(th), math.cos(th)          # huong tien, giong map_live.html
    px, py = -hy, hx                             # phap tuyen
    w = size * 0.42
    return [(cx + hx * size, cy + hy * size),
            (cx - hx * size * 0.45 + px * w, cy - hy * size * 0.45 + py * w),
            (cx - hx * size * 0.45 - px * w, cy - hy * size * 0.45 - py * w)]


def build_figure(args):
    from matplotlib.collections import LineCollection
    from matplotlib.patches import Polygon
    import matplotlib.pyplot as plt

    pos, solid, dotted = load_graph(args.graph)
    pts = [to_disp(*p) for p in pos.values()]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    scale = 11.0 / max(w, h)

    fig, ax = plt.subplots(figsize=(w * scale + 1.4, h * scale + 1.4))
    if hasattr(fig.canvas, "manager") and fig.canvas.manager:
        fig.canvas.manager.set_window_title("UWB Tag - vi tri truc tiep")

    ax.add_collection(LineCollection(solid, colors="#1f77b4", linewidths=1.0))
    ax.add_collection(LineCollection(dotted, colors="#d62728", linewidths=0.9,
                                     linestyles=(0, (3, 3))))
    ax.scatter(xs, ys, s=4, c="#333333", zorder=3)
    if args.ids:
        for n, p in pos.items():
            ax.annotate(n, to_disp(*p), fontsize=2, color="#888800", zorder=4)

    art = {
        "trail": ax.plot([], [], "-", color="#ff7f0e", alpha=0.55, lw=1.3, zorder=5)[0],
        "tri": Polygon([(0, 0)] * 3, closed=True, facecolor="red",
                       edgecolor="#7f0000", linewidth=0.8, zorder=6),
    }
    ax.add_patch(art["tri"])
    art["tri"].set_visible(False)

    m = 0.3
    ax.set_aspect("equal")
    ax.set_xlim(0, max(xs) + m)
    ax.set_ylim(max(ys) + m, 0)                  # (0,0) o tren trai, x tang xuong duoi
    ax.plot(0, 0, marker="+", ms=10, mew=1.4, color="#333333", clip_on=False)
    ax.set_xlabel("y [m]")
    ax.set_ylabel("x [m]")
    ax.grid(True, linewidth=0.3, alpha=0.4)
    ax.set_title(" ", fontsize=11)      # chua cho truoc, khong thi tight_layout cat mat
    fig.tight_layout()
    return fig, ax, art


def make_updater(ax, art, args):
    seen = {"n": 0, "t": time.time(), "hz": 0.0}

    def update(_frame=None):
        with lock:
            pose, status, count = live["pose"], live["status"], live["count"]
            pts = [to_disp(*p) for p in trail[-args.trail:]]

        now = time.time()
        if now - seen["t"] >= 1.0:
            seen["hz"] = (count - seen["n"]) / (now - seen["t"])
            seen["n"], seen["t"] = count, now

        if pose is None:
            ax.set_title(status, fontsize=10)
        else:
            x, y, yaw = pose
            art["tri"].set_xy(triangle(x, y, yaw, args.size))
            art["tri"].set_visible(True)
            if len(pts) > 1:
                art["trail"].set_data([p[0] for p in pts], [p[1] for p in pts])
            ax.set_title(f"x={x:6.2f} m   y={y:6.2f} m   yaw={yaw:7.1f}°"
                         f"    {seen['hz']:4.1f} Hz   ({count} mau)",
                         fontsize=11, family="monospace")
        return [art["tri"], art["trail"]]

    return update


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--graph", type=Path, default=HERE / "graph.hml")
    ap.add_argument("--port", help="cong serial cua Tag (mac dinh: tu tim)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--replay", type=Path, help="phat lai CSV thay vi doc Tag")
    ap.add_argument("--rate", type=float, help="ep tan so phat lai [Hz]")
    ap.add_argument("--loop", action="store_true", help="phat lai lap vo han")
    ap.add_argument("--save", type=Path, help="headless: ghi PNG thay vi mo cua so")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--size", type=float, default=0.45, help="kich thuoc tam giac [m]")
    ap.add_argument("--trail", type=int, default=4000, help="so diem vet di (0 = tat)")
    ap.add_argument("--ids", action="store_true", help="hien ten node")
    ap.add_argument("--yaw", type=float, default=0.0, help="yaw gui kem ranging_start")
    ap.add_argument("--reinit", action="store_true")
    args = ap.parse_args()

    if not args.graph.is_file():
        print(f"Thieu {args.graph}")
        return 1

    interactive = args.save is None
    if interactive and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("Khong co DISPLAY - Orin chay headless nen khong mo duoc cua so.\n"
              "  Cach 1: ssh -X orin@... roi chay lai lenh nay\n"
              "  Cach 2: py live_plot.py --save live.png")
        return 1
    matplotlib.use("TkAgg" if interactive else "Agg")
    import matplotlib.pyplot as plt

    threading.Thread(target=read_csv if args.replay else read_tag,
                     args=(args,), daemon=True).start()

    fig, ax, art = build_figure(args)
    update = make_updater(ax, art, args)
    try:
        if interactive:
            from matplotlib.animation import FuncAnimation
            fig._anim = FuncAnimation(fig, update, interval=1000.0 / args.fps,
                                      blit=False, cache_frame_data=False)
            plt.show()
        else:
            print(f"Ghi {args.save} moi {1.0 / args.fps:.2f}s  (Ctrl-C de dung)")
            while not stop.is_set():
                update()
                fig.savefig(args.save, dpi=110)
                time.sleep(1.0 / args.fps)
    except KeyboardInterrupt:
        print("\ndang dung...")
    finally:
        stop.set()
        print(f"{live['count']} mau")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
