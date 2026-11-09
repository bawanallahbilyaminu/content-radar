"""CLI: python -m content_radar run [--dry-run] [--sources hn,reddit] [--no-sink]."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from content_radar.config import load_settings
from content_radar.pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="content_radar", description="Content Radar researcher agent"
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the daily pipeline")
    run_p.add_argument(
        "--dry-run", action="store_true", help="print results as JSON, write nothing"
    )
    run_p.add_argument(
        "--sources", default=None, help="comma-separated subset, e.g. hackernews,rss"
    )
    run_p.add_argument("--no-sink", action="store_true", help="skip writing to Notion")
    run_p.add_argument("--limit", type=int, default=None, help="print at most N items (dry run)")
    run_p.add_argument(
        "--items", action="store_true", help="in dry run, print collected items instead of ideas"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(args.config)

    if args.command == "run":
        sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
        result = run(settings, sources=sources, dry_run=args.dry_run, sink=not args.no_sink)
        print(result.summary(), file=sys.stderr)
        if args.dry_run:
            show_items = args.items or not result.ideas
            records = result.items if show_items else result.ideas
            if args.limit:
                records = records[: args.limit]
            print(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
