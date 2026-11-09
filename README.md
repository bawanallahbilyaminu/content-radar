# Content Radar

A daily **Researcher Agent** that finds what is trending and what people are
asking about in **AI / ML / Data / Cybersecurity**, scores the signals, and
uses Claude to turn the strongest ones into **ranked video ideas with a
Nigerian / African angle**. Ideas land in a Notion database that serves as a
post-idea backlog.

**Status:** Phases 1 to 3 complete: collect, normalize, dedupe, deterministic
scoring, a batched Haiku filter, Sonnet synthesis into ranked `Idea` objects under
a per-run cost guard, and a create-only Notion sink. The daily schedule (Phase 4)
comes next. See `docs/PLAN.md` for the plan and `CLAUDE.md` for the project brief.

## Setup (dev, Windows or Linux)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # creates .venv and installs everything
cp .env.example .env         # then fill in the keys you have (all optional in Phase 1)
uv run python -m content_radar run --dry-run --no-sink
```

`--dry-run` runs the whole pipeline, including the Claude calls, and prints the
ranked ideas as JSON without writing anywhere. Add `--skip-llm` to stop after
collection (prints items instead), `--items` to print items even when ideas
exist, `--sources hackernews,rss` to run a subset, `--limit 20` to print fewer
records, and `-v` for debug logging. A per-source summary and the LLM token /
cost summary are printed to stderr.

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

## Pipeline

```
collect -> tag/dedupe -> score & cluster -> Haiku filter (batched) -> Sonnet synthesis -> ideas
```

- **Scoring** (`scoring.py`, no LLM): recency decay by kind, log-scaled engagement,
  bonuses for questions and local-angle items, title clustering, and a strong
  bonus when the same topic appears on 2+ independent sources.
- **Filter** (`filter.py`): the top 120 items go to `claude-haiku-5-5` in batches of
  about 50 per call (never one call per item). Each verdict carries keep/drop,
  niche, an honest local angle or null, and a demand flag. Items the model skips
  are kept (fail-open).
- **Synthesis** (`synthesize.py`): the top 25 clusters go to `claude-sonnet-5-5`
  with the scoring rubric from `CLAUDE.md` (timeliness 25, demand 25, local angle
  20, cross-source 15, effort 15). Output is schema-validated JSON. Every idea must
  cite URLs from the collected items; invented URLs are dropped and the model gets
  one retry with the validation errors. At most 7 ideas per run.
- **Prompts** live in `src/content_radar/prompts/*.md`.

### Cost guard

`budget.max_cost_usd_per_run` in `config.yaml` (default **$0.25**) caps each run.
Before any paid call the pipeline counts input tokens with the free
`count_tokens` endpoint, adds the configured output estimates and the prices in
`budget.pricing`, and skips the LLM steps with a logged reason if the projection
exceeds the cap. Actual tokens and cost per call are logged; a typical run costs
about $0.05.

### Idea rules enforced in code

The model proposes, code decides. After synthesis every idea is checked:

- **Sources** must be URLs from the collected items and each carries a one-phrase
  justification (`source_notes`). Unknown or unjustified URLs are dropped.
- **Breaking** is kept only if a cited item was published in the last 48 hours *and*
  the topic appeared on 2+ sources, is a CISA KEV entry, or is a Hacker News thread
  with 300+ points.
- **Angle evidence** is `sourced` only when `angle_source` is a collected local item
  (TechCabal, NDPC, NITDA, Techeconomy, Benjamin Dada, r/Nigeria, or anything tagged
  local). Otherwise it is `assumed`, and the local-angle rubric points are capped at
  6. `angle: none` earns 0.
- **Score** is recomputed from the model's rubric breakdown, and ideas under
  `run.min_idea_score` (default 55) are dropped. Fewer than 7 ideas is fine.

## Notion

One-time setup: create an internal integration at notion.so/profile/integrations,
put its token in `NOTION_TOKEN`, share a parent page with the integration, put the
page ID in `NOTION_PARENT_PAGE_ID`, then run:

```bash
uv run python scripts/create_notion_db.py --write-env
```

This creates the Ideas database with every property (Title, Hook, Angle, Angle
Evidence, Angle Source, Format, Length, Niche, Why Now, Demand Signal, Sources,
Primary Source, Score, Effort, Evergreen, Breaking, Status, Date Found) and writes
`NOTION_IDEAS_DB_ID` into `.env`.

The sink only ever **creates** rows, always with `Status = New`. It dedupes against
existing rows by normalized title plus primary source URL and never edits or deletes
a row, so anything a human changes stays changed. `--no-sink` skips it; `--dry-run`
never writes.

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
