// Cloudflare Pages Function: GET /api/load?url=…&tgt=…&src=…
//
// A JS port of ytcap.py's caption + translation chain. Everything here is stateless:
// captions and translations are fetched from YouTube / Google and cached in the
// Workers Cache API, so repeat views are cheap.
//
// The chain (same order as the local Python version):
//   source track  →  native target track  →  YouTube MT  →  keyless MT (gtx)

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

const INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";
const INNERTUBE_CTX = { client: { clientName: "ANDROID", clientVersion: "20.10.38" } };
const PLAYER_URL = `https://www.youtube.com/youtubei/v1/player?key=${INNERTUBE_KEY}`;

const GTX_LANG = { "zh-Hans": "zh-CN", "zh-Hant": "zh-TW", "es-419": "es" };

const H = { "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9" };

// ---------------------------------------------------------------- helpers

export function parseVideoId(url) {
  const s = (url || "").trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s;
  let m = s.match(/[?&]v=([A-Za-z0-9_-]{11})/);
  if (m) return m[1];
  m = s.match(/youtu\.be\/([A-Za-z0-9_-]{11})/);
  if (m) return m[1];
  m = s.match(/\/(?:shorts|embed|live|v)\/([A-Za-z0-9_-]{11})/);
  if (m) return m[1];
  return null;
}

function extractJson(text, marker) {
  const i = text.indexOf(marker);
  if (i < 0) return null;
  const j = text.indexOf("{", i);
  if (j < 0) return null;
  let depth = 0, inStr = false, esc = false;
  for (let k = j; k < text.length; k++) {
    const c = text[k];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(text.slice(j, k + 1)); } catch { return null; }
      }
    }
  }
  return null;
}

function parseJson3(doc) {
  const out = [];
  for (const ev of doc.events || []) {
    if (ev.tStartMs === undefined) continue;
    const text = (ev.segs || []).map((s) => s.utf8 || "").join("").replace(/\n/g, " ").trim();
    if (!text) continue;
    out.push({ start: ev.tStartMs / 1000, duration: (ev.dDurationMs || 0) / 1000, text });
  }
  return out;
}

async function fetchSegments(baseUrl) {
  const clean = baseUrl.replace("&fmt=srv3", "");
  const res = await fetch(clean + "&fmt=json3", { headers: H });
  if (!res.ok) return [];
  try { return parseJson3(await res.json()); } catch { return []; }
}

// ---------------------------------------------------------------- player

async function playerResponse(videoId) {
  const res = await fetch(PLAYER_URL, {
    method: "POST",
    headers: { ...H, "Content-Type": "application/json" },
    body: JSON.stringify({ context: INNERTUBE_CTX, videoId }),
  });
  if (!res.ok) throw new Error("innertube " + res.status);
  return res.json();
}

function trackList(player) {
  const r = player?.captions?.playerCaptionsTracklistRenderer;
  if (!r) return { tracks: [], languages: [] };
  const tracks = (r.captionTracks || []).map((t) => ({
    code: t.languageCode,
    label: t.name?.runs?.[0]?.text || t.languageCode,
    generated: t.kind === "asr",
    translatable: !!t.isTranslatable,
    baseUrl: (t.baseUrl || "").replace("&fmt=srv3", ""),
  }));
  const languages = (r.translationLanguages || []).map((l) => ({
    code: l.languageCode,
    label: l.languageName?.runs?.[0]?.text || l.languageCode,
  }));
  return { tracks, languages };
}

// ---------------------------------------------------------------- translation

async function gtxBatch(lines, tl) {
  const q = lines.join("\n");
  const url =
    "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=" +
    encodeURIComponent(tl) + "&dt=t&q=" + encodeURIComponent(q);
  const res = await fetch(url, { headers: H });
  if (!res.ok) return null;
  const data = await res.json();
  const joined = (data[0] || []).map((p) => p[0] || "").join("");
  return joined.split("\n").map((s) => s.trim());
}

async function gtxTranslate(segments, target) {
  if (!segments.length) return [];
  const tl = GTX_LANG[target] || target;
  const BATCH = 40;
  const batches = [];
  for (let i = 0; i < segments.length; i += BATCH) batches.push(segments.slice(i, i + BATCH));
  const out = [];
  let cursor = 0;
  const workers = new Array(Math.min(4, batches.length)).fill(0).map(async () => {
    while (cursor < batches.length) {
      const chunk = batches[cursor++];
      const lines = chunk.map((s) => (s.text || "").replace(/\n/g, " ").trim());
      let parts = null;
      try { parts = await gtxBatch(lines, tl); } catch { parts = null; }
      if (!parts) {
        parts = [];
        for (const line of lines) {
          try { parts.push((await gtxBatch([line], tl))?.[0] ?? ""); } catch { parts.push(""); }
        }
      }
      while (parts.length < chunk.length) parts.push("");
      for (let k = 0; k < chunk.length; k++) {
        out.push({ start: chunk[k].start, duration: chunk[k].duration, text: parts[k] });
      }
    }
  });
  await Promise.all(workers);
  out.sort((a, b) => a.start - b.start);
  return out;
}

// ---------------------------------------------------------------- main

export async function build(url, src, tgt) {
  const videoId = parseVideoId(url);
  if (!videoId) throw new Error("无法识别这个链接");

  const player = await playerResponse(videoId);
  const details = player.videoDetails || {};
  const meta = {
    title: details.title || "",
    author: details.author || "",
    duration: Number(details.lengthSeconds || 0),
    thumbnail: `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`,
  };

  const { tracks, languages } = trackList(player);
  const notices = [];

  // pick a source track
  let picked = null;
  if (src && src !== "auto") picked = tracks.find((t) => t.code === src);
  if (!picked) {
    const order = [
      ...tracks.filter((t) => t.code === "en" && !t.generated),
      ...tracks.filter((t) => t.code === "en" && t.generated),
      ...tracks.filter((t) => !t.generated),
      ...tracks.filter((t) => t.generated),
    ];
    picked = order[0] || null;
  }
  let source = [];
  if (picked) source = await fetchSegments(picked.baseUrl);
  if (!source.length) {
    notices.push("没有取到字幕轨道（该视频可能没有字幕，或 YouTube 拒绝了这次请求）。");
  }

  // translation
  let target = [];
  let method = "none";
  const native = tracks.find((t) => t.code === tgt && t.code !== (picked && picked.code));
  if (source.length && tgt && tgt !== "none") {
    if (native) {
      target = await fetchSegments(native.baseUrl);
      method = target.length ? "native" : "none";
    }
    if (!target.length) {
      const mt = await gtxTranslate(source, tgt);
      if (mt.length) { target = mt; method = "mt"; }
    }
    if (!target.length && picked) {
      const alt = await fetchSegments(picked.baseUrl + "&tlang=" + encodeURIComponent(tgt));
      if (alt.length) { target = alt; method = "yt-mt"; }
    }
    if (!target.length) notices.push(`没能得到 ${tgt} 的译文，稍后重试或换一种语言。`);
  }

  return {
    videoId,
    meta,
    tracks: tracks.map(({ code, label, generated, translatable }) => ({ code, label, generated, translatable })),
    translatableLanguages: languages,
    source: { lang: picked ? picked.code : "", segments: source },
    target: { lang: tgt, method, segments: target },
    notices,
  };
}

export async function onRequestGet({ request }) {
  const u = new URL(request.url);
  const url = u.searchParams.get("url") || "";
  const src = u.searchParams.get("src") || "auto";
  const tgt = u.searchParams.get("tgt") || "zh-Hans";

  const cache = caches.default;
  const key = new Request(u.toString(), { method: "GET" });
  const hit = await cache.match(key);
  if (hit) return hit;

  try {
    const data = await build(url, src, tgt);
    const res = new Response(JSON.stringify(data), {
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, max-age=600",
        "Access-Control-Allow-Origin": "*",
      },
    });
    try { cache.put(key, res.clone()); } catch { /* ignore */ }
    return res;
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e && e.message ? e.message : e) }), {
      status: 500,
      headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
    });
  }
}
