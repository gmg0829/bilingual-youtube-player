"""Caption retrieval + bilingual translation for arbitrary YouTube videos.

Two caption pipelines, tried in this order:

1. innertube (youtube-transcript-api) — the `get_transcript` endpoint. This is the only
   path that still returns caption bodies: the classic `timedtext` URLs now answer 200
   with an empty body unless a POT token is attached.
   It also exposes YouTube's own machine translation of any track (`translate()`).

2. watch page — `ytInitialPlayerResponse` gives the full track list (manual + ASR),
   the video metadata and the list of languages YouTube can translate into.
   One request per video, cached, so the player still knows the available languages
   even when the caption bodies are blocked.

Everything is cached on disk (cache/), so re-opening a video costs zero YouTube requests
and the app keeps working while YouTube rate-limits the IP.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PLAYER_TTL = 6 * 3600       # caption baseUrls carry a ~6h signature
TRACK_TTL = 30 * 24 * 3600

# ---------------------------------------------------------------- helpers

def _cache_get(name: str, ttl: float):
    p = CACHE / name
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
            return json.loads(p.read_text("utf-8"))
    except Exception:
        return None
    return None


def _cache_get_stale(name: str):
    p = CACHE / name
    try:
        if p.exists():
            return json.loads(p.read_text("utf-8"))
    except Exception:
        return None
    return None


def _cache_put(name: str, data) -> None:
    try:
        tmp = CACHE / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(CACHE / name)
    except Exception:
        pass


def _http(url: str, timeout: int = 25, data: bytes | None = None, headers: dict | None = None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _extract_json(text: str, marker: str):
    """Pull the JSON object that follows `marker` out of a JS payload (brace-balanced)."""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except Exception:
                    return None
    return None


# ---------------------------------------------------------------- video id

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(url: str) -> str | None:
    """Accepts watch/youtu.be/shorts/embed/live links, or a bare 11-char id."""
    url = (url or "").strip().strip("<>\"'")
    if not url:
        return None
    if _ID.match(url):
        return url
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower().replace("www.", "").replace("m.", "")
    path = p.path or ""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    if host == "youtu.be":
        m = re.match(r"/([A-Za-z0-9_-]{11})", path)
        if m:
            return m.group(1)
    if "youtube.com" in host or host == "youtube-nocookie.com":
        m = re.match(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- innertube

_MEM: dict = {}
_MEM_TL: dict = {}

# Chinese and a few other languages ship under regional codes; map a requested code
# onto the codes YouTube actually uses.
NATIVE_ALIASES = {
    "zh-Hans": ["zh-Hans", "zh-CN", "zh-SG", "zh-Hans-CN"],
    "zh-Hant": ["zh-Hant", "zh-TW", "zh-HK", "zh-MO"],
    "en": ["en", "en-US", "en-GB"],
    "es": ["es", "es-419", "es-ES", "es-MX"],
    "pt": ["pt", "pt-BR", "pt-PT"],
    "fr": ["fr", "fr-CA"],
    "de": ["de", "de-DE"],
    "ja": ["ja", "ja-JP"],
    "ko": ["ko", "ko-KR"],
    "it": ["it", "it-IT"],
    "nl": ["nl", "nl-NL"],
    "ru": ["ru", "ru-RU"],
    "ar": ["ar", "ar-EG"],
    "hi": ["hi", "hi-IN"],
    "id": ["id", "id-ID"],
    "th": ["th", "th-TH"],
    "vi": ["vi", "vi-VN"],
    "tr": ["tr", "tr-TR"],
    "pl": ["pl", "pl-PL"],
}


def _api():
    from youtube_transcript_api import YouTubeTranscriptApi
    return YouTubeTranscriptApi()


def _tl(video_id: str):
    """TranscriptList, kept in memory so we pay the list request only once per video."""
    if video_id not in _MEM_TL:
        _MEM_TL[video_id] = _api().list(video_id)
    return _MEM_TL[video_id]


def _with_retry(fn, tries: int = 3, waits=(0, 6, 20)):
    """innertube occasionally answers 'IpBlocked'; back off and try again."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e)
            transient = ("IpBlocked" in type(e).__name__) or ("Connection reset" in msg) \
                or ("timed out" in msg.lower()) or ("Too Many Requests" in msg)
            if not transient or i == tries - 1:
                raise
            time.sleep(waits[i] if i < len(waits) else 20)
    raise last


def _inn_list(video_id: str, refresh: bool = False) -> list:
    """Caption tracks as innertube sees them: [{code,label,generated,translatable}]."""
    if not refresh and video_id in _MEM:
        return _MEM[video_id]
    cached = _cache_get(f"it_{video_id}.json", TRACK_TTL)
    if cached:
        _MEM[video_id] = cached
        return cached
    out = []
    try:
        tl = _api().list(video_id)
        for t in tl:
            out.append({
                "code": t.language_code,
                "label": t.language or t.language_code,
                "generated": bool(t.is_generated),
                "translatable": bool(t.is_translatable),
            })
    except Exception as e:
        stale = _cache_get_stale(f"it_{video_id}.json")
        if stale:
            _MEM[video_id] = stale
            return stale
        raise RuntimeError(f"YouTube 拒绝了字幕请求（{type(e).__name__}）。稍后重试即可，已缓存的视频不受影响。")
    _cache_put(f"it_{video_id}.json", out)
    _MEM[video_id] = out
    return out


def _snips_from(raw) -> list:
    out = []
    for s in raw:
        if isinstance(s, dict):
            start = s.get("start", 0.0)
            dur_ = s.get("duration", 0.0)
            text = s.get("text", "")
        else:
            start = getattr(s, "start", 0.0)
            dur_ = getattr(s, "duration", 0.0)
            text = getattr(s, "text", "")
        text = (text or "").replace("\n", " ").strip()
        if text:
            out.append({"start": float(start), "duration": float(dur_), "text": text})
    return out


def _inn_fetch(video_id: str, code: str) -> list:
    key = f"itr_{video_id}_{code}.json"
    cached = _cache_get(key, TRACK_TTL)
    if cached is not None:
        return cached
    tr = _with_retry(lambda: _tl(video_id).find_transcript([code]))
    segs = _snips_from(_with_retry(lambda: tr.fetch()))
    if segs:
        _cache_put(key, segs)
    return segs


def _inn_translate(video_id: str, code: str, target: str) -> list:
    key = f"itl_{video_id}_{code}_{target}.json"
    cached = _cache_get(key, TRACK_TTL)
    if cached is not None:
        return cached
    tr = _with_retry(lambda: _tl(video_id).find_transcript([code]))
    tr2 = _with_retry(lambda: tr.translate(target))
    segs = _snips_from(tr2.fetch() if hasattr(tr2, "fetch") else tr2)
    if segs:
        _cache_put(key, segs)
    return segs


# ---------------------------------------------------------------- watch page

def _player_response(video_id: str, refresh: bool = False):
    name = f"pr_{video_id}.json"
    if not refresh:
        cached = _cache_get(name, PLAYER_TTL)
        if cached:
            return cached
    html = _http(f"https://www.youtube.com/watch?v={video_id}&hl=en").decode("utf-8", "replace")
    pr = _extract_json(html, "ytInitialPlayerResponse")
    if pr is None:
        raise RuntimeError("could not read the video page (YouTube may be rate-limiting this IP)")
    _cache_put(name, pr)
    return pr


def video_meta(video_id: str) -> dict:
    try:
        pr = _player_response(video_id)
    except Exception:
        return {"title": "", "author": "", "duration": 0,
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}
    vd = pr.get("videoDetails") or {}
    thumbs = vd.get("thumbnail") or {}
    meta = {
        "title": vd.get("title") or "",
        "author": vd.get("author") or "",
        "duration": int(vd.get("lengthSeconds") or 0),
        "thumbnail": thumbs.get("thumbnails", [{}])[-1].get("url", "") if thumbs else "",
    }
    if not meta["thumbnail"]:
        meta["thumbnail"] = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return meta


def _label(track: dict) -> str:
    name = track.get("name") or {}
    if "simpleText" in name:
        return name["simpleText"]
    return "".join(r.get("text", "") for r in name.get("runs") or [])


def _watch_tracks(video_id: str) -> list:
    try:
        pr = _player_response(video_id)
    except Exception:
        return []
    r = ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
    out = []
    for t in r.get("captionTracks") or []:
        code = t.get("languageCode")
        if not code:
            continue
        out.append({
            "code": code,
            "label": _label(t) or code,
            "generated": t.get("kind") == "asr",
            "translatable": bool(t.get("isTranslatable", True)),
            "baseUrl": t.get("baseUrl", ""),
        })
    return out


def translation_languages(video_id: str) -> list:
    """Languages YouTube's own machine translation can produce for this video."""
    try:
        pr = _player_response(video_id)
    except Exception:
        return []
    r = ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
    langs = []
    for l in r.get("translationLanguages") or []:
        code = l.get("languageCode")
        if not code:
            continue
        nm = l.get("languageName") or {}
        name = nm.get("simpleText") or "".join(x.get("text", "") for x in nm.get("runs") or [])
        langs.append({"code": code, "label": name or code})
    return langs


# ---------------------------------------------------------------- tracks

def list_tracks(video_id: str) -> list:
    """Every caption track the video offers (innertube first, watch page as backup)."""
    try:
        return _inn_list(video_id)
    except Exception:
        return _watch_tracks(video_id)


def _pick_track(tracks: list, code: str):
    return next((t for t in tracks if t["code"] == code), None)


def fetch_track(video_id: str, code: str) -> list:
    """Segments of one caption track (manual or auto-generated)."""
    try:
        return _inn_fetch(video_id, code)
    except Exception:
        pass
    # fallback: classic timedtext (still works for some videos)
    for t in _watch_tracks(video_id):
        if t["code"] == code and t.get("baseUrl"):
            return _timedtext(t["baseUrl"], f"tr_{video_id}_{code}.json")
    return []


def fetch_translated(video_id: str, code: str, target: str) -> list:
    """YouTube's own machine translation of a track."""
    try:
        segs = _inn_translate(video_id, code, target)
        if segs:
            return segs
    except Exception:
        pass
    for t in _watch_tracks(video_id):
        if t["code"] == code and t.get("baseUrl"):
            url = t["baseUrl"] + "&tlang=" + urllib.parse.quote(target) + "&fmt=json3"
            return _timedtext(url, f"tl_{video_id}_{code}_{target}.json")
    return []


def _timedtext(url: str, key: str) -> list:
    cached = _cache_get(key, TRACK_TTL)
    if cached is not None:
        return cached
    segs = []
    try:
        raw = _http(url + "&fmt=json3")
        segs = _parse_json3(raw)
        if not segs:
            segs = _parse_vtt(_http(url + "&fmt=vtt"))
    except Exception:
        segs = []
    _cache_put(key, segs)
    return segs


def _parse_json3(raw: bytes) -> list:
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return []
    segs = []
    for ev in doc.get("events") or []:
        if "tStartMs" not in ev:
            continue
        text = "".join(p.get("utf8", "") for p in (ev.get("segs") or []))
        text = text.replace("\n", " ").strip()
        if text:
            segs.append({"start": ev["tStartMs"] / 1000.0,
                         "duration": (ev.get("dDurationMs") or 0) / 1000.0,
                         "text": text})
    return segs


def _parse_vtt(raw: bytes) -> list:
    text = raw.decode("utf-8", "replace")
    segs, cur = [], None

    def sec(t):
        t = t.replace(",", ".")
        h, mnt, s = t.split(":")
        return int(h) * 3600 + int(mnt) * 60 + float(s)

    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[.,]\d{3})", line)
        if m:
            if cur:
                segs.append(cur)
            cur = {"start": sec(m.group(1)),
                   "duration": max(0.0, sec(m.group(2)) - sec(m.group(1))), "text": ""}
        elif cur is not None and line and not line.isdigit() and not line.startswith("WEBVTT"):
            cur["text"] = (cur["text"] + " " + line).strip()
    if cur:
        segs.append(cur)
    return [s for s in segs if s["text"]]


# ---------------------------------------------------------------- direct stream

_STREAM_CACHE: dict = {}


def _pick_stream_format(info: dict) -> dict | None:
    """Prefer a progressive mp4 (video+audio in one file) — no muxing needed."""
    fmts = info.get("formats") or []
    prog = [f for f in fmts
            if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
            and f.get("url")]
    if not prog:
        return None
    prog.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    best = prog[-1]
    for f in reversed(prog):
        if (f.get("height") or 0) <= 720:
            best = f
            break
    return best


# Player clients in order of preference. The `android` client hands out media URLs that
# are actually fetchable; android_vr/web need a poToken and answer 403 without one.
STREAM_CLIENTS = ["android", "android_vr", "web", "default"]


def _verify_stream(url: str) -> bool:
    """Make sure the CDN really serves this URL before we hand it to the browser."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read(2048)
            return r.status in (200, 206)
    except Exception:
        return False


def _yt_dlp_info(video_id: str, client: str) -> dict | None:
    import subprocess
    args = ["yt-dlp", "--no-warnings", "--no-playlist", "-J"]
    if client != "default":
        args += ["--extractor-args", f"youtube:player_client={client}"]
    args.append(f"https://www.youtube.com/watch?v={video_id}")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=240)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def download_media(video_id: str, on_progress=None) -> str:
    """Download the progressive mp4 to disk so playback is instant and seekable.

    Returns the local path, or "" on failure.
    """
    import subprocess
    outdir = CACHE / "media"
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 1000:
        return str(dest)
    for client in ("android", "default"):
        cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--newline",
               "--extractor-args", f"youtube:player_client={client}",
               "-f", "best[ext=mp4][height<=720]",
               "-o", str(outdir / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
        except Exception:
            continue
        for line in p.stdout:
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m and on_progress:
                try:
                    on_progress(float(m.group(1)))
                except Exception:
                    pass
        p.wait()
        if dest.exists() and dest.stat().st_size > 1000:
            return str(dest)
    return ""


def get_cached_stream(video_id: str) -> dict | None:
    """The resolved stream for a video, if it is already on disk."""
    return _cache_get(f"stream_{video_id}.json", 3 * 3600)


def get_stream(video_id: str, refresh: bool = False) -> dict:
    """Direct media URL for a video (progressive mp4), resolved with yt-dlp."""
    key = f"stream_{video_id}.json"
    if not refresh:
        cached = _cache_get(key, 3 * 3600)
        if cached:
            return cached
    last_err = "解析视频流失败"
    for client in STREAM_CLIENTS:
        info = _yt_dlp_info(video_id, client)
        if not info:
            continue
        fmt = _pick_stream_format(info)
        if not fmt:
            continue
        if not _verify_stream(fmt["url"]):
            last_err = f"视频流被 YouTube 拒绝（客户端 {client}）"
            continue
        data = {
            "url": fmt["url"],
            "ext": fmt.get("ext") or "mp4",
            "height": fmt.get("height") or 0,
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "client": client,
        }
        _cache_put(key, data)
        return data
    raise RuntimeError(last_err + "。可以改用右上角的 YT 按钮（YouTube 官方播放器）。")


# ---------------------------------------------------------------- translation

_CC = {}


def _opencc(text: str, cfg: str = "t2s") -> str:
    try:
        from opencc import OpenCC
        if cfg not in _CC:
            _CC[cfg] = OpenCC(cfg)
        return _CC[cfg].convert(text)
    except Exception:
        return text


# Google's public translate endpoint: no key, and it keeps newline-separated input
# line-aligned, which is exactly what subtitle segmentation needs.
GTX_LANG = {"zh-Hans": "zh-CN", "zh-Hant": "zh-TW", "es-419": "es", "pt": "pt",
            "iw": "he", "in": "id", "fil": "tl"}


def _gtx_request(text: str, tl: str) -> list:
    import urllib.parse
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text})
    raw = _http(url, timeout=45)
    data = json.loads(raw.decode("utf-8", "replace"))
    return data[0] or []


def _parse_rolling_vtt(text: str) -> list:
    """Parse YouTube's rolling auto-caption VTT (word-level <c> tags).

    A cue without <c> tags is a completed line; its display span runs from the end of
    the previous completed line to its own end.
    """
    def sec(t):
        t = t.replace(",", ".")
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    cues = []
    for m in re.finditer(
            r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)",
            text, re.S):
        cues.append((sec(m.group(1)), sec(m.group(2)), m.group(3).strip()))
    lines = []
    prev_end = cues[0][0] if cues else 0.0
    for start, end, raw in cues:
        if "<c>" in raw:
            continue
        clean = re.sub(r"<[^>]+>", "", raw).replace("\n", " ").strip()
        if not clean:
            continue
        lines.append({"start": round(prev_end, 2),
                      "duration": round(max(0.1, end - prev_end), 2),
                      "text": clean})
        prev_end = end
    return lines


def yt_mt_segments(video_id: str, target: str) -> list:
    """YouTube's own automatic-translated subtitle track — free, fetched with yt-dlp."""
    import subprocess
    key = f"ytsub_{video_id}_{target}.json"
    cached = _cache_get(key, TRACK_TTL)
    if cached:
        return cached
    outdir = CACHE / "subs"
    outdir.mkdir(parents=True, exist_ok=True)
    lang = {"zh-Hans": "zh-Hans", "zh-Hant": "zh-Hant", "ja": "ja", "ko": "ko",
            "fr": "fr", "es": "es", "de": "de", "pt": "pt", "ru": "ru"}.get(target, target)
    cmd = ["yt-dlp", "--no-warnings", "--skip-download", "--write-auto-subs",
           "--sub-langs", lang, "--sub-format", "vtt",
           "--extractor-args", "youtube:player_client=android",
           "-o", str(outdir / "%(id)s"),
           f"https://www.youtube.com/watch?v={video_id}"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except Exception:
        return []
    files = sorted(outdir.glob(f"{video_id}.{lang}*.vtt"))
    if not files:
        return []
    try:
        lines = _parse_rolling_vtt(files[0].read_text("utf-8", "replace"))
    except Exception:
        return []
    if lines:
        _cache_put(key, lines)
    return lines


def gtx_translate(video_id: str, segments: list, target: str) -> list:
    """Machine translation that always works (no key), used when YouTube's own MT fails.

    Batches are sent in parallel; the endpoint keeps newline-separated input line-aligned,
    so a batch of N lines comes back as N lines and the timing stays exact.
    """
    if not segments:
        return []
    key = f"gtx_{video_id}_{target}.json"
    cached = _cache_get(key, TRACK_TTL)
    if cached:
        return cached
    tl = GTX_LANG.get(target, target)
    batch = 40

    def work(chunk):
        lines = [(x.get("text") or "").replace("\n", " ").strip() for x in chunk]
        try:
            pieces = _gtx_request("\n".join(lines), tl)
            joined = "".join((p[0] or "") for p in pieces)
        except Exception:
            return None
        parts = [p.strip() for p in joined.split("\n")]
        if len(parts) != len(chunk):
            parts = []
            for line in lines:
                try:
                    pieces = _gtx_request(line, tl)
                    parts.append("".join((p[0] or "") for p in pieces).strip())
                except Exception:
                    return None
        return [{"start": x["start"], "duration": x["duration"], "text": t}
                for x, t in zip(chunk, parts)]

    chunks = [segments[i:i + batch] for i in range(0, len(segments), batch)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(work, chunks))
    if any(r is None for r in results):
        return []
    out = [item for r in results for item in r]
    if out:
        _cache_put(key, out)
    return out


# ---------------------------------------------------------------- assembly

def _align(source: list, target: list) -> list:
    """Pair translated segments with source timing (same order -> same index)."""
    if not target:
        return []
    if len(target) == len(source):
        return [{"start": s["start"], "duration": s["duration"], "text": t["text"]}
                for s, t in zip(source, target)]
    starts = [s["start"] for s in source]
    out = []
    for t in target:
        i = bisect.bisect_left(starts, t.get("start", 0))
        cand = [k for k in (i - 1, i) if 0 <= k < len(source)]
        if not cand:
            continue
        k = min(cand, key=lambda k: abs(starts[k] - t.get("start", 0)))
        out.append({"start": source[k]["start"], "duration": source[k]["duration"], "text": t["text"]})
    return out


def _choose_source(tracks: list, src: str) -> list:
    """Candidate tracks to try, best first."""
    cands = []
    if src and src != "auto":
        hit = _pick_track(tracks, src)
        if hit:
            cands.append(hit)
    for code in ("en", "en-US", "en-GB"):
        for t in tracks:
            if t["code"] == code and t not in cands:
                cands.append(t)
    manual = [t for t in tracks if not t["generated"] and t not in cands]
    auto_en = [t for t in tracks if t["generated"] and t["code"].startswith("en") and t not in cands]
    auto = [t for t in tracks if t["generated"] and t not in cands]
    return cands + manual + auto_en + auto


def load(video_id: str, src: str = "auto", tgt: str = "zh-Hans", prefer: str = "auto") -> dict:
    """Everything the player needs for one video: meta, tracks, source text, translation."""
    tracks = list_tracks(video_id)
    meta = video_meta(video_id)
    notices = []

    # ---- pick the source track (first candidate that actually has text)
    source_segs, source_lang, picked = [], "", None
    for cand in _choose_source(tracks, src):
        segs = fetch_track(video_id, cand["code"])
        if segs:
            source_segs, source_lang, picked = segs, cand["code"], cand
            break
    if picked is None:
        notices.append("没有取到字幕轨道（该视频可能没有字幕，或 YouTube 暂时限制了请求）。")

    # ---- translation
    target_segs, method = [], "none"
    if source_segs and tgt and tgt != "none":
        if tgt == source_lang:
            method = "same"
        else:
            # 1) a track the video already carries in the target language
            for code in NATIVE_ALIASES.get(tgt, [tgt]):
                if code == source_lang:
                    continue
                if not _pick_track(tracks, code):
                    continue
                segs = fetch_track(video_id, code)
                if not segs:
                    continue
                if tgt == "zh-Hans" and code in ("zh-TW", "zh-HK", "zh-MO", "zh-Hant"):
                    segs = [{"start": s["start"], "duration": s["duration"], "text": _opencc(s["text"], "t2s")} for s in segs]
                    method = "native+t2s"
                elif tgt == "zh-Hant" and code in ("zh-CN", "zh-SG", "zh-Hans"):
                    segs = [{"start": s["start"], "duration": s["duration"], "text": _opencc(s["text"], "s2t")} for s in segs]
                    method = "native+s2t"
                else:
                    method = "native"
                target_segs = segs
                break
            # 2) translation providers, in order of preference — every one of them is
            #    free except the LLM, and a failure simply falls through to the next
            if prefer == "llm":
                order = ["llm", "ytmt", "gtx"]
            elif prefer == "yt":
                order = ["ytmt", "gtx"]
            elif prefer == "mt":
                order = ["gtx", "ytmt"]
            else:
                order = (["llm"] if os.environ.get("YTB_LLM_KEY") else []) + ["ytmt", "gtx"]

            for prov in order:
                if target_segs:
                    break
                if prov == "llm":
                    target_segs = llm_translate(source_segs, tgt) or []
                    method = "llm" if target_segs else "none"
                elif prov == "ytmt":
                    target_segs = yt_mt_segments(video_id, tgt)
                    method = "yt-mt" if target_segs else "none"
                elif prov == "gtx":
                    target_segs = gtx_translate(video_id, source_segs, tgt)
                    method = "mt" if target_segs else "none"

            # 3) YouTube innertube translation (last resort, often empty)
            if not target_segs and prefer != "llm":
                codes = [tgt] if tgt != "zh-Hans" else ["zh-Hans", "zh-Hant"]
                for code in codes:
                    cand = fetch_translated(video_id, source_lang, code)
                    if cand:
                        if code == "zh-Hant" and tgt == "zh-Hans":
                            cand = [{"start": c["start"], "duration": c["duration"],
                                     "text": _opencc(c["text"], "t2s")} for c in cand]
                            method = "yt-mt+t2s"
                        else:
                            method = "yt-mt"
                        target_segs = cand
                        break
            if not target_segs:
                notices.append(f"没能得到 {tgt} 的译文，可以稍后重试或换一种语言。")
    target_segs = _align(source_segs, target_segs) if method in ("llm",) else target_segs

    return {
        "videoId": video_id,
        "meta": meta,
        "tracks": [{k: t[k] for k in ("code", "label", "generated", "translatable")} for t in tracks],
        "translatableLanguages": translation_languages(video_id),
        "source": {"lang": source_lang, "segments": source_segs},
        "target": {"lang": tgt, "method": method, "segments": target_segs},
        "notices": notices,
    }
