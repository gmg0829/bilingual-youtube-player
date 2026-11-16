"""Step 1 of make.sh: resolve a link and fetch its bilingual subtitles.

Exit codes: 0 ok · 2 bad link · 9 no subtitles right now (worth retrying).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ytcap  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fetch.py <url> <target-lang>")
        return 2
    url, tgt = sys.argv[1], sys.argv[2]
    vid = ytcap.parse_video_id(url)
    if not vid:
        print("  无法识别这个链接")
        return 2
    data = ytcap.load(vid, src="auto", tgt=tgt)
    meta = data["meta"]
    print("  标题:", (meta.get("title") or "?")[:70])
    print("  作者:", meta.get("author") or "?", "| 时长:", meta.get("duration"), "秒")
    print("  原文:", data["source"]["lang"], len(data["source"]["segments"]), "句")
    print("  译文:", data["target"]["lang"], data["target"]["method"],
          len(data["target"]["segments"]), "句")
    if not data["source"]["segments"]:
        return 9
    Path(".last_video_id").write_text(vid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
