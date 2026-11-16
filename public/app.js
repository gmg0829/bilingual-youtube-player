/* Bilingual YouTube player — front end.

   Playback goes through our own proxy into a native <video> element, so it works in any
   browser (including in-app webviews) and gives us exact playback time for the subtitles.
   The YouTube iframe stays available as a fallback.

   Subtitles are two independent timed streams (original track + translated track). Each
   line is looked up by playback time, so tracks with different segmentation still line up. */

const $ = (s) => document.querySelector(s);
const el = {
  form: $("#form"), url: $("#url"), load: $("#load"), hint: $("#hint"),
  srcLang: $("#srcLang"), tgtLang: $("#tgtLang"), prefer: $("#prefer"),
  stageWrap: $("#stageWrap"), stage: $("#stage"),
  vid: $("#vid"), cover: $("#cover"), bigplay: $("#bigplay"), tapnote: $("#tapnote"),
  subs: $("#subs"), subOrig: $("#subOrig"), subTrans: $("#subTrans"), bufnote: $("#bufnote"),
  safari: $("#safari"),
  pp: $("#pp"), ppIcon: $("#ppIcon"), seek: $("#seek"), tm: $("#tm"),
  rate: $("#rate"), mute: $("#mute"), vol: $("#vol"), ccmode: $("#ccmode"), fs: $("#fs"),
  vtitle: $("#vtitle"), vby: $("#vby"), notes: $("#notes"), streamNote: $("#streamNote"),
  intro: $("#intro"), meta: $("#meta"),
  exportBtn: $("#exportBtn"), exportNote: $("#exportNote"), steps: $("#steps"),
  trlist: $("#trlist"), trInfo: $("#trInfo"), trToggle: $("#trToggle"),
  useYT: $("#useYT"),
};

const TARGETS = [
  ["zh-Hans", "简体中文"], ["zh-Hant", "繁體中文"], ["en", "English"], ["ja", "日本語"],
  ["ko", "한국어"], ["fr", "Français"], ["es", "Español"], ["es-419", "Español (LatAm)"],
  ["de", "Deutsch"], ["pt", "Português"], ["ru", "Русский"], ["it", "Italiano"],
  ["nl", "Nederlands"], ["tr", "Türkçe"], ["pl", "Polski"], ["ar", "العربية"],
  ["hi", "हिन्दी"], ["id", "Bahasa Indonesia"], ["th", "ไทย"], ["vi", "Tiếng Việt"],
];

const state = {
  kind: null,            // "native" | "yt"
  vid: null,             // HTMLVideoElement (native mode)
  yt: null, ytReady: false, ytQueued: [],
  userPaused: false, startedAt: 0, direct: false, muted: false, drag: false,
  srcSegs: [], tgtSegs: [], srcLang: "", tgtLang: "", method: "",
  ccMode: "bi", lastNow: -1, streamTimer: null, kickTries: 0,
};

const ICON = {
  play: '<path d="M8 5v14l11-7z"/>',
  pause: '<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>',
  vol: '<path d="M3 9v6h4l5 5V4L7 9H3z"/>',
  mute: '<path d="M3 9v6h4l5 5V4L7 9H3zm11 .4L16.6 7l1.4 1.4L19.2 10l1.2-1.6L21.8 10l-1.2 1.6 1.2 1.6-1.4 1.4-1.2-1.6-1.2 1.6-1.4-1.4z"/>',
};

/* ------------------------------------------------------------------ helpers */
const dur = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
};

function findIndex(segs, t) {
  let lo = 0, hi = segs.length - 1, i = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (segs[m].start <= t) { i = m; lo = m + 1; } else hi = m - 1;
  }
  return i;
}

function videoIdOf(url) {
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

/* ------------------------------------------------------------------ player adapter */
const player = {
  ready: false,
  play() {
    if (state.kind === "native" && el.vid) {
      const v = el.vid;
      const p = v.play();
      if (!p || !p.catch) return;
      p.then(() => {
        // the promise can resolve while the decoder is still stalled
        setTimeout(() => {
          if (v.paused) {
            state.triedPlay = (state.triedPlay || 0) + 1;
            showTapHint();
          } else if (v.muted) showSoundHint();
        }, 700);
      }).catch(() => {
        // no sound autoplay is allowed on iOS — retry muted
        v.muted = true; state.muted = true; paintMute();
        const q = v.play();
        if (!q || !q.catch) return;
        q.then(() => setTimeout(() => { if (v.paused) showTapHint(); else showSoundHint(); }, 700))
          .catch(() => showTapHint());
      });
    } else if (state.ytReady) {
      state.yt.playVideo();
    }
  },
  pause() {
    if (state.kind === "native" && el.vid) el.vid.pause();
    else if (state.ytReady) state.yt.pauseVideo();
  },
  seek(t) {
    if (state.kind === "native" && el.vid) el.vid.currentTime = t;
    else if (state.ytReady) state.yt.seekTo(t, true);
  },
  time() {
    if (state.kind === "native" && el.vid) return el.vid.currentTime || 0;
    return state.ytReady ? (state.yt.getCurrentTime() || 0) : 0;
  },
  dur() {
    if (state.kind === "native" && el.vid) return el.vid.duration || 0;
    return state.ytReady ? (state.yt.getDuration() || 0) : 0;
  },
  playing() {
    if (state.kind === "native" && el.vid) return !el.vid.paused && !el.vid.ended && el.vid.readyState > 2;
    return state.ytReady && state.yt.getPlayerState() === 1;
  },
  rate(r) {
    if (state.kind === "native" && el.vid) el.vid.playbackRate = r;
    else if (state.ytReady) state.yt.setPlaybackRate(r);
  },
  mute(m) {
    if (state.kind === "native" && el.vid) el.vid.muted = m;
    else if (state.ytReady) m ? state.yt.mute() : state.yt.unMute();
    state.muted = m; paintMute();
  },
  volume(v) {
    if (state.kind === "native" && el.vid) el.vid.volume = Math.max(0, Math.min(1, v));
    else if (state.ytReady) state.yt.setVolume(Math.max(0, Math.min(100, v * 100)));
  },
};

function showTapHint() {
  // second refusal: the webview itself is blocking media, not the autoplay policy
  if ((state.triedPlay || 0) >= 2 && el.safari) {
    el.safari.href = location.href;
    el.safari.hidden = false;
  }
  if (state.direct) return;
  state.direct = true;
  el.stageWrap.classList.add("direct");
  el.cover.classList.remove("off");
  el.tapnote.textContent = "浏览器拦截了自动播放，点一下画面即可开始";
  el.tapnote.hidden = false;
}

function showSoundHint() {
  if (!state.muted) return;
  el.tapnote.textContent = "静音自动播放中，点右上角喇叭图标开声音";
  el.tapnote.hidden = false;
  setTimeout(() => { if (!state.direct) el.tapnote.hidden = true; }, 4500);
}

/* ------------------------------------------------------------------ subtitles */
function currentLine(segs, t) {
  if (!segs.length) return "";
  const i = findIndex(segs, t);
  if (i < 0) return "";
  const s = segs[i];
  const next = segs[i + 1];
  if (next && t > next.start + 0.25) return "";
  return s.text || "";
}

/* The two tracks are cut very differently (the Chinese TED track uses coarse 4-second
   blocks, the English one is fine-grained), so each track is looked up on its own
   timeline. Pairing by index or by "best overlap" invents matches that are not there. */
function drawSubs(t) {
  const mode = state.ccMode;
  if (mode === "off" || (!state.srcSegs.length && !state.tgtSegs.length)) {
    el.subs.hidden = true;
    return;
  }
  const orig = mode === "trans" ? "" : currentLine(state.srcSegs, t);
  const trans = mode === "orig" ? "" : currentLine(state.tgtSegs, t);
  const changed = el.subOrig.textContent !== orig || el.subTrans.textContent !== trans;
  el.subOrig.textContent = orig;
  el.subTrans.textContent = trans;
  el.subs.hidden = !(orig || trans);
  if (changed && (orig || trans)) {
    el.subs.classList.remove("pulse");
    void el.subs.offsetWidth;          // restart the fade
    el.subs.classList.add("pulse");
  }
}

/* Transcript rows: each translated line is shown once, on the source row it starts in. */
function buildRowMap() {
  const map = {};
  const segs = state.srcSegs;
  if (!segs.length) return map;
  for (const g of state.tgtSegs) {
    const i = findIndex(segs, g.start + 0.01);
    if (i >= 0) (map[i] = map[i] || []).push(g.text);
  }
  return map;
}

function syncTranscript(t) {
  if (!state.srcSegs.length) return;
  const idx = findIndex(state.srcSegs, t);
  if (idx === state.lastNow) return;
  state.lastNow = idx;
  document.querySelectorAll(".seg.now").forEach((n) => n.classList.remove("now"));
  const node = document.querySelector('.seg[data-i="' + idx + '"]');
  if (node) {
    node.classList.add("now");
    if (player.playing()) {
      const box = el.trlist;
      box.scrollTo({ top: node.offsetTop - box.offsetTop - 60, behavior: "smooth" });
    }
  }
}

/* ------------------------------------------------------------------ tick */
function tick() {
  if (!player.ready) return;
  const t = player.time(), d = player.dur();
  el.tm.textContent = dur(t) + " / " + (d ? dur(d) : "–:––");
  if (!state.drag && d) el.seek.value = Math.round(1000 * t / d);
  drawSubs(t);
  syncTranscript(t);
}
setInterval(tick, 120);

/* ------------------------------------------------------------------ transcript */
function renderTranscript() {
  const segs = state.srcSegs;
  if (!segs.length) {
    el.trlist.innerHTML = '<p class="empty">这个视频没有字幕文本。</p>';
    el.trInfo.textContent = "—";
    return;
  }
  const rowMap = buildRowMap();
  el.trlist.innerHTML = segs.map((s, i) => {
    const x = (rowMap[i] || []).join(" ").trim();
    return `<div class="seg" data-i="${i}">
      <button class="ts" data-seek="${s.start}">${dur(s.start)}</button>
      <div class="seg-txt">
        <div class="seg-o">${escapeHtml(s.text)}</div>
        ${x ? `<div class="seg-x">${escapeHtml(x)}</div>` : ""}
      </div></div>`;
  }).join("");
  const bits = [`${segs.length} 句`];
  if (state.srcLang) bits.push(state.srcLang);
  if (state.tgtLang && state.method) bits.push("→ " + state.tgtLang);
  el.trInfo.textContent = bits.join(" ");
  const how = {
    "yt-mt": "YouTube 机翻", "yt-mt+t2s": "YouTube 机翻＋转简体",
    native: "视频自带", "native+t2s": "视频自带＋转简体", "native+s2t": "视频自带＋转繁体",
    llm: "AI 翻译", mt: "无密钥机翻", same: "同语",
  }[state.method] || state.method;
  el.trInfo.title = `原文 ${state.srcLang} · 译文 ${state.tgtLang}（${how}）`;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------------------------------------------ load */
function setHint(msg, cls) {
  el.hint.textContent = msg;
  el.hint.className = "hint" + (cls ? " " + cls : "");
}

async function loadVideo(rawUrl) {
  const url = rawUrl.trim();
  if (!url) return;
  el.load.disabled = true;
  setHint("正在读取视频与字幕…");

  const vid = videoIdOf(url);
  const streamPromise = vid ? resolveStream(vid) : Promise.resolve(false);

  try {
    const q = new URLSearchParams({
      url,
      src: el.srcLang.value,
      tgt: el.tgtLang.value,
      prefer: el.prefer ? el.prefer.value : "auto",
    });
    const res = await fetch("/api/load?" + q.toString());
    const data = await res.json();
    if (data.error) { setHint(data.error, "err"); return; }

    state.srcSegs = data.source.segments || [];
    state.tgtSegs = data.target.segments || [];
    state.srcLang = data.source.lang;
    state.tgtLang = data.target.lang;
    state.method = data.target.method;

    const keep = el.srcLang.value;
    el.srcLang.innerHTML = '<option value="auto">自动</option>';
    (data.tracks || []).forEach((t) => {
      const o = document.createElement("option");
      o.value = t.code;
      o.textContent = t.label + (t.generated ? " (自动)" : "");
      el.srcLang.appendChild(o);
    });
    el.srcLang.value = keep === "auto" ? "auto" : (keep || "auto");

    el.vtitle.textContent = data.meta.title || "(无标题)";
    el.vby.textContent = [data.meta.author, dur(data.meta.duration)].filter(Boolean).join(" · ");
    const notes = (data.notices || []).slice();
    if (!state.srcSegs.length) notes.unshift("这个视频没有可用的字幕轨道。");
    else if (!state.tgtSegs.length && el.tgtLang.value !== "off") notes.unshift("没有译文，只显示原文。");
    el.notes.textContent = notes.join("\n");

    el.stageWrap.hidden = false;
    el.intro.hidden = true;
    el.meta.hidden = false;
    el.cover.style.backgroundImage = `url("${data.meta.thumbnail}")`;
    if (data.meta.thumbnail) el.vid.poster = data.meta.thumbnail;
    el.cover.classList.remove("off");
    state.lastNow = -1;
    renderTranscript();
    if (state.kind === 'native') setTimeout(() => { try { el.vid.load(); } catch (e) {} kickLoop(); }, 120);

    setHint("", "ok");
    await streamPromise;
  } catch (e) {
    setHint("出错了：" + String(e.message || e).split("\n")[0].slice(0, 160), "err");
  } finally {
    el.load.disabled = false;
  }
}

/* resolve the media (download once, then it is served from disk), then mount the player */
async function resolveStream(videoId) {
  // Cloudflare 没有本地视频代理：直接走 YouTube 官方播放器（真实浏览器里能播）
  if (new URLSearchParams(location.search).get("cloud") === "1" || !location.hostname.match(/^(127\.|localhost|10\.|192\.168\.)/)) {
    mountYT(videoId);
    return true;
  }
  for (let i = 0; i < 200; i++) {
    let st;
    try {
      const r = await fetch("/api/stream-info?video=" + encodeURIComponent(videoId));
      if (r.status === 404) { mountYT(videoId); return true; }
      st = await r.json();
    } catch (e) {
      st = { state: "error", error: String(e.message) };
    }
    if (st.state === "ready") {
      el.streamNote.textContent = "";
      mountNative(videoId, st.url);
      return true;
    }
    if (st.state === "error") {
      el.streamNote.textContent = "视频流解析失败：" + (st.error || "") + "。可点右下角 YT 改用官方播放器。";
      return false;
    }
    const pct = Math.round(st.progress || 0);
    el.streamNote.textContent = `正在缓存视频 ${pct}%（只下一次，之后播放与拖动都很快）`;
    await new Promise((r) => setTimeout(r, 1500));
  }
  el.streamNote.textContent = "视频流解析超时，可点右下角 YT 改用官方播放器。";
  return false;
}

/* WKWebView silently drops the load when the video element sits in a display:none
   container (the player is mounted before its section is unhidden), and a background
   tab suspends media too. Retry while the element is actually visible. */
function kickLoop() {
  if (state.kickTimer) clearInterval(state.kickTimer);
  const until = Date.now() + 40000;
  state.kickTimer = setInterval(() => {
    if (state.kind !== "native" || !el.vid || !el.vid.getAttribute("src")) return;
    if (el.vid.readyState >= 1 || Date.now() > until) {
      clearInterval(state.kickTimer); state.kickTimer = null; return;
    }
    if (el.vid.clientWidth > 0) { try { el.vid.load(); } catch (e) {} }
  }, 1000);
}

function mountNative(videoId, url) {
  state.kind = "native";
  state.yt = null; state.ytReady = false;
  el.stageWrap.hidden = false;   // a hidden container silently swallows the media load
  if (el.intro) el.intro.hidden = true;
  el.vid.hidden = false;
  el.vid.src = url;
  el.vid.muted = false;
  el.vid.playsInline = true;
  el.vid.load();
  kickLoop();
  el.vid.addEventListener("play", () => {
    el.cover.classList.add("off");
    el.stageWrap.classList.remove("paused");
    el.ppIcon.innerHTML = ICON.pause;
    el.tapnote.hidden = true;
    if (el.safari) el.safari.hidden = true;
    state.direct = false;
    el.stageWrap.classList.remove("direct");
  });
  el.vid.addEventListener("waiting", () => { el.bufnote.hidden = false; });
  el.vid.addEventListener("playing", () => { el.bufnote.hidden = true; });
  el.vid.addEventListener("pause", () => {
    el.stageWrap.classList.add("paused");
    el.ppIcon.innerHTML = ICON.play;
  });
  el.vid.addEventListener("volumechange", () => {
    state.muted = el.vid.muted;
    paintMute();
  });
  const onMeta = () => {
    player.ready = true;
    el.stageWrap.classList.remove("paused");
    el.ppIcon.innerHTML = ICON.pause;
    state.userPaused = false;
    state.startedAt = Date.now();
    player.play();
  };
  el.vid.addEventListener("loadedmetadata", onMeta, { once: true });
  el.vid.addEventListener("canplay", () => {
    if (!player.ready) { player.ready = true; state.startedAt = Date.now(); player.play(); }
  }, { once: true });
  el.vid.addEventListener("error", () => {
    el.streamNote.textContent = "视频流播放失败，可以改用 YouTube 播放器。";
  }, { once: true });
}

/* The page can sit in a background tab (WKWebView then suspends media). When it comes
   back, resume loading and playing. */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (state.kind === "native" && el.vid && el.vid.getAttribute("src")) {
    if (el.vid.readyState < 1) { try { el.vid.load(); } catch (e) {} kickLoop(); }
    if (!state.userPaused) player.play();
  }
});

/* YouTube iframe — fallback path */
/* The YouTube iframe API is heavy — pull it in only when the fallback is used. */
function loadYTApi() {
  return new Promise((resolve, reject) => {
    if (window.YT && window.YT.Player) return resolve(window.YT);
    const s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    s.onerror = () => reject(new Error("无法加载 YouTube 播放器脚本"));
    s.onload = () => {
      let tries = 0;
      const iv = setInterval(() => {
        if (window.YT && window.YT.Player) { clearInterval(iv); resolve(window.YT); }
        else if (++tries > 100) { clearInterval(iv); reject(new Error("YouTube 播放器脚本没有就绪")); }
      }, 100);
    };
    document.head.appendChild(s);
  });
}

async function mountYT(videoId) {
  try { await loadYTApi(); } catch (e) { setHint(String(e.message || e).slice(0, 160), "err"); return; }
  state.kind = "yt";
  el.vid.hidden = true;
  el.vid.removeAttribute("src");
  const host = document.createElement("div");
  host.className = "yt-host";
  host.style.cssText = "position:absolute;inset:0";
  el.stage.insertBefore(host, el.stage.firstChild);
  state.yt = new YT.Player(host, {
    videoId,
    playerVars: {
      controls: 0, disablekb: 1, rel: 0, fs: 0,
      playsinline: 1, cc_load_policy: 0, enablejsapi: 1, autoplay: 1, origin: location.origin,
    },
    events: {
      onReady: () => {
        state.ytReady = true; player.ready = true;
        try { state.yt.unloadModule("captions"); state.yt.unloadModule("cc"); } catch (_) {}
        state.yt.playVideo();
      },
      onStateChange: (e) => {
        const playing = e.data === 1;
        el.stageWrap.classList.toggle("paused", !playing);
        el.ppIcon.innerHTML = playing ? ICON.pause : ICON.play;
        if (playing) {
          state.direct = false;
          el.stageWrap.classList.remove("direct");
          el.tapnote.hidden = true;
          el.cover.classList.add("off");
        } else if (e.data === 2 && !state.userPaused && state.startedAt && Date.now() - state.startedAt < 15000) {
          showTapHint();
        }
      },
      onError: (e) => {
        const map = { 2: "视频 ID 无效", 5: "HTML5 播放器无法播放此视频", 100: "视频不存在或已被删除",
          101: "视频所有者禁止了嵌入播放", 150: "视频所有者禁止了嵌入播放" };
        setHint((map[e.data] || "视频无法播放（错误 " + e.data + "）"), "err");
      },
    },
  });
}

/* ------------------------------------------------------------------ wiring */
el.form.addEventListener("submit", (e) => { e.preventDefault(); loadVideo(el.url.value); });
el.pp.addEventListener("click", togglePlay);
function togglePlay() {
  if (state.kind === "native") {
    const v = el.vid;
    if (v.paused || v.ended) {
      state.userPaused = false;
      if (v.readyState === 0) {
        // no data yet — WKWebView needs an explicit nudge, then play
        el.bufnote.hidden = false;
        v.addEventListener("canplay", () => { el.bufnote.hidden = true; }, { once: true });
        try { v.load(); } catch (e) {}
      }
      player.play();
    } else {
      state.userPaused = true;
      player.pause();
    }
    return;
  }
  player.playing() ? player.pause() : player.play();
}

el.bigplay.addEventListener("click", (e) => {
  e.stopPropagation();
  el.cover.classList.add("off");
  state.direct = false;
  el.stageWrap.classList.remove("direct");
  el.tapnote.hidden = true;
  if (state.muted) player.mute(false);
  state.userPaused = false;
  player.play();
});
/* Tapping anywhere on the picture plays / pauses — like any video player. */
el.stage.addEventListener("click", (e) => {
  if (e.target.closest("#bigplay")) return;
  if (state.direct) {
    state.direct = false;
    el.stageWrap.classList.remove("direct");
    el.tapnote.hidden = true;
  }
  if (state.kind === "native") togglePlay();
});
el.seek.addEventListener("input", () => { state.drag = true; });
el.seek.addEventListener("change", () => {
  const d = player.dur();
  if (d) player.seek((el.seek.value / 1000) * d);
  state.drag = false;
});
el.rate.addEventListener("change", () => player.rate(parseFloat(el.rate.value)));
el.mute.addEventListener("click", () => player.mute(!state.muted));
el.vol.addEventListener("input", () => {
  player.volume(parseFloat(el.vol.value));
  if (parseFloat(el.vol.value) > 0 && state.muted) player.mute(false);
});
el.ccmode.addEventListener("change", () => {
  state.ccMode = el.ccmode.value;
  drawSubs(player.time());
});
el.fs.addEventListener("click", () => {
  const p = el.player;
  if (document.fullscreenElement) document.exitFullscreen();
  else if (p.requestFullscreen) p.requestFullscreen();
  else if (p.webkitRequestFullscreen) p.webkitRequestFullscreen();
});
el.trToggle.addEventListener("click", () => {
  const collapsed = el.trlist.classList.toggle("collapsed");
  el.trToggle.textContent = collapsed ? "展开" : "收起";
});
el.trlist.addEventListener("click", (e) => {
  const b = e.target.closest("[data-seek]");
  if (b) { player.seek(parseFloat(b.getAttribute("data-seek"))); return; }
  const row = e.target.closest(".seg");
  if (row) {
    const i = parseInt(row.getAttribute("data-i"), 10);
    const s = state.srcSegs[i];
    if (s) player.seek(s.start);
  }
});
function paintSteps(state) {
  const st = state.stages || {};
  const order = state.order || ["captions", "translate", "download", "mux", "play"];
  el.steps.hidden = false;
  el.steps.querySelectorAll("li").forEach((li) => {
    const key = li.dataset.stage;
    const d = st[key];
    const i = li.querySelector(".bar i");
    const stl = li.querySelector(".st");
    li.classList.remove("active", "done", "fail");
    if (!d) { i.style.width = "0%"; stl.textContent = ""; return; }
    i.style.width = (d.pct || 0) + "%";
    if (d.done) {
      li.classList.add("done");
      stl.textContent = "完成";
    } else {
      li.classList.add("active");
      stl.textContent = (d.pct || 0) + "%" + (d.detail ? " · " + d.detail : "");
    }
  });
  if (state.state === "error") {
    const order2 = order;
    const active = el.steps.querySelector("li.active");
    if (active) { active.classList.remove("active"); active.classList.add("fail"); }
    else if (order2.length) {
      const last = el.steps.querySelector('li[data-stage="' + order2[order2.length - 1] + '"]');
      if (last) last.classList.add("fail");
    }
  }
}

async function runPrepare() {
  const vid = videoIdOf(el.url.value);
  if (!vid) { el.exportNote.textContent = "先载入一个视频"; return; }
  el.exportBtn.disabled = true;
  el.exportNote.className = "action-note";
  el.exportNote.textContent = "开始准备…";
  el.steps.hidden = false;
  const tgt = el.tgtLang.value || "zh-Hans";
  try {
    const r = await fetch(`/api/prepare?video=${encodeURIComponent(vid)}&tgt=${encodeURIComponent(tgt)}`);
    let st = await r.json();
    if (st.error && st.state !== "running") throw new Error(st.error);
    paintSteps(st);
    const timer = setInterval(async () => {
      let s2;
      try {
        s2 = await (await fetch(`/api/prepare-status?video=${encodeURIComponent(vid)}`)).json();
      } catch (e) { return; }
      paintSteps(s2);
      if (s2.state === "done") {
        clearInterval(timer);
        el.exportBtn.disabled = false;
        el.exportNote.className = "action-note ok";
        el.exportNote.textContent = s2.played
          ? "已在系统播放器打开，字幕在播放器里可开关"
          : "已生成：" + (s2.name || "");
      } else if (s2.state === "error") {
        clearInterval(timer);
        el.exportBtn.disabled = false;
        el.exportNote.className = "action-note err";
        el.exportNote.textContent = "失败：" + (s2.error || "未知原因");
      }
    }, 600);
  } catch (e) {
    el.exportBtn.disabled = false;
    el.exportNote.className = "action-note err";
    el.exportNote.textContent = "失败：" + String(e.message || e).slice(0, 90);
  }
}

const isLocal = location.hostname.match(/^(127\.|localhost|10\.|192\.168\.)/);
if (!isLocal) {
  if (el.exportBtn) el.exportBtn.hidden = true;
  if (el.exportNote) el.exportNote.textContent = "云端版只提供网页播放；「带字幕 mp4」在本机生成";
}

el.exportBtn.addEventListener("click", runPrepare);

el.useYT.addEventListener("click", () => {
  const vid = videoIdOf(el.url.value);
  if (vid) mountYT(vid);
});
el.srcLang.addEventListener("change", () => { if (el.url.value.trim()) loadVideo(el.url.value); });
el.tgtLang.addEventListener("change", () => { if (el.url.value.trim()) loadVideo(el.url.value); });
if (el.prefer) el.prefer.addEventListener("change", () => { if (el.url.value.trim()) loadVideo(el.url.value); });

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  const k = e.key.toLowerCase();
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") player.seek(Math.max(0, player.time() - 5));
  else if (e.key === "ArrowRight") player.seek(player.time() + 5);
  else if (e.key === "ArrowUp") { e.preventDefault(); el.vol.value = Math.min(1, parseFloat(el.vol.value) + 0.1); el.vol.dispatchEvent(new Event("input")); }
  else if (e.key === "ArrowDown") { e.preventDefault(); el.vol.value = Math.max(0, parseFloat(el.vol.value) - 0.1); el.vol.dispatchEvent(new Event("input")); }
  else if (k === "c") { el.ccmode.value = el.ccmode.value === "off" ? "bi" : "off"; el.ccmode.dispatchEvent(new Event("change")); }
  else if (k === "f") el.fs.click();
});

function paintMute() {
  el.mute.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">${state.muted ? ICON.mute : ICON.vol}</svg>`;
}

/* ------------------------------------------------------------------ init */
TARGETS.forEach(([code, label]) => {
  const o = document.createElement("option");
  o.value = code; o.textContent = label;
  el.tgtLang.appendChild(o);
});
el.tgtLang.value = "zh-Hans";

const qUrl = new URLSearchParams(location.search).get("url");
if (qUrl) { el.url.value = qUrl; loadVideo(qUrl); }
