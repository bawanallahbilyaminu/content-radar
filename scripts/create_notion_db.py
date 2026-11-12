"""Create a Content Radar Notion database under a Notion parent page.

Usage:
    uv run python scripts/create_notion_db.py [--kind ideas|repos] [--parent PAGE_ID]
        [--title NAME] [--write-env]

--kind ideas (default) creates the Ideas database (NOTION_IDEAS_DB_ID).
--kind repos creates the Worth the Clone? trending repos database (NOTION_REPOS_DB_ID).

Reads NOTION_TOKEN and NOTION_PARENT_PAGE_ID from .env unless overridden. The parent
page must be shared with the integration. Prints the new database ID; with
--write-env it also sets the matching *_DB_ID variable in .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from content_radar.scout.sink import PROPERTIES as REPO_PROPERTIES  # noqa: E402
from content_radar.sinks.notion import PROPERTIES as IDEA_PROPERTIES  # noqa: E402

KINDS = {
    "ideas": ("Content Radar Ideas", "NOTION_IDEAS_DB_ID", IDEA_PROPERTIES),
    "repos": ("Worth the Clone - Trending Repos", "NOTION_REPOS_DB_ID", REPO_PROPERTIES),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Content Radar Notion database")
    parser.add_argument("--kind", choices=sorted(KINDS), default="ideas")
    parser.add_argument(
        "--parent", default=None, help="parent page ID (default: NOTION_PARENT_PAGE_ID)"
    )
    parser.add_argument("--title", default=None, help="database title (default per kind)")
    parser.add_argument("--write-env", action="store_true", help="write the database ID into .env")
    args = parser.parse_args()
    default_title, env_var, properties = KINDS[args.kind]
    title = args.title or default_title

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
    if os.environ.get(env_var, "").strip():
        print(
            f"{env_var} is already set ({os.environ[env_var]}); "
            "unset it first if you really want a second database.",
            file=sys.stderr,
        )
        return 1

    from notion_client import Client

    client = Client(auth=token)
    db = client.databases.create(
        parent={"type": "page_id", "page_id": parent},
        title=[{"type": "text", "text": {"content": title}}],
        initial_data_source={"properties": properties},
    )
    db_id = db["id"]
    ds = (db.get("data_sources") or [{}])[0].get("id", "?")
    print(f"Created database '{title}'")
    print(f"  database id:    {db_id}")
    print(f"  data source id: {ds}")
    print(f"  url:            {db.get('url', '')}")
    if args.write_env:
        set_key(str(env_path), env_var, db_id, quote_mode="never")
        print(f"  wrote {env_var} to {env_path}")
    else:
        print(f"Add to .env:  {env_var}={db_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
