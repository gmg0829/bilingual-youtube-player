"""Tiny HTTP server for the bilingual YouTube player.

    python3 server.py            # http://127.0.0.1:8765
    python3 server.py --port 9000

Endpoints
    GET /                      the player
    GET /api/load?url=…        captions + translation for a video
    GET /api/stream-info?video=…   download/resolve state for the media
    GET /api/local/<id>        the downloaded file (Range aware)
    GET /api/stream/<id>       live passthrough of the upstream media (fallback)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import subprocess
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ytcap  # noqa: E402

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# video_id -> job state for the "prepare and play" pipeline
JOBS: dict = {}
JOBS_LOCK = threading.Lock()

STAGE_ORDER = ["captions", "translate", "download", "mux", "play"]


def _job_set(video_id: str, **kw):
    with JOBS_LOCK:
        j = JOBS.setdefault(video_id, {"stages": {}, "state": "running", "error": ""})
        j.update(kw)


def _stage(video_id: str, stage: str, frac: float, detail: str = ""):
    with JOBS_LOCK:
        j = JOBS.setdefault(video_id, {"stages": {}, "state": "running", "error": ""})
        prev = j["stages"].get(stage) or {}
        j["stages"][stage] = {"pct": round(frac * 100), "detail": detail,
                              "done": frac >= 1.0 or prev.get("done", False)}


def prepare_and_play(video_id: str, tgt: str = "zh-Hans"):
    """captions → translation → download → mux → hand to the native player."""
    try:
        _job_set(video_id, state="running", error="")
        data = ytcap.load(video_id, tgt=tgt, on_progress=lambda st, f, d: _stage(video_id, st, f, d))
        src_n = len(data["source"]["segments"])
        if not src_n:
            raise RuntimeError("这个视频没有字幕，做不到双语版")
        _stage(video_id, "captions", 1.0, f"{data['source']['lang']} · {src_n} 句")
        _stage(video_id, "translate", 1.0, f"{len(data['target']['segments'])} 句")

        _stage(video_id, "download", 0.0, "准备下载")
        path = download_with_progress(video_id, lambda f: _stage(video_id, "download", f, "下载视频"))
        if not path:
            raise RuntimeError("下载失败（YouTube 可能在限流），过一会儿再点一次")

        _stage(video_id, "mux", 0.2, "写入字幕轨")
        out = export_bilingual(video_id, tgt)
        _stage(video_id, "mux", 1.0, out.name)

        _stage(video_id, "play", 0.5, "打开播放器")
        played = False
        try:
            r = subprocess.run(["apple-player", "play", str(out)],
                               capture_output=True, text=True, timeout=45)
            played = "playing" in (r.stdout or "")
        except Exception:
            played = False
        _stage(video_id, "play", 1.0, "已播放" if played else "已生成")
        _job_set(video_id, state="done", error="", file=str(out), name=out.name,
                 size=out.stat().st_size, played=played)
    except Exception as e:
        _job_set(video_id, state="error", error=str(e)[:200])


# video_id -> {"state": "pending"|"ready"|"error", "url":…, "path":…, "progress":…}
STREAMS: dict = {}
STREAMS_LOCK = threading.Lock()


# ---------------------------------------------------------------- media

def download_with_progress(video_id: str, on_progress) -> str:
    """Same as ytcap.download_media but reports yt-dlp's own percentage."""
    import subprocess
    outdir = ytcap.CACHE / "media"
    outdir.mkdir(exist_ok=True)
    dest = outdir / f"{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 1000:
        on_progress(1.0)
        return str(dest)
    for client in ("android", "default"):
        cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--newline",
               "--extractor-args", f"youtube:player_client={client}",
               "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
               "--merge-output-format", "mp4",
               "-o", str(outdir / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
        except Exception:
            continue
        for line in p.stdout:
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                try:
                    on_progress(float(m.group(1)) / 100.0)
                except Exception:
                    pass
        p.wait()
        if dest.exists() and dest.stat().st_size > 1000:
            on_progress(1.0)
            return str(dest)
    return ""


def _media_worker(video_id: str):
    """Download the video once; afterwards playback is served from disk."""
    try:
        def prog(pct):
            with STREAMS_LOCK:
                if video_id in STREAMS:
                    STREAMS[video_id]["progress"] = pct
        path = ytcap.download_media(video_id, prog)
        if not path:
            raise RuntimeError("下载视频流失败，可以改用 YouTube 播放器。")
        with STREAMS_LOCK:
            STREAMS[video_id] = {"state": "ready", "url": f"/api/local/{video_id}",
                                 "path": path, "progress": 100.0}
    except Exception as e:
        with STREAMS_LOCK:
            STREAMS[video_id] = {"state": "error", "error": str(e)[:200]}


def stream_status(video_id: str) -> dict:
    with STREAMS_LOCK:
        st = STREAMS.get(video_id)
    if st and st["state"] in ("ready", "error"):
        return st
    local = ytcap.CACHE / "media" / f"{video_id}.mp4"
    if local.exists() and local.stat().st_size > 1000:
        with STREAMS_LOCK:
            STREAMS[video_id] = {"state": "ready", "url": f"/api/local/{video_id}",
                                 "path": str(local), "progress": 100.0}
        return STREAMS[video_id]
    with STREAMS_LOCK:
        STREAMS[video_id] = {"state": "pending", "progress": 0.0}
    threading.Thread(target=_media_worker, args=(video_id,), daemon=True).start()
    return STREAMS[video_id]


def serve_local(video_id: str, range_header: str | None):
    """Serve the downloaded file with Range support.

    Each response is capped at 2MB: a single 28MB body keeps the page's load event
    pending forever in WKWebView (and makes the tab look wedged).
    """
    path = ytcap.CACHE / "media" / f"{video_id}.mp4"
    if not path.exists():
        raise RuntimeError("本地没有这个视频的缓存")
    size = path.stat().st_size
    start, end = 0, size - 1
    partial = False
    m = re.match(r"bytes=(\d+)-(\d*)$", (range_header or "").strip())
    if m:
        partial = True
        start = int(m.group(1))
        if m.group(2):
            end = min(int(m.group(2)), size - 1)
    cap = 2 * 1024 * 1024
    if end - start + 1 > cap:
        end = min(size - 1, start + cap - 1)
        partial = True
    length = max(0, end - start + 1)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(length)
    return start, end, size, data


def job_state(video_id: str) -> dict:
    with JOBS_LOCK:
        j = JOBS.get(video_id) or {"stages": {}, "state": "idle", "error": ""}
        return {"state": j.get("state", "idle"), "error": j.get("error", ""),
                "stages": {k: dict(v) for k, v in (j.get("stages") or {}).items()},
                "file": j.get("file"), "name": j.get("name"),
                "played": j.get("played", False), "order": STAGE_ORDER}


def export_bilingual(video_id: str, tgt: str = "zh-Hans") -> Path:
    """Mux the bilingual subtitle track into the mp4 (no re-encode) and return the file."""
    import export as exp
    media = ytcap.CACHE / "media" / f"{video_id}.mp4"
    if not (media.exists() and media.stat().st_size > 1000):
        # the page may already be downloading this video; wait for it instead of
        # starting a second yt-dlp that would fight over the same output file
        for _ in range(120):
            with STREAMS_LOCK:
                st = STREAMS.get(video_id) or {}
            if media.exists() and media.stat().st_size > 1000:
                break
            if st.get("state") not in ("pending",):
                break
            time.sleep(1)
    if not (media.exists() and media.stat().st_size > 1000):
        path = ytcap.download_media(video_id)
        if not path:
            raise RuntimeError("视频还没下载下来（YouTube 可能在限流），稍后再试")
        media = Path(path)
    srt = exp.write_srt(video_id, tgt=tgt)
    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    out = outdir / f"{video_id}-双语.mp4"
    if not out.exists() or out.stat().st_mtime < max(media.stat().st_mtime, srt.stat().st_mtime):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(media), "-i", str(srt),
               "-map", "0:v", "-map", "0:a?", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=zho", "-metadata:s:s:0", "title=EN+ZH",
               "-disposition:s:0", "default", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("内嵌字幕失败：" + (r.stderr or "").strip()[-160:])
    return out


def proxy_stream(video_id: str, range_header: str | None):
    """Live passthrough of the upstream media (used when the local copy is missing)."""
    info = ytcap.get_cached_stream(video_id)
    for _ in range(180):
        if info:
            break
        with STREAMS_LOCK:
            st = STREAMS.get(video_id) or {}
        if st.get("state") == "ready":
            info = st["info"]
            break
        if st.get("state") == "error":
            raise RuntimeError(st.get("error", "stream error"))
        info = ytcap.get_cached_stream(video_id)
        if info:
            break
        time.sleep(1)
    if not info:
        raise RuntimeError("解析视频流超时")
    upstream = info["url"]
    headers = {"User-Agent": UA}
    # An open-ended range ("bytes=0-") would pull the whole file in one request;
    # cap it so the browser asks for the rest itself.
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)$", range_header.strip())
        if m and not m.group(2):
            start = int(m.group(1))
            headers["Range"] = f"bytes={start}-{start + 8 * 1024 * 1024 - 1}"
        else:
            headers["Range"] = range_header
    req = urllib.request.Request(upstream, headers=headers)
    return urllib.request.urlopen(req, timeout=60)


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "YTBilingual/1.0"
    protocol_version = "HTTP/1.1"
    wbufsize = 256 * 1024          # buffered writes — tiny writes cost 40ms each
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ------------------------------------------------------------ routing
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/api/health":
                return self._json({"ok": True, "llm": bool(os.environ.get("YTB_LLM_KEY"))})
            if u.path == "/api/load":
                url = (q.get("url") or [""])[0].strip()
                vid = ytcap.parse_video_id(url)
                if not vid:
                    return self._json({"error": "无法识别这个链接，请贴一个 YouTube 视频链接（watch / youtu.be / shorts）。"}, 400)
                src = (q.get("src") or ["auto"])[0]
                tgt = (q.get("tgt") or ["zh-Hans"])[0]
                prefer = (q.get("prefer") or ["auto"])[0]
                return self._json(ytcap.load(vid, src=src, tgt=tgt, prefer=prefer))
            if u.path == "/api/tracks":
                vid = (q.get("id") or [""])[0]
                if not ytcap.parse_video_id(vid):
                    return self._json({"error": "bad id"}, 400)
                return self._json({"tracks": ytcap.list_tracks(vid),
                                   "languages": ytcap.translation_languages(vid)})
            if u.path == "/api/stream-info":
                vid = (q.get("video") or [""])[0]
                if not ytcap.parse_video_id(vid):
                    return self._json({"error": "bad id"}, 400)
                st = stream_status(vid)
                out = {"state": st["state"], "url": st.get("url"),
                       "progress": st.get("progress", 0)}
                if st.get("error"):
                    out["error"] = st["error"]
                return self._json(out)
            if u.path == "/api/prepare":
                vid = (q.get("video") or [""])[0]
                tgt = (q.get("tgt") or ["zh-Hans"])[0]
                if not ytcap.parse_video_id(vid):
                    return self._json({"error": "bad id"}, 400)
                with JOBS_LOCK:
                    running = (JOBS.get(vid) or {}).get("state") == "running"
                    if not running:
                        JOBS[vid] = {"stages": {}, "state": "running", "error": ""}
                if not running:
                    threading.Thread(target=prepare_and_play, args=(vid, tgt), daemon=True).start()
                return self._json(job_state(vid))
            if u.path == "/api/prepare-status":
                vid = (q.get("video") or [""])[0]
                if not ytcap.parse_video_id(vid):
                    return self._json({"error": "bad id"}, 400)
                return self._json(job_state(vid))
            if u.path == "/api/export":
                vid = (q.get("video") or [""])[0]
                tgt = (q.get("tgt") or ["zh-Hans"])[0]
                if not ytcap.parse_video_id(vid):
                    return self._json({"error": "bad id"}, 400)
                path = export_bilingual(vid, tgt)
                played = False
                try:
                    r = subprocess.run(["apple-player", "play", str(path)],
                                       capture_output=True, text=True, timeout=45)
                    played = '"status" : "playing"' in (r.stdout or "") or "playing" in (r.stdout or "")
                except Exception:
                    played = False
                return self._json({"ok": True, "file": str(path), "name": path.name,
                                   "size": path.stat().st_size, "played": played})
            if u.path.startswith("/api/local/"):
                return self._local(u.path[len("/api/local/"):])
            if u.path.startswith("/api/stream/"):
                return self._live(u.path[len("/api/stream/"):])
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:
            traceback.print_exc()
            try:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                return

    def _local(self, vid: str):
        if not ytcap.parse_video_id(vid):
            return self._send(404, b"bad id", "text/plain; charset=utf-8")
        try:
            start, end, size, data = serve_local(vid, self.headers.get("Range"))
        except Exception as e:
            return self._send(404, str(e)[:120].encode("utf-8"), "text/plain; charset=utf-8")
        # A truncated body must never be labelled 200 — say 206 with the real range.
        partial = (start != 0 or end != size - 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _live(self, vid: str):
        if not ytcap.parse_video_id(vid):
            return self._send(404, b"bad id", "text/plain; charset=utf-8")
        rng = self.headers.get("Range")
        try:
            upstream = proxy_stream(vid, rng)
        except Exception as e:
            return self._send(502, str(e)[:200].encode("utf-8"), "text/plain; charset=utf-8")
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "video/mp4"))
        self.send_header("Accept-Ranges", "bytes")
        has_len = False
        for h in ("Content-Length", "Content-Range"):
            if upstream.headers.get(h):
                self.send_header(h, upstream.headers.get(h))
                has_len = True
        if not has_len:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            buf = bytearray()
            while True:
                chunk = upstream.read(512 * 1024)
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= 256 * 1024:
                    self.wfile.write(bytes(buf))
                    buf.clear()
            if buf:
                self.wfile.write(bytes(buf))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _static(self, rel: str):
        p = (STATIC / rel).resolve()
        if not str(p).startswith(str(STATIC.resolve())) or not p.exists():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(200, p.read_bytes(), ctype)


def prune_media(max_bytes: int = 1500 * 1024 * 1024):
    """Keep the download cache bounded — oldest videos go first."""
    media = ytcap.CACHE / "media"
    try:
        files = sorted(media.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return
    total = sum(f.stat().st_size for f in files)
    while files and total > max_bytes:
        f = files.pop(0)
        try:
            total -= f.stat().st_size
            f.unlink()
            print("pruned", f.name)
        except Exception:
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    prune_media()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"▶ bilingual YouTube player  →  http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
