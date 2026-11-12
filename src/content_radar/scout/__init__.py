"""Repo scout for "Worth the Clone?": find this week's trending GitHub repos, rank them
by momentum, flag risky ones and keep a separate Notion table of candidates.

Separate from the ideas pipeline: no LLM calls, its own CLI command
(`python -m content_radar scout`) and its own Notion database (NOTION_REPOS_DB_ID).
"""
