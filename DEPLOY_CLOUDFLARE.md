# 任务书：把「双语 YouTube 播放器」部署到 Cloudflare Pages

> 这份文档是给执行者（另一个 AI agent 或人）看的操作说明。按顺序做，每步都有验收点。

---

## 0. 背景（你需要知道的全部上下文）

**项目**：一个双语 YouTube 播放器。输入一个 YouTube 链接 → 网页播放器自动播放，
画面下方同时显示**原文**和**译文**两行字幕；右侧是一份可点击跳转的双语字幕稿。

**代码仓库**：`https://github.com/gmg0829/bilingual-youtube-player`（公开仓库）

**要部署的分支**：**`feat/cloudflare`**（⚠️ 云端代码只在这个分支上，`main` 分支里没有云端文件，**不要动 main**）

**Cloudflare 版与本地版的分工**（部署的是前者）：

| | 本地版（不在你范围内） | Cloudflare 版（你要做的） |
|---|---|---|
| 网页播放器 | ✅ | ✅ |
| 视频画面 | 本地代理 `<video>` | YouTube 官方播放器 iframe |
| 字幕与译文 | Python 服务现取现译 | Worker（`functions/api/load.js`）现取现译 |
| 「带字幕 mp4」导出 | ✅（本机 ffmpeg） | ❌ 不支持，别实现 |

**技术构成**（不用改代码，只需配置）：

```
functions/api/load.js   Pages Function → 路由到 /api/load（字幕 + 翻译的 JS 实现）
public/                 静态站点输出目录（index.html / style.css / app.js）
build.sh                构建脚本：把 static/ 复制到 public/
wrangler.toml           Pages 配置（pages_build_output_dir = "public"）
```

---

## 1. 前置条件

- 一个 Cloudflare 账号（免费即可，不需要绑卡）——dash.cloudflare.com 注册
- 该账号能访问 GitHub 公开仓库 `gmg0829/bilingual-youtube-player`
- 目标分支 `feat/cloudflare` 已存在且包含上面 4 个文件（`functions/`、`public/`、`build.sh`、`wrangler.toml`）

---

## 2. 操作步骤

### 2.1 建项目

1. 打开 https://dash.cloudflare.com 并登录
2. 左侧菜单 **Workers & Pages**
3. 右上角 **Create application** → 选 **Pages** → **Connect to Git**

### 2.2 连仓库

4. 点 **GitHub** → 授权 → 选中账号 `gmg0829`
5. 在仓库列表里选 **`bilingual-youtube-player`** → 点 **Select**
6. ⚠️ **Production branch 改成 `feat/cloudflare`**（默认是 main，必须改）
7. 点 **Save and Deploy**

### 2.3 构建设置（首次部署会弹出表单）

8. 填：

   | 字段 | 值 |
   |---|---|
   | Framework preset | **None** |
   | Build command | **`sh build.sh`** |
   | Build output directory | **`public`** |
   | Environment variables | 留空 |
   | Root directory | 留空（仓库根） |

9. 点 **Save and Deploy**

### 2.4 等构建

10. 构建日志跑完（带自定义 build command 的首次构建用 Docker，约 2–5 分钟）。成功后得到地址：

```
https://<项目名>.pages.dev
```

---

## 3. 验收（必须逐条做完并把结果回传）

### 3.1 站点可达

```
GET https://<项目名>.pages.dev/           → 200，且 HTML 里出现「双语播放器」
GET https://<项目名>.pages.dev/app.js     → 200
GET https://<项目名>.pages.dev/style.css  → 200
```

### 3.2 接口可用

```
GET https://<项目名>.pages.dev/api/load?url=https%3A%2F%2Fyoutu.be%2Fy1-9yeYLJUo&tgt=zh-Hans
```

**期望返回 JSON**，且包含：

```json
{
  "videoId": "y1-9yeYLJUo",
  "meta": { "title": "The Rise Of AI Coworkers", "author": "Y Combinator", "duration": 3606 },
  "source": { "lang": "en",  "segments": [ { "start": 0.1, "duration": 1.8, "text": "…" } ] },
  "target": { "lang": "zh-Hans", "method": "mt", "segments": [ "…" ] },
  "notices": []
}
```

判定标准：

- `source.segments` 长度 **> 1000**（这条视频是 1833 句）
- `target.segments` 长度与 `source.segments` **大致相等**（1833 句）
- `target.segments[0].text` 是中文（例如「Grokbot 就是这个产品」）
- `meta.title` 不是空字符串

### 3.3 页面行为（用真实浏览器，不是内置 webview）

打开：

```
https://<项目名>.pages.dev/?url=https%3A%2F%2Fyoutu.be%2Fy1-9yeYLJUo
```

确认：

1. 页面标题是「双语播放器 · Bilingual Player」，整体是**真黑底**（`#000`），不是白底
2. 顶栏有：左侧「双语播放器」标志 + 中间链接输入框 + 右侧三个语言选择（源语言／译入／译文来源）
3. 左侧是视频区（YouTube iframe），右侧 384px 是「字幕稿」侧栏
4. **字幕稿里有 1800 行以上**，每行两行文字（英文白色 + 中文琥珀色）
5. 点视频区域的播放键后，**画面下方浮出两行字幕**（英文一行、译文一行）
6. 点字幕稿任意一行 → 视频跳到那一刻
7. 页面里**不应该**出现「带字幕播放」按钮（云端版隐藏了）

---

## 4. 出问题时怎么查

| 现象 | 原因 | 处理 |
|---|---|---|
| 构建报 `public` 不存在 | Output directory 填错 | 必须是 `public` |
| 构建报 shell 不存在 | Build command 写了 `bash` | 改 `sh build.sh`（Pages 容器里只有 `sh`） |
| 部署成功但页面 404 | Production branch 是 main | 回到项目设置改成 `feat/cloudflare`，重新部署 |
| `/api/load` 返回 500 且 error 里是 innertube 相关 | **YouTube 拒绝了 Cloudflare 的出口 IP**（已知风险，非配置错误） | 见 §5 |
| `/api/load` 返回的 segments 是空数组 | 同上 | 见 §5 |
| 视频区一片黑、不播 | Cloudflare 版用 YouTube iframe，需要**真实浏览器**（Safari/Chrome）；某些内置 webview 会禁播 | 换真实浏览器复测；若真实浏览器也不播，报给维护者 |
| 页面出来了但样式全丢 | `/style.css` 404 | 检查 `public/` 里三个文件是否齐全（构建脚本 `sh build.sh` 会重新拷贝） |

---

## 5. 已知风险与 B 计划（如果 §3.2 失败）

**风险**：YouTube 的 innertube 接口经常拒绝数据中心 IP（Cloudflare 出口属于这一类）。这是本项目最大的未知数。

**B 计划**（按顺序试，每步都重新验收 §3.2）：

1. **重试**：Cloudflare 的出口段会轮换，隔 10–30 分钟再试；Worker 里已加 10 分钟缓存，失败后不会反复打 YouTube
2. **换出口**：把 `functions/api/load.js` 里 `INNERTUBE_CTX.client.clientName` 由 `"ANDROID"` 改为 `"WEB"`（并把 `INNERTUBE_KEY` 换成 WEB 客户端的 key），重新部署再试
3. **换字幕源**：前端不依赖后端返回字幕轨的顺序，可以改成由前端直连 YouTube 的 `timedtext` 接口取原文，后端只负责翻译
4. **兜底**：如果 3 也不通，就退化成「网页播放器只出画面 + 机器翻译在浏览器端做（`translate.googleapis.com` 的 gtx 接口从浏览器直连）」

---

## 6. 边界（不要做的事）

- **不要合并到 `main`**、不要改 `main` 分支、不要 force push
- **不要修改** `functions/api/load.js`、`static/`、`public/` 里的任何代码（除非 §5 明确要求，且改了要单独提 PR 说明）
- **不要在本任务里配置 R2**（存成品 mp4 是下一步，单独的任务书）
- **不要把任何密钥/token 写进代码或提交到仓库**
- 不要删除或清理 `cache/`、`out/` 里的东西（那是本地机器上的）

---

## 7. 请回传这些信息

```
部署状态：成功 / 失败（附构建日志最后 30 行）
站点地址：https://xxxx.pages.dev
§3.2 的 JSON 摘要：source.segments 长度 = ？，target.segments 长度 = ？，method = ？
§3.3 的逐条结果：1 可达 / 2 顶栏 / 3 视频区 / 4 字幕行数 / 5 双语字幕浮出 / 6 点击跳转 / 7 无导出按钮
遇到的问题与已做的处理：
```

如果 §3.2 或 §3.3 有任何一条不通过，**先回传失败**，不要自行改代码硬凑。
