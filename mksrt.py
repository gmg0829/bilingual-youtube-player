"""Step 3 of make.sh: write the bilingual .srt next to the downloaded mp4."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mksrt.py <videoId> [target-lang]")
        return 2
    vid = sys.argv[1]
    tgt = sys.argv[2] if len(sys.argv) > 2 else "zh-Hans"
    p = export.write_srt(vid, tgt=tgt)
    print("  ", p.name, p.stat().st_size // 1024, "KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
