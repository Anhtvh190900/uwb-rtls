#!/usr/bin/env python3
"""Phục vụ graph.hml qua trình duyệt, không cần Matplotlib hay GUI.

Ví dụ (trên máy SSH):
    python3 graph_server.py graph.hml --port 8000

Sau đó tạo SSH tunnel ở máy local và mở http://localhost:8000 .
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent


def load_graph(path: Path) -> dict:
    """Đọc GraphML, kể cả graph.hml có marker @node/@graph không phải XML."""
    text = "".join(line for line in path.read_text(encoding="utf-8").splitlines(True)
                   if not line.lstrip().startswith("@"))
    root = ET.fromstring(text)
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    names = {key.attrib["id"]: key.attrib.get("attr.name", key.attrib["id"])
             for key in root.findall(f"{ns}key")}

    nodes = []
    for node in root.findall(f".//{ns}node"):
        data = {names.get(item.attrib.get("key"), item.attrib.get("key")): item.text
                for item in node.findall(f"{ns}data")}
        nodes.append({"id": node.attrib["id"], "x": float(data["x"]), "y": float(data["y"])})
    edges = []
    for edge in root.findall(f".//{ns}edge"):
        data = {names.get(item.attrib.get("key"), item.attrib.get("key")): item.text
                for item in edge.findall(f"{ns}data")}
        edges.append({"source": edge.attrib["source"], "target": edge.attrib["target"],
                      "dotted": str(data.get("dotted", "")).lower() == "true"})
    return {"nodes": nodes, "edges": edges, "name": path.name}


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Graph viewer</title>
<style>
  :root { color-scheme: light dark; --bg:#eceeec; --panel:#f8f9f8; --ink:#17201e;
    --line:#c8cecb; --edge:#1673aa; --dot:#c63c32; --node:#17201e; --accent:#007d8a; }
  @media (prefers-color-scheme:dark) { :root { --bg:#101514; --panel:#171e1d;
    --ink:#e7ecea; --line:#303a38; --edge:#58b9ee; --dot:#ff7b72; --node:#e7ecea; --accent:#55d3df; } }
  * { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
    font:14px system-ui,sans-serif; } main { min-height:100vh; display:flex; flex-direction:column; padding:12px; gap:10px }
  header { display:flex; gap:12px; align-items:center; flex-wrap:wrap } h1 { font-size:17px; margin:0 }
  .meta { color:color-mix(in srgb,var(--ink) 65%,transparent); font:12px ui-monospace,monospace }
  .spacer { flex:1 } button,label { border:1px solid var(--line); background:var(--panel); color:var(--ink);
    border-radius:6px; padding:5px 9px; font:inherit; cursor:pointer } button:hover,label:hover { filter:brightness(.96) }
  .stage { position:relative; flex:1; min-height:72vh; overflow:hidden; border:1px solid var(--line);
    border-radius:10px; background:var(--panel) } canvas { display:block; width:100%; height:100%; cursor:crosshair; touch-action:none }
  .tip { display:none; position:absolute; pointer-events:none; padding:6px 9px; white-space:pre-line;
    border:1px solid var(--line); border-left:3px solid var(--accent); border-radius:5px; background:var(--panel);
    font:12px ui-monospace,monospace; line-height:1.5; box-shadow:0 3px 12px #0003 }
  .hint { position:absolute; left:9px; bottom:9px; pointer-events:none; padding:4px 7px; border:1px solid var(--line);
    border-radius:5px; background:color-mix(in srgb,var(--panel) 88%,transparent); font-size:12px }
</style>
<main>
  <header><h1 id="title">Graph</h1><span class="meta" id="meta">đang tải…</span><span class="spacer"></span>
    <label><input id="show-id" type="checkbox"> hiện mọi ID</label><button id="fit">Vừa khung</button></header>
  <div class="stage" id="stage"><canvas id="cv"></canvas><div class="tip" id="tip"></div>
    <div class="hint">Rê chuột để xem node · Lăn để zoom · Kéo để di chuyển</div></div>
</main>
<script>
(() => {
  const $ = id => document.getElementById(id), cv = $('cv'), stage = $('stage'), ctx = cv.getContext('2d');
  let nodes = [], byId = new Map(), edges = [], bounds, dpr = 1, hover = null, drag = null;
  let view = {s: 1, tx: 0, ty: 0};
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const screen = n => [n.x * view.s + view.tx, n.y * view.s + view.ty];
  function resize() { dpr = devicePixelRatio || 1; cv.width = stage.clientWidth*dpr; cv.height = stage.clientHeight*dpr; draw(); }
  function fit() { if (!bounds) return; const w=stage.clientWidth,h=stage.clientHeight,p=34;
    view.s=Math.min((w-p*2)/(bounds.x1-bounds.x0 || 1),(h-p*2)/(bounds.y1-bounds.y0 || 1));
    view.tx=(w-(bounds.x0+bounds.x1)*view.s)/2; view.ty=(h-(bounds.y0+bounds.y1)*view.s)/2; draw(); }
  function draw() { const w=stage.clientWidth,h=stage.clientHeight; ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
    ctx.lineWidth=1; ctx.strokeStyle=css('--edge'); ctx.setLineDash([]); ctx.beginPath();
    for (const e of edges) { if (e.dotted) continue; const a=byId.get(e.source),b=byId.get(e.target); if (!a||!b) continue;
      const p=screen(a),q=screen(b);ctx.moveTo(p[0],p[1]);ctx.lineTo(q[0],q[1]); } ctx.stroke();
    ctx.strokeStyle=css('--dot');ctx.setLineDash([4,4]);ctx.beginPath(); for (const e of edges) { if (!e.dotted) continue;
      const a=byId.get(e.source),b=byId.get(e.target);if(!a||!b)continue;const p=screen(a),q=screen(b);ctx.moveTo(p[0],p[1]);ctx.lineTo(q[0],q[1]); }ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle=css('--node'); for (const n of nodes) { const p=screen(n);ctx.beginPath();ctx.arc(p[0],p[1],2.5,0,2*Math.PI);ctx.fill(); }
    if ($('show-id').checked) { ctx.fillStyle=css('--ink');ctx.font='10px ui-monospace,monospace'; for(const n of nodes){const p=screen(n);ctx.fillText(n.id,p[0]+4,p[1]-4);} }
    if (hover) { const p=screen(hover);ctx.strokeStyle=css('--accent');ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],7,0,2*Math.PI);ctx.stroke(); }
  }
  function nearest(x,y) { let best=null,bd=12*12; for(const n of nodes){const p=screen(n),d=(p[0]-x)**2+(p[1]-y)**2;if(d<bd){best=n;bd=d;}}return best; }
  cv.addEventListener('mousemove', e => { const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
    if(drag){view.tx+=x-drag.x;view.ty+=y-drag.y;drag={x,y};draw();return;} hover=nearest(x,y); const tip=$('tip');
    if(hover){tip.textContent=`Node: ${hover.id}\nx: ${hover.x.toFixed(4)}\ny: ${hover.y.toFixed(4)}`;tip.style.display='block';tip.style.left=(x+13)+'px';tip.style.top=(y+13)+'px';}else tip.style.display='none';draw(); });
  cv.addEventListener('mouseleave',()=>{hover=null;drag=null;$('tip').style.display='none';draw();});
  cv.addEventListener('mousedown',e=>{const r=cv.getBoundingClientRect();drag={x:e.clientX-r.left,y:e.clientY-r.top};});
  addEventListener('mouseup',()=>drag=null);
  cv.addEventListener('wheel',e=>{e.preventDefault();const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,k=e.deltaY<0?1.15:1/1.15;view.tx=x-(x-view.tx)*k;view.ty=y-(y-view.ty)*k;view.s*=k;draw();},{passive:false});
  $('fit').onclick=fit;$('show-id').onchange=draw;new ResizeObserver(resize).observe(stage);
  fetch('graph.json').then(r=>{if(!r.ok)throw Error(r.statusText);return r.json()}).then(data=>{nodes=data.nodes;byId=new Map(nodes.map(n=>[n.id,n]));edges=data.edges;
    bounds={x0:Math.min(...nodes.map(n=>n.x)),x1:Math.max(...nodes.map(n=>n.x)),y0:Math.min(...nodes.map(n=>n.y)),y1:Math.max(...nodes.map(n=>n.y))};
    $('title').textContent=data.name;$('meta').textContent=`${nodes.length} node · ${edges.length} edge`;resize();fit();
  }).catch(e=>{$('meta').textContent='Không đọc được graph: '+e.message;});
})();
</script>"""


def make_handler(graph_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def send(self, code: int, content: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/graph.json":
                try:
                    body = json.dumps(load_graph(graph_path), separators=(",", ":")).encode()
                except (OSError, ET.ParseError, ValueError, KeyError) as exc:
                    return self.send(500, str(exc).encode(), "text/plain; charset=utf-8")
                return self.send(200, body, "application/json")
            self.send(404, b"Not found", "text/plain; charset=utf-8")
    return Handler


def main():
    parser = argparse.ArgumentParser(description="Mở graph.hml bằng web browser")
    parser.add_argument("graph", nargs="?", type=Path, default=HERE / "graph.hml")
    parser.add_argument("--host", default="127.0.0.1", help="mặc định chỉ localhost, an toàn khi SSH")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    graph = args.graph.resolve()
    if not graph.is_file():
        parser.error(f"không tìm thấy graph: {graph}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(graph))
    print(f"Đang phục vụ {graph.name} tại http://{args.host}:{args.port}")
    print("Dừng bằng Ctrl+C. Refresh trang để đọc graph.hml mới nhất.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
