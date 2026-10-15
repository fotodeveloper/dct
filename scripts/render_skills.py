"""Regenerate packaged workflow artifacts, or check freshness without writing."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dct.skill_render import freshness_errors, render_all


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale/missing/unexpected outputs without writes")
    args = parser.parse_args()
    try:
        if args.check:
            errors = freshness_errors(ROOT)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
        else:
            for relative, text in render_all(ROOT).items():
                path = ROOT / relative
                if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(text.encode("utf-8"))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
