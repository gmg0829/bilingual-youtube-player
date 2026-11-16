#!/bin/sh
# 给一个 YouTube 链接，产出一个「内嵌双语字幕」的 mp4。
#
#   sh make.sh https://youtu.be/XXXX              # 译文简体中文，画质上限 1080p
#   sh make.sh https://youtu.be/XXXX ja           # 指定译文语言
#   sh make.sh https://youtu.be/XXXX zh-Hans 720  # 指定画质上限
#
# 产物：out/<videoId>-双语.mp4 —— 可直接用系统播放器打开，字幕跟着画面走。

set -u
URL="${1:-}"
TGT="${2:-zh-Hans}"
HEIGHT="${3:-1080}"

if [ -z "$URL" ]; then
  echo "用法: sh make.sh <youtube链接> [译文语言=zh-Hans] [画质上限=1080]"
  exit 1
fi

cd "$(dirname "$0")" || exit 1
mkdir -p out

echo "① 解析链接与字幕（译文语言 $TGT）…"
attempt=1
while : ; do
  python3 fetch.py "$URL" "$TGT" && break
  rc=$?
  if [ "$rc" -ne 9 ] || [ "$attempt" -ge 3 ]; then
    echo "  YouTube 可能正在限流（同一 IP 请求过多）。过几分钟再跑一次即可。"
    exit 4
  fi
  echo "  第 $attempt 次没取到字幕，60 秒后重试…"
  attempt=$((attempt + 1))
  sleep 60
done

VID=$(cat .last_video_id)

echo "② 下载视频（画质上限 ${HEIGHT}p）…"
if [ ! -s "cache/media/$VID.mp4" ]; then
  yt-dlp --no-warnings --no-playlist \
    --extractor-args "youtube:player_client=android" \
    -f "bestvideo[height<=${HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" \
    --merge-output-format mp4 \
    -o "cache/media/%(id)s.%(ext)s" \
    "https://www.youtube.com/watch?v=$VID" || {
      echo "  下载失败（YouTube 可能限流，稍后重试）"; exit 5; }
fi
ls -lh "cache/media/$VID.mp4" | awk '{print "  ",$5}'

echo "③ 生成双语字幕文件…"
python3 mksrt.py "$VID" "$TGT" || exit 6

echo "④ 内嵌字幕（不重编码，只改容器）…"
SRT=$(ls cache/media/"$VID".*.srt 2>/dev/null | head -1)
ffmpeg -y -loglevel error -i "cache/media/$VID.mp4" -i "$SRT" \
  -map 0:v -map 0:a? -map 1:0 -c copy -c:s mov_text \
  -metadata:s:s:0 language=zho -metadata:s:s:0 title="EN+ZH" \
  -disposition:s:0 default "out/$VID-双语.mp4" || exit 7

ls -lh "out/$VID-双语.mp4" | awk '{print "  ",$5}'
echo
echo "完成 →  out/$VID-双语.mp4"
echo "点开就能看，双语字幕跟着画面走。"
