"""Command-line entry point: `python -m mewbo_demo_framer`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import FrameRunner

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _PACKAGE_ROOT.parents[1]


def main(argv: list[str] | None = None) -> int:
    """Render every declared artifact, printing one line per file."""
    parser = argparse.ArgumentParser(prog="mewbo-demo-frame", description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_PACKAGE_ROOT / "artifacts.json",
        help="manifest declaring each artifact's treatment",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=_REPO_ROOT / "docs" / "assets" / "img-src",
        help="raw captures, the input tree",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_REPO_ROOT / "docs" / "assets" / "img",
        help="published artifacts, the tree docs and the README reference",
    )
    args = parser.parse_args(argv)

    runner = FrameRunner(
        manifest_path=args.manifest,
        source_dir=args.source_dir,
        output_dir=args.output_dir,
    )
    try:
        lines = runner.run()
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for line in lines:
        print(line)
    print(f"\n{len(lines)} artifacts published to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
