# Content Radar

A daily **Researcher Agent** that finds what is trending and what people are
asking about in **AI / ML / Data / Cybersecurity**, scores the signals, and
uses Claude to turn the strongest ones into **ranked video ideas with a
Nigerian / African angle**. Ideas land in a Notion database that serves as a
post-idea backlog.

**Status:** Phase 1 complete (collect, normalize, dedupe, CLI). Scoring, LLM
synthesis, the Notion sink and the daily schedule come next. See
`docs/PLAN.md` for the plan and `CLAUDE.md` for the project brief.

## Setup (dev, Windows or Linux)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # creates .venv and installs everything
cp .env.example .env         # then fill in the keys you have (all optional in Phase 1)
uv run python -m content_radar run --dry-run --no-sink
```

`--dry-run` prints the collected items as JSON and writes nothing. Add
`--sources hackernews,rss` to run a subset, `--limit 20` to print fewer items,
and `-v` for debug logging. A per-source summary is printed to stderr.

### Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests never touch the network. Every collector is tested against a recorded
fixture in `tests/fixtures/`.

## Environment variables

Copy `.env.example` to `.env`. Nothing in Phase 1 strictly requires a key.

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Phase 2 synthesis | Claude API. |
| `GITHUB_TOKEN` | GitHub collector | Optional locally but recommended: anonymous Search API allows 10 requests/min. GitHub Actions supplies one automatically. |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | Reddit collector | App-only (client_credentials) read-only OAuth, no username or password. Create a **script** app at reddit.com/prefs/apps. When missing or refused, the collector falls back to public subreddit Atom feeds. |
| `STACKEXCHANGE_KEY` | Stack Exchange collector | Optional. Raises quota from 300 to 10,000 requests/day. Register at stackapps.com. |
| `NOTION_TOKEN`, `NOTION_IDEAS_DB_ID` | Phase 3 sink | Internal integration token and the Ideas database ID. |
| `YOUTUBE_API_KEY` | Phase 5 | YouTube Data API v3. |

## Sources

| Source | What it collects | Kind |
|---|---|---|
| Hacker News (Algolia) | Front page, Ask HN, niche keyword searches | trend / question |
| Stack Exchange | Recent high-vote questions: Stack Overflow (python, machine-learning, pandas, llm, docker), Data Science, Security, AI | question |
| Reddit | Hot and new posts from r/MachineLearning, LocalLLaMA, artificial, datascience, dataengineering, cybersecurity, netsec, Nigeria | trend / question |
| GitHub | Repos created in the last 7 days, ranked by stars, niche-filtered, spam-filtered | repo |
| Hugging Face | Trending models and daily papers | trend / paper |
| arXiv | cs.AI, cs.LG, cs.CL, cs.CR titles and abstracts | paper |
| CISA KEV | Known Exploited Vulnerabilities added in the last 7 days | vuln |
| RSS | BleepingComputer, The Hacker News, Krebs, TechCrunch AI, The Verge AI, TechCabal, **NDPC, NITDA, Techeconomy, Benjamin Dada** | trend |

Reddit stores post-level fields only: title, URL, score, comment count, date,
flair. No author names, no post bodies, no comments. Stack Exchange calls honour
the API's `backoff` field and stop early on throttle errors or a low quota.

Sources that block non-browser clients (ngCERT, Techpoint Africa, Reddit's
public JSON) are **skipped, never scraped or worked around**. X, TikTok and
Instagram are out of scope.

## Time zone

All dates and time windows use **Africa/Lagos**. Items are stored with a
`+01:00` offset. The default lookback is 48 hours; slower sources (arXiv,
Hugging Face papers, CISA KEV, Stack Exchange) use longer windows set in
`config.yaml`.

## Configuration

`config.yaml` holds everything that is not a secret: niche keywords, local-angle
keywords, question markers, per-source limits and windows, subreddits, Stack
Exchange sites and tags, RSS feeds, and model names. Secrets only ever come from
`.env` or GitHub repo secrets.

## Schedule

Coming in Phase 4: a GitHub Actions workflow runs daily at 06:00 Africa/Lagos
(05:00 UTC) with a manual trigger. Dedupe state lives in Notion, so the workflow
never commits back to the repo.
