"""CLI: python -m content_radar run [--dry-run] [--sources hn,reddit] [--no-sink]
python -m content_radar scout [--dry-run] [--no-sink] [--sources ...]."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from content_radar.config import Settings, load_settings
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
    run_p.add_argument(
        "--skip-llm", action="store_true", help="stop after collection; no Claude calls"
    )
    run_p.add_argument("--limit", type=int, default=None, help="print at most N items (dry run)")
    run_p.add_argument(
        "--items", action="store_true", help="in dry run, print collected items instead of ideas"
    )
    scout_p = sub.add_parser(
        "scout", help="Worth the Clone? repo scout: trending repos to the Notion repos table"
    )
    scout_p.add_argument(
        "--dry-run", action="store_true", help="print ranked repos as JSON, write nothing"
    )
    scout_p.add_argument("--no-sink", action="store_true", help="skip writing to Notion")
    scout_p.add_argument(
        "--sources", default=None, help="comma-separated subset, e.g. trending_weekly,search"
    )
    scout_p.add_argument("--limit", type=int, default=None, help="print at most N repos (dry run)")
    return parser


def _run_scout(args: argparse.Namespace, settings: Settings) -> int:
    from content_radar.scout.run import run_scout

    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    result = run_scout(settings, dry_run=args.dry_run, sink=not args.no_sink, sources=sources)
    print(result.summary(), file=sys.stderr)
    if result.skipped_reason:
        print(f"SKIPPED: {result.skipped_reason}", file=sys.stderr)
    if not args.dry_run and not args.no_sink and not result.skipped_reason:
        print(f"notion repos: {result.created} created, {result.updated} updated", file=sys.stderr)
    if args.dry_run:
        repos = result.repos[: args.limit] if args.limit else result.repos
        print(json.dumps([r.model_dump(mode="json") for r in repos], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(args.config)

    if args.command == "run":
        sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
        result = run(
            settings,
            sources=sources,
            dry_run=args.dry_run,
            sink=not args.no_sink,
            skip_llm=args.skip_llm,
        )
        print(result.summary(), file=sys.stderr)
        if result.llm_summary:
            print(result.llm_summary, file=sys.stderr)
        if result.skipped_reason:
            print(f"SKIPPED: {result.skipped_reason}", file=sys.stderr)
        if not args.dry_run and not args.no_sink:
            print(f"notion: {result.written} new row(s) written", file=sys.stderr)
        if args.dry_run:
            show_items = args.items or not result.ideas
            records = result.items if show_items else result.ideas
            if args.limit:
                records = records[: args.limit]
            print(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
        return 0
    if args.command == "scout":
        return _run_scout(args, settings)
    return 1


if __name__ == "__main__":
    sys.exit(main())
