"""Export a video as a self-contained bilingual player (HTML + mp4) you can open in Safari.

The app's built-in browser blocks media playback on a memory-starved device, so this
writes a plain HTML file next to the downloaded mp4: open it from the Files app and
Safari plays it locally with the same bilingual subtitle layer.

    python3 export.py <videoId> [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ytcap  # noqa: E402

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} · 双语字幕</title>
<style>
  :root{{--bg:#000;--card:#111;--line:#1a1a1a;--text:#fff;--muted:#b4b4b4;--amber:#f2b33d}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--text);
    font:400 14px/1.5 -apple-system,"PingFang SC","Noto Sans SC",Inter,sans-serif}}
  .wrap{{max-width:1100px;margin:0 auto;padding:20px}}
  h1{{font-size:18px;font-weight:600;margin:0 0 4px}}
  .by{{color:var(--muted);font-size:12px;margin-bottom:14px}}
  .stage{{position:relative;background:#000;border-radius:14px;overflow:hidden}}
  video{{width:100%;display:block;background:#000}}
  .subs{{position:absolute;left:5%;right:5%;bottom:5%;text-align:center;pointer-events:none}}
  .subs div{{display:inline-block;max-width:100%;background:rgba(0,0,0,.78);padding:2px 10px;
    font-weight:500;font-size:clamp(15px,2.3vw,24px);line-height:1.5}}
  .subs .t{{color:var(--amber)}}
  .subs div:empty{{display:none}}
  .bar{{display:flex;gap:10px;align-items:center;padding:10px 2px;color:var(--muted);font-size:12px}}
  .bar input[type=range]{{flex:1;accent-color:var(--amber)}}
  button{{background:var(--text);color:#000;border:0;border-radius:999px;padding:6px 14px;font-weight:600}}
  .rows{{margin-top:10px;max-height:44vh;overflow:auto;border:1px solid var(--line);border-radius:14px}}
  .row{{display:flex;gap:12px;padding:8px 14px;border-bottom:1px solid #161616;cursor:pointer}}
  .row:hover{{background:#1c1c1c}}
  .row .t{{flex:0 0 54px;color:#949494}}
  .row .x{{color:var(--amber);font-size:12px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="by">{author} · {duration} · 原文 {src_lang} / 译文 {tgt_lang}（{method}）</p>
  <div class="stage">
    <video id="v" src="{mp4}" playsinline preload="metadata" poster="{poster}"></video>
    <div class="subs"><div id="o"></div><div id="t" class="t"></div></div>
  </div>
  <div class="bar">
    <button id="pp">播放</button>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span id="tm">0:00 / {duration}</span>
  </div>
  <div class="rows" id="rows"></div>
</div>
<script>
const SRC={src}, TGT={tgt};
const v=document.getElementById('v'), o=document.getElementById('o'), t=document.getElementById('t');
const seek=document.getElementById('seek'), tm=document.getElementById('tm'), rows=document.getElementById('rows');
const dur=(s)=>{{s=Math.max(0,Math.floor(s||0));const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0');}};
const idx=(segs,tt)=>{{let lo=0,hi=segs.length-1,i=-1;while(lo<=hi){{const m=(lo+hi)>>1;
  if(segs[m].s<=tt){{i=m;lo=m+1}}else hi=m-1;}}return i;}};
const cur=(segs,tt)=>{{const i=idx(segs,tt);if(i<0)return '';const s=segs[i],n=segs[i+1];
  if(n&&tt>n.s+0.3)return '';return s.x;}};
document.getElementById('pp').onclick=()=>{{v.paused?(v.play(),document.getElementById('pp').textContent='暂停'):
  (v.pause(),document.getElementById('pp').textContent='播放'))}};
seek.oninput=()=>{{if(v.duration)v.currentTime=seek.value/1000*v.duration;}};
rows.innerHTML=SRC.map((s,i)=>`<div class="row" data-i="${{i}}"><span class="t">${{dur(s.s)}}</span>
  <span>${{s.x}}<br><span class="x">${{TGT[i]?TGT[i].x:''}}</span></span></div>`).join('');
rows.onclick=(e)=>{{const r=e.target.closest('.row');if(!r)return;
  const s=SRC[+r.dataset.i];if(s)v.currentTime=s.s;}};
setInterval(()=>{{const tt=v.currentTime;o.textContent=cur(SRC,tt);t.textContent=cur(TGT,tt);
  tm.textContent=dur(tt)+' / '+dur(v.duration);if(v.duration)seek.value=Math.round(1000*tt/v.duration);}},100);
</script>
</body>
</html>
"""


def _dur(s: int) -> str:
    s = max(0, int(s or 0))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return (f"{h}:{m:02d}" if h else str(m)) + f":{x:02d}"


def export(video_id: str, out_dir: Path, src: str = "auto", tgt: str = "zh-Hans") -> tuple:
    media = ytcap.CACHE / "media" / f"{video_id}.mp4"
    if not media.exists():
        raise SystemExit(f"本地没有这个视频的缓存：{media}")
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4 = out_dir / f"{video_id}.mp4"
    if not mp4.exists() or mp4.stat().st_size != media.stat().st_size:
        shutil.copyfile(media, mp4)
    data = ytcap.load(video_id, src=src, tgt=tgt)
    segs = [{"s": s["start"], "x": s["text"]} for s in data["source"]["segments"]]
    tgt_segs = [{"s": s["start"], "x": s["text"]} for s in data["target"]["segments"]]
    if len(tgt_segs) != len(segs):
        # keep the file layout simple: pad or trim so the two arrays stay index-aligned
        tgt_segs = (tgt_segs + [{"s": 0, "x": ""}] * len(segs))[:len(segs)]
    html = TEMPLATE.format(
        title=data["meta"]["title"] or video_id,
        author=data["meta"]["author"],
        duration=_dur(data["meta"]["duration"]),
        src_lang=data["source"]["lang"], tgt_lang=data["target"]["lang"],
        method=data["target"]["method"] or "—",
        mp4=mp4.name,
        poster="",
        src=json.dumps(segs, ensure_ascii=False),
        tgt=json.dumps(tgt_segs, ensure_ascii=False),
    )
    html_path = out_dir / f"{video_id}.html"
    html_path.write_text(html, "utf-8")
    return html_path, mp4


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "export"))
    ap.add_argument("--src", default="auto")
    ap.add_argument("--tgt", default="zh-Hans")
    a = ap.parse_args()
    h, m = export(a.video, Path(a.out), src=a.src, tgt=a.tgt)
    print("html:", h)
    print("mp4 :", m)
