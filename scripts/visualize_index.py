"""Generate a self-contained page for exploring the vector store.

Projects the 1024-dimension vectors down to two with PCA, precomputes each
document's nearest neighbours, and writes everything into a single HTML file.
No server, no API calls, no network — open it in a browser and click around.

    python scripts/visualize_index.py
    python scripts/visualize_index.py --open
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import webbrowser

import numpy as np

from core.config import SETTINGS

NEIGHBOURS = 8
SNIPPET = 400


def project(vectors: np.ndarray) -> np.ndarray:
    """PCA to two dimensions.

    A linear projection, so it shows the broad shape rather than fine cluster
    structure — but it needs no extra dependencies and is honest about what it
    is. Distances on screen approximate similarity; they don't reproduce it.
    """
    centered = vectors - vectors.mean(axis=0)
    # Right singular vectors are the principal axes.
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vt[:2].T
    # Normalize into a friendly drawing range.
    span = np.abs(coords).max(axis=0)
    span[span == 0] = 1.0
    return coords / span


def build_payload(vectors: np.ndarray, docs: list[dict]) -> dict:
    coords = project(vectors)
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, -1.0)
    order = np.argsort(-sims, axis=1)[:, :NEIGHBOURS]

    items = []
    for i, doc in enumerate(docs):
        text = doc.get("text", "")
        items.append(
            {
                "i": i,
                "id": doc.get("id", ""),
                "t": doc.get("title") or doc.get("id") or f"#{i}",
                "s": doc.get("source", ""),
                "c": doc.get("category") or doc.get("subcategory") or "",
                "u": doc.get("url", ""),
                "x": round(float(coords[i][0]), 4),
                "y": round(float(coords[i][1]), 4),
                "len": len(text),
                "txt": text[:SNIPPET] + ("…" if len(text) > SNIPPET else ""),
                "n": [[int(j), round(float(sims[i][j]), 4)] for j in order[i]],
            }
        )

    off = sims[sims > -1]
    return {
        "docs": items,
        "sources": sorted({d["s"] for d in items}),
        "stats": {
            "count": len(items),
            "dims": int(vectors.shape[1]),
            "meanSim": round(float(off.mean()), 3),
            "p95Sim": round(float(np.percentile(off, 95)), 3),
        },
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vector Store Explorer</title>
<style>
:root {
  --bg:#f6f7f9; --panel:#fff; --ink:#14171a; --muted:#6b7280; --line:#e5e7eb;
  --accent:#1f6feb; --grid:#eceef1;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --panel:#171a21; --ink:#e8eaed; --muted:#9aa3af;
          --line:#262b34; --accent:#4d8dfd; --grid:#1c2028; }
}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);
     font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:.7rem 1rem;border-bottom:1px solid var(--line);display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
h1{font-size:.95rem;margin:0;font-weight:600}
.stat{color:var(--muted);font-size:.8rem}
input[type=search]{flex:1;min-width:12rem;padding:.45rem .6rem;border:1px solid var(--line);
     border-radius:8px;background:var(--panel);color:var(--ink)}
main{flex:1;display:flex;min-height:0}
#plot{flex:1;position:relative;min-width:0}
canvas{width:100%;height:100%;display:block;cursor:crosshair}
aside{width:24rem;border-left:1px solid var(--line);background:var(--panel);overflow-y:auto;padding:1rem}
#tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);
     border-radius:6px;padding:.3rem .5rem;font-size:.78rem;display:none;max-width:18rem}
.legend{display:flex;gap:.75rem;flex-wrap:wrap;font-size:.78rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:.3rem;cursor:pointer;user-select:none}
.legend i{width:.6rem;height:.6rem;border-radius:50%;display:inline-block}
.legend .off{opacity:.35;text-decoration:line-through}
h2{font-size:.9rem;margin:0 0 .2rem}
.meta{color:var(--muted);font-size:.78rem;margin-bottom:.7rem}
.body{white-space:pre-wrap;font-size:.83rem;border-top:1px solid var(--line);padding-top:.7rem;margin-bottom:1rem}
.nb{display:flex;justify-content:space-between;gap:.5rem;padding:.3rem 0;border-bottom:1px solid var(--line);
    cursor:pointer;font-size:.8rem}
.nb:hover{color:var(--accent)}
.nb b{font-variant-numeric:tabular-nums;font-weight:600;color:var(--muted)}
.hint{color:var(--muted);font-size:.83rem}
</style></head><body>
<header>
  <h1>Vector Store</h1>
  <span class="stat" id="stats"></span>
  <input type="search" id="q" placeholder="Filter by text, title or category…">
  <span class="legend" id="legend"></span>
</header>
<main>
  <div id="plot"><canvas id="c"></canvas><div id="tip"></div></div>
  <aside id="side"><p class="hint">Click a point to inspect a document. Nearest
  neighbours are computed from the stored vectors — no API calls.</p></aside>
</main>
<script>
const DATA = __DATA__;
const docs = DATA.docs;
const PALETTE = ['#1f6feb','#e0662b','#12a150','#a855f7','#e11d48','#0891b2'];
const colorOf = {};
DATA.sources.forEach((s,i) => colorOf[s] = PALETTE[i % PALETTE.length]);
const hidden = new Set();
let filter = '', selected = null, hover = null;

document.getElementById('stats').textContent =
  `${DATA.stats.count} docs · ${DATA.stats.dims} dims · mean similarity ${DATA.stats.meanSim} · p95 ${DATA.stats.p95Sim}`;

const legend = document.getElementById('legend');
DATA.sources.forEach(s => {
  const el = document.createElement('span');
  el.innerHTML = `<i style="background:${colorOf[s]}"></i>${s}`;
  el.onclick = () => { hidden.has(s) ? hidden.delete(s) : hidden.add(s);
                       el.classList.toggle('off'); draw(); };
  legend.appendChild(el);
});

const canvas = document.getElementById('c'), ctx = canvas.getContext('2d');
const tip = document.getElementById('tip');
let W = 0, H = 0, pts = [];

function matches(d) {
  if (hidden.has(d.s)) return false;
  if (!filter) return true;
  const f = filter.toLowerCase();
  return d.t.toLowerCase().includes(f) || d.txt.toLowerCase().includes(f)
      || (d.c || '').toLowerCase().includes(f);
}

function resize() {
  const r = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  canvas.width = W * dpr; canvas.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  const pad = 28;
  const sx = v => pad + (v + 1) / 2 * (W - pad * 2);
  const sy = v => pad + (1 - (v + 1) / 2) * (H - pad * 2);

  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--grid');
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const x = pad + g / 4 * (W - pad * 2), y = pad + g / 4 * (H - pad * 2);
    ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, H - pad); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - pad, y); ctx.stroke();
  }

  pts = [];
  for (const d of docs) {
    const on = matches(d);
    const x = sx(d.x), y = sy(d.y);
    pts.push({ d, x, y, on });
    ctx.globalAlpha = on ? 0.85 : 0.07;
    ctx.fillStyle = colorOf[d.s] || '#888';
    ctx.beginPath(); ctx.arc(x, y, 3.2, 0, 6.284); ctx.fill();
  }

  // Draw the selected document's neighbours on top, joined by lines.
  if (selected !== null) {
    const s = pts[selected];
    ctx.globalAlpha = 0.5; ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--accent');
    for (const [j] of docs[selected].n) {
      ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(pts[j].x, pts[j].y); ctx.stroke();
    }
    ctx.globalAlpha = 1; ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--accent');
    ctx.beginPath(); ctx.arc(s.x, s.y, 6, 0, 6.284); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

function nearest(mx, my) {
  let best = null, bestD = 100;
  for (let i = 0; i < pts.length; i++) {
    if (!pts[i].on) continue;
    const dx = pts[i].x - mx, dy = pts[i].y - my, dist = dx * dx + dy * dy;
    if (dist < bestD) { bestD = dist; best = i; }
  }
  return best;
}

canvas.addEventListener('mousemove', e => {
  const r = canvas.getBoundingClientRect();
  hover = nearest(e.clientX - r.left, e.clientY - r.top);
  if (hover === null) { tip.style.display = 'none'; return; }
  const d = docs[hover];
  tip.textContent = d.t;
  tip.style.display = 'block';
  tip.style.left = (e.clientX - r.left + 12) + 'px';
  tip.style.top = (e.clientY - r.top + 12) + 'px';
});
canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
canvas.addEventListener('click', () => { if (hover !== null) select(hover); });

function select(i) {
  selected = i;
  const d = docs[i];
  const side = document.getElementById('side');
  side.innerHTML = '';

  const h = document.createElement('h2'); h.textContent = d.t; side.appendChild(h);
  const meta = document.createElement('div'); meta.className = 'meta';
  meta.textContent = [d.s, d.c, d.id, d.len + ' chars'].filter(Boolean).join(' · ');
  side.appendChild(meta);
  if (d.u) {
    const a = document.createElement('a');
    a.href = d.u; a.target = '_blank'; a.rel = 'noreferrer';
    a.textContent = d.u; a.style.fontSize = '.78rem';
    side.appendChild(a);
  }
  const body = document.createElement('div'); body.className = 'body'; body.textContent = d.txt;
  side.appendChild(body);

  const t = document.createElement('div'); t.className = 'meta';
  t.textContent = 'Nearest neighbours (cosine)'; side.appendChild(t);
  for (const [j, score] of d.n) {
    const row = document.createElement('div'); row.className = 'nb';
    row.innerHTML = `<span>${docs[j].t}</span><b>${score.toFixed(3)}</b>`;
    row.onclick = () => select(j);
    side.appendChild(row);
  }
  draw();
}

document.getElementById('q').addEventListener('input', e => { filter = e.target.value; draw(); });
window.addEventListener('resize', resize);
resize();
</script></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(SETTINGS.knowledge_index_path).with_name("index_explorer.html")))
    parser.add_argument("--open", action="store_true", help="Open it when done")
    args = parser.parse_args()

    path = Path(SETTINGS.knowledge_index_path)
    if not path.exists():
        sys.exit(f"No index at {path}. Run: python scripts/build_index.py")

    z = np.load(path, allow_pickle=False)
    vectors, docs = z["vectors"], json.loads(str(z["documents"]))
    print(f"projecting {len(docs)} vectors from {vectors.shape[1]} dimensions to 2")

    payload = build_payload(vectors, docs)
    html = PAGE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))

    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB, self-contained)")

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    else:
        print(f"open it: {out.resolve().as_uri()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
