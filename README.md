# 双语 YouTube 播放器

粘贴一个 YouTube 链接 → 视频自动播放 → 画面下方同时显示**原文**和**译文**两行字幕。
字幕是逐句对齐的，下面还有一份可点击跳转的双语字幕稿。

---

## 运行

```bash
cd /var/minis/workspace/yt-bilingual
python3 server.py            # 默认 http://127.0.0.1:8765
python3 server.py --port 9000
```

在 Minis 内置浏览器里打开 `http://127.0.0.1:8765/`（或直接点 [打开播放器](http://127.0.0.1:8765/)）。
也可以带参数直接进：`http://127.0.0.1:8765/?url=https://www.youtube.com/watch?v=xxxx`。

`run.sh` 会启动服务并把地址打印出来。

## 界面上的东西

| 控件 | 作用 |
|---|---|
| 源语言 | 选原文轨道（自动＝优先英文人工字幕，其次英文自动字幕） |
| 译入 | 译文语言，默认简体中文 |
| 译文来源 | 自动 / 只用 YouTube 机翻 / 只用 AI 翻译 |
| 双语 / 仅原文 / 仅译文 / 关闭 | 字幕显示模式 |
| 1× | 播放速度 |
| 🔊 + 滑块 | 音量 |
| **YT** | 改用 YouTube 官方播放器（我们的直链失败时的备用方案） |
| 字幕文本 | 整篇双语字幕稿，点任意一句跳到那一刻，播放时自动高亮 |

快捷键：`空格` 播放/暂停 · `←/→` ±5 秒 · `↑/↓` 音量 · `C` 字幕开关 · `F` 全屏。

## 字幕是怎么来的

1. **原文**：YouTube 的字幕轨道（人工字幕或自动字幕），通过 innertube `get_transcript` 接口取，
   带回完整时间码。
2. **译文**，按优先级：
   - 视频自带的同语言轨道（人工翻译，质量最好）。中文按 `zh-Hans → zh-CN`、`zh-Hant → zh-TW` 匹配，
     繁体轨道会用 OpenCC 转成简体（反之亦然）。
   - **AI 翻译**（如果配置了 `YTB_LLM_*`，默认优先）。
   - **YouTube 自带机翻**（`&tlang=`）：拿不到简体时先取繁体再转简体。
3. 两条字幕是**两条独立的时间轴**：每一行按播放时间各自查表，所以原文和译文的断句方式不一样也能对上。
4. 全部结果落盘缓存（`cache/`），同一个视频再看一次不会重新请求 YouTube。

### 可选：AI 翻译

```bash
export YTB_LLM_BASE=https://api.example.com/v1     # 任何 OpenAI 兼容的 /chat/completions
export YTB_LLM_KEY=sk-xxxx
export YTB_LLM_MODEL=your-model
python3 server.py
```

翻译按 30 句一批发给模型，要求只回 JSON 数组，失败就自动退回 YouTube 机翻。

## 视频流

默认走**本地缓存**：服务端用 `yt-dlp`（`youtube:player_client=android`，这个客户端给出的直链才不会被
CDN 拒绝）把视频下一个单文件 MP4 到 `cache/media/`，再用 `GET /api/local/<id>` 带 Range 发给浏览器的
`<video>` 元素。这样播放和拖动进度条都是本地磁盘速度，不受 YouTube CDN 抖动影响。

* 第一次播放会看到「正在缓存视频 xx%」；下完之后同一个视频再打开是秒开。
* 下不下来时（CDN 限流、或者视频只有分离的音视频轨）会提示，点 **YT** 换成 YouTube 官方播放器。
* `GET /api/stream/<id>` 保留了一条直连通道（实时转发上游），作为兜底。

## 目录

```
yt-bilingual/
├── server.py          HTTP 服务：页面 + /api/load + /api/stream-info + /api/stream（Range 代理）
├── ytcap.py           字幕抓取、翻译链（原生轨道 → AI → YouTube 机翻）、视频流解析
├── static/
│   ├── index.html     页面结构
│   ├── style.css      样式
│   └── app.js         播放器逻辑（字幕双轨渲染、控制栏、字幕稿）
├── cache/             字幕 / 译文 / 视频流缓存（自动生成）
└── run.sh             一键启动
```

## 已知限制

* YouTube 会按 IP 限流。字幕接口被限流时会提示「没能取到字幕」，稍后重试即可；已缓存的视频不受影响。
* 直链方案只拿到 360p 左右的单文件流（没有 ffmpeg 可用，无法把高清音视频轨合流）。想要更高画质就用 **YT**。
* iOS / WKWebView 会拦掉「无手势自动播放」（连静音也拦）。页面会先尝试有声、再试静音，都不行就显示
  「点一下画面即可开始」；这是系统策略，任何网页都绕不过去，点一下之后就一直能播了。
* 字幕两条轨道如果断句粒度差很多（比如某些人工中文字幕是每 4 秒一块），同一时刻两行说的不是同一句话是正常的
  —— 各自显示「此刻正在讲的那一行」。字幕稿里每块译文只出现一次。

---

## Cloudflare Pages 部署（云端版）

云端版只做「网页播放器」：画面用 YouTube 官方播放器（iframe），我们的双语字幕层叠在上面，
用 `getCurrentTime()` 对时；字幕与译文由一个无状态的 Worker 现取现译。

```
functions/api/load.js   Worker：ytcap.py 的 JS 版（innertube 取字幕 + gtx 机翻 + 缓存）
public/                 静态页（index.html / style.css / app.js）
build.sh                把 static/ 复制到 public/
wrangler.toml           Pages 配置（输出目录 public，functions/ 自动路由 /api/load）
```

部署步骤（Cloudflare 控制台，3 步）：

1. **Create a project** → *Pages* → *Connect to Git* → 选 `gmg0829/bilingual-youtube-player`
2. Framework preset 选 **None**，Build command 填 `sh build.sh`，Output directory 填 `public`
3. Deploy。函数在 `functions/api/load.js`，自动映射到 `/api/load`

或者用 CLI：

```bash
npm i -g wrangler
wrangler pages deploy public --project-name bilingual-youtube-player
```

### 云端版与本地版的分工

| | 本地版 | Cloudflare 版 |
|---|---|---|
| 网页播放器 | ✅ | ✅ |
| 视频画面 | 本地代理（`<video>`） | YouTube iframe |
| 「带字幕 mp4」导出 | ✅（ffmpeg + 系统播放器） | ❌（无持久磁盘、无 ffmpeg） |
| 成品分享 | 本机文件 | 需要 R2（下一步） |

### 已知风险

- **YouTube 对云 IP 的封锁**：数据中心段经常被 innertube 拒绝。Worker 里已加 10 分钟缓存，
  被拒时只能等 IP 解封或换字幕源。
- Worker 免费额度对个人自用足够；首次请求有冷启动。
