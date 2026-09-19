"""Vẽ graph.hml/GraphML bằng Matplotlib.

Ví dụ:
    python3 draw_graph.py graph.hml graph.png
    python3 draw_graph.py graph.hml --interactive

Ở chế độ ``--interactive``, rê chuột gần một node để xem ID và tọa độ của
node đó. Không cần cài thêm mplcursors.
"""
import argparse
import io


def parse_args():
    parser = argparse.ArgumentParser(description="Vẽ GraphML và xem ID node khi hover")
    parser.add_argument("src", nargs="?", default="graph.hml", help="file GraphML/HML nguồn")
    parser.add_argument("out", nargs="?", default="graph.png", help="file PNG đầu ra")
    parser.add_argument("--interactive", "--show", action="store_true", dest="interactive",
                        help="mở cửa sổ tương tác; hover lên node để xem ID")
    parser.add_argument("--no-save", action="store_true", help="không lưu PNG")
    parser.add_argument("--ids", action="store_true", help="hiện mọi ID node trên ảnh")
    parser.add_argument("--corner", action="store_true", help="đặt (0, 0) tại góc trên trái")
    parser.add_argument("--rot", type=int, choices=(0, 90, 180, 270), default=0,
                        help="xoay theo chiều kim đồng hồ")
    parser.add_argument("--flip", choices=("h", "v"), default=None,
                        help="lật ngang (h) hoặc dọc (v)")
    return parser.parse_args()


def add_hover(ax, node_ids, positions):
    """Hiện tooltip node gần con trỏ nhất, với khoảng bắt 10 px."""
    import numpy as np

    ids = list(node_ids)
    xy = np.asarray(positions, dtype=float)
    tooltip = ax.annotate(
        "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
        bbox={"boxstyle": "round,pad=0.35", "fc": "#fffde7", "ec": "#555555", "alpha": 0.96},
        arrowprops={"arrowstyle": "->", "color": "#555555"}, zorder=10,
    )
    tooltip.set_visible(False)
    highlight = ax.scatter([], [], s=42, facecolors="none", edgecolors="#e65100",
                           linewidths=1.2, zorder=9)
    last_index = None

    def on_move(event):
        nonlocal last_index
        if event.inaxes is not ax or event.x is None or event.y is None:
            if tooltip.get_visible():
                tooltip.set_visible(False)
                highlight.set_offsets(np.empty((0, 2)))
                ax.figure.canvas.draw_idle()
            last_index = None
            return

        # So sánh theo pixel để khoảng hover không phụ thuộc zoom/tỷ lệ trục.
        pixels = ax.transData.transform(xy)
        distances = np.hypot(pixels[:, 0] - event.x, pixels[:, 1] - event.y)
        index = int(np.argmin(distances))
        if distances[index] > 10:
            if tooltip.get_visible():
                tooltip.set_visible(False)
                highlight.set_offsets(np.empty((0, 2)))
                ax.figure.canvas.draw_idle()
            last_index = None
            return

        if index == last_index and tooltip.get_visible():
            return
        x, y = xy[index]
        tooltip.xy = (x, y)
        tooltip.set_text(f"Node: {ids[index]}\\nx: {x:.4f}\\ny: {y:.4f}")
        tooltip.set_visible(True)
        highlight.set_offsets([[x, y]])
        last_index = index
        ax.figure.canvas.draw_idle()

    ax.figure.canvas.mpl_connect("motion_notify_event", on_move)


def main():
    args = parse_args()

    # Agg cho xuất PNG không cần màn hình; interactive để Matplotlib chọn GUI backend.
    import matplotlib
    if not args.interactive:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    import networkx as nx

    # graph.hml có các marker @node/@graph ngoài chuẩn XML, nên loại chúng trước khi đọc.
    with open(args.src, "r", encoding="utf-8") as source:
        cleaned = "".join(line for line in source if not line.lstrip().startswith("@"))
    graph = nx.read_graphml(io.StringIO(cleaned))
    print(f"Nodes: {graph.number_of_nodes()}  Edges: {graph.number_of_edges()}  "
          f"Directed: {graph.is_directed()}")

    pos = {node: (float(data["x"]), float(data["y"])) for node, data in graph.nodes(data=True)}
    if args.corner:
        pos = {node: (y, x) for node, (x, y) in pos.items()}
    elif args.rot:
        ymax = max(y for _, y in pos.values())
        for _ in range(args.rot // 90):
            pos = {node: (ymax - y, x) for node, (x, y) in pos.items()}
            ymax = max(y for _, y in pos.values())

    solid, dotted = [], []
    for source, target, data in graph.edges(data=True):
        segment = (pos[source], pos[target])
        (dotted if str(data.get("dotted", "")).lower() == "true" else solid).append(segment)

    node_ids = list(pos)
    positions = [pos[node] for node in node_ids]
    xs, ys = zip(*positions)
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    scale = 14.0 / max(width, height)
    fig, ax = plt.subplots(figsize=(width * scale + 1, height * scale + 1), dpi=150)
    ax.add_collection(LineCollection(solid, colors="#1f77b4", linewidths=1.0,
                                     label=f"solid ({len(solid)})"))
    ax.add_collection(LineCollection(dotted, colors="#d62728", linewidths=0.9,
                                     linestyles=(0, (3, 3)), label=f"dotted ({len(dotted)})"))
    ax.scatter(xs, ys, s=8, c="#333333", zorder=3, label=f"nodes ({len(pos)})")

    if args.ids:
        for node, (x, y) in pos.items():
            ax.annotate(node, (x, y), fontsize=2, color="#888800", zorder=4)

    ax.set_aspect("equal")
    if args.corner:
        margin = 0.3
        ax.set_xlim(0, max(xs) + margin)
        ax.set_ylim(max(ys) + margin, 0)
        ax.plot(0, 0, marker="+", ms=10, mew=1.4, color="#333333", clip_on=False)
        ax.annotate("(0, 0)", (0, 0), xytext=(6, 10), textcoords="offset points", fontsize=8)
    else:
        ax.invert_yaxis()
        if args.flip == "h":
            ax.invert_xaxis()
        elif args.flip == "v":
            ax.invert_yaxis()
        ax.autoscale_view()
    if args.corner or args.rot in (90, 270):
        ax.set(xlabel="y [m]", ylabel="x [m]")
    else:
        ax.set(xlabel="x [m]", ylabel="y [m]")
    transforms = ["gốc tọa độ ở trên trái"] if args.corner else (
        ([f"rot {args.rot}"] if args.rot else []) + ([f"flip {args.flip}"] if args.flip else []))
    ax.set_title(f"{args.src} ({', '.join(transforms)})" if transforms else args.src)
    ax.grid(True, linewidth=0.3, alpha=0.4)
    ax.legend(loc="upper right", fontsize=8)

    if args.interactive:
        add_hover(ax, node_ids, positions)
        ax.set_title(f"{ax.get_title()} — rê chuột lên node để xem ID")
    fig.tight_layout()
    if not args.no_save:
        fig.savefig(args.out)
        print("Saved", args.out)
    if args.interactive:
        plt.show()


if __name__ == "__main__":
    main()
