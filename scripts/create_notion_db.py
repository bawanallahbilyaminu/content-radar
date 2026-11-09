"""Create the Content Radar Ideas database under a Notion parent page.

Usage:
    uv run python scripts/create_notion_db.py [--parent PAGE_ID] [--title NAME] [--write-env]

Reads NOTION_TOKEN and NOTION_PARENT_PAGE_ID from .env unless overridden. The parent
page must be shared with the integration. Prints the new database ID; with
--write-env it also sets NOTION_IDEAS_DB_ID in .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from content_radar.sinks.notion import PROPERTIES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the Notion Ideas database")
    parser.add_argument(
        "--parent", default=None, help="parent page ID (default: NOTION_PARENT_PAGE_ID)"
    )
    parser.add_argument("--title", default="Content Radar Ideas")
    parser.add_argument(
        "--write-env", action="store_true", help="write NOTION_IDEAS_DB_ID into .env"
    )
    args = parser.parse_args()

    env_path = ROOT / ".env"
    load_dotenv(env_path)
    token = os.environ.get("NOTION_TOKEN", "").strip()
    parent = (args.parent or os.environ.get("NOTION_PARENT_PAGE_ID", "")).strip()
    if not token:
        print("NOTION_TOKEN is not set", file=sys.stderr)
        return 2
    if not parent:
        print("No parent page: pass --parent or set NOTION_PARENT_PAGE_ID", file=sys.stderr)
        return 2
    if os.environ.get("NOTION_IDEAS_DB_ID", "").strip():
        print(
            f"NOTION_IDEAS_DB_ID is already set ({os.environ['NOTION_IDEAS_DB_ID']}); "
            "unset it first if you really want a second database.",
            file=sys.stderr,
        )
        return 1

    from notion_client import Client

    client = Client(auth=token)
    db = client.databases.create(
        parent={"type": "page_id", "page_id": parent},
        title=[{"type": "text", "text": {"content": args.title}}],
        initial_data_source={"properties": PROPERTIES},
    )
    db_id = db["id"]
    ds = (db.get("data_sources") or [{}])[0].get("id", "?")
    print(f"Created database '{args.title}'")
    print(f"  database id:    {db_id}")
    print(f"  data source id: {ds}")
    print(f"  url:            {db.get('url', '')}")
    if args.write_env:
        set_key(str(env_path), "NOTION_IDEAS_DB_ID", db_id, quote_mode="never")
        print(f"  wrote NOTION_IDEAS_DB_ID to {env_path}")
    else:
        print("Add to .env:  NOTION_IDEAS_DB_ID=" + db_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
