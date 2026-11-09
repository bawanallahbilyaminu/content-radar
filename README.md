# Content Radar

A daily Researcher Agent that finds what's trending and what people are asking
about in AI / ML / Data / Cybersecurity, scores the signals, and uses Claude to
turn the strongest ones into ranked video ideas with a Nigerian / African angle.
Ideas are saved to a Notion database as a post-idea backlog.

**Status:** scaffold only. See `docs/PLAN.md` for the build plan and `CLAUDE.md`
for the project brief.

## Quick start (dev)

```bash
uv sync
cp .env.example .env   # fill in keys
uv run python -m content_radar run --dry-run
```
