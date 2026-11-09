# Content Radar — Implementation Plan

Build order follows `KICKOFF_PROMPT.md`. Each phase ends with a passing
`ruff check`, `ruff format --check`, `pytest`, a `--dry-run` sample for review,
and a push to `main`. Commits are small and conventional (`feat:`, `test:`, …).

Guiding rules (from `CLAUDE.md`): demand over trends, the Nigerian / African
angle is the edge, few ranked ideas beat many, every collector fails safely,
no secrets in git, tests never touch the network.

---

## Phase 1 — Collect, normalize, dedupe, CLI ✅

Goal: `python -m content_radar run --dry-run --no-sink` prints a deduplicated,
tagged list of `Item`s from every source without any LLM calls.

1. **`models.py`** — pydantic `Item` and `Idea` exactly as specified in
   `CLAUDE.md`, plus `Item.fingerprint()` (normalized title + canonical URL)
   used for dedupe.
2. **`config.py`** — load `config.yaml`, overlay env vars from `.env`
   (`python-dotenv`), expose a typed `Settings` object. Fill `config.yaml`
   with real niche keywords, local-angle keywords, subreddits, arXiv
   categories and RSS feeds.
3. **`collectors/base.py`** — shared `httpx` client with descriptive
   `User-Agent`, timeout, retry/backoff on 429 and 5xx, and a `safe_collect()`
   wrapper that logs and returns `[]` on any exception.
4. **Collectors**, one module each, each with a recorded fixture in
   `tests/fixtures/` and a unit test:
   - `hackernews.py` — Algolia `search_by_date` with niche keywords, last 48h.
   - `reddit.py` — **built last**, after a dry run of the keyless collectors
     is reviewed. PRAW script app; hot + new per subreddit; posts with a `?`
     title or "how do I / help / confused" phrasing tagged `kind="question"`.
     Fallback when OAuth creds are missing or API approval is slow: public
     subreddit RSS feeds (`/r/<sub>/new/.rss`) through the RSS collector.
   - `github.py` — Search API, repos created in the last 7 days sorted by
     stars, filtered to niche topics.
   - `huggingface.py` — trending models API and daily papers page JSON.
   - `arxiv.py` — Atom API for cs.AI, cs.LG, cs.CL, cs.CR (title + abstract).
   - `cisa_kev.py` — Known Exploited Vulnerabilities JSON, `kind="vuln"`.
   - `rss.py` — `feedparser` over the feeds in `config.yaml`.
5. **`normalize.py` / dedupe** — canonical URLs (strip tracking params),
   normalized titles, in-run dedupe by fingerprint, simple keyword tagging
   into `niche_tags`.
6. **`__main__.py` / CLI** — `argparse`: `run [--dry-run] [--sources a,b]
   [--no-sink] [--limit N]`. `--dry-run` prints JSON and writes nothing.
7. **`pipeline.py`** — orchestrates collect → normalize → dedupe; later phases
   plug in score → synthesize → sink.
8. **Tests** — one per collector (fixture-driven), one for dedupe, one for
   config loading. Add a `conftest.py` that blocks real HTTP.

   - `stackexchange.py` — **primary demand-signal source.** Stack Exchange API
     2.3, recent high-vote questions from stackoverflow (python,
     machine-learning, pandas, llm, docker), datascience, security and ai.
     `kind="question"`. Honours `backoff`, stops on throttle errors and
     below a quota floor. Optional `STACKEXCHANGE_KEY` raises the quota.
   - Nigerian feeds verified: NDPC, NITDA, Techeconomy, Benjamin Dada.
     ngCERT and Techpoint block non-browser clients and are skipped (never
     scraped or worked around).
   - All dates and windows use Africa/Lagos (`timeutil.py`).

**Keys needed:** Reddit script app (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`,
`REDDIT_USER_AGENT`). Optional `GITHUB_TOKEN` for higher Search API limits.
Everything else in Phase 1 is keyless.

**Deliverable for review:** dry-run output showing items per source and the
tagged question posts.

---

## Phase 2 — Score and synthesize ✅

Goal: the dry run prints up to 7 validated `Idea` objects with scores.

Done 2026-11-09. Additions over the original plan: a **cost guard**
(`budget.max_cost_usd_per_run`, default $0.25) that estimates with the free
`count_tokens` endpoint and skips LLM steps with a logged reason; structured
outputs via `messages.parse`; truncated or malformed model output fails open
(filter) or retries once (synthesis) instead of crashing the run.

1. **`scoring.py`** (deterministic, no LLM) — per-item signals: recency
   decay, engagement velocity (points / comments / stars per hour), question
   flag, local-angle keyword hit. Cluster items by normalized title tokens and
   shared URLs; **cross-source count** is the strongest cluster signal.
   Output: ranked clusters with a numeric pre-score.
2. **`prompts/filter.md`** — Haiku pass over the top items, **batched ~50
   items per call** (never one call per item): tag niche, drop off-topic,
   flag obvious local angle. JSON out, pydantic-validated. Log the number of
   calls and tokens used.
3. **`prompts/synthesize.md`** — Sonnet pass over the top clusters. Carries
   the rubric verbatim (timeliness 25, demand 25, local angle 20, cross-source
   15, effort 15), the "angle is the edge" rule, the max-7 cap, the `breaking`
   flag, and the rule that every idea must cite a real collected URL.
4. **`synthesize.py`** — Claude API via `anthropic` SDK, model names from
   `config.yaml`, JSON output, pydantic validation, one retry on validation
   error, token usage logged per run. Post-validate that every `sources` URL
   exists in the collected set; drop ideas that fail.
5. **Tests** — scoring on synthetic items; synthesize with a mocked client
   returning fixture JSON (valid and invalid, to exercise the retry).

**Keys needed:** `ANTHROPIC_API_KEY`.

**Deliverable for review:** 7 sample ideas as JSON so the usefulness and the
local angles can be judged.

---

## Phase 3 — Notion sink

Goal: ideas land in the Notion database as `Status = New` rows, no duplicates.

1. **`scripts/create_notion_db.py`** — creates the Ideas database under a
   parent page ID with all properties from `CLAUDE.md` (Title, Hook, Angle,
   Format, Length, Niche, Why Now, Demand Signal, Sources, Score, Effort,
   Evergreen, Breaking, Status, Date Found). Prints the new DB ID.
2. **`sinks/notion.py`** — query existing rows, build a set of
   (normalized title, primary source URL), create only new pages. Never
   updates or deletes.
3. **Pipeline wiring** — `--no-sink` skips it; `--dry-run` still never writes.
4. **Tests** — mocked `notion_client`, fixture of existing rows, assert that
   duplicates are skipped and new rows have `Status = New`.

**Keys needed:** Notion internal integration (`NOTION_TOKEN`) shared with the
parent page, then `NOTION_IDEAS_DB_ID` after the script runs.

**Deliverable for review:** a real run that writes to Notion and a screenshot
or row count.

---

## Phase 4 — Schedule and docs

Goal: runs unattended every morning.

1. **`.github/workflows/daily.yml`** — `cron: "0 5 * * *"` (06:00
   Africa/Lagos) plus `workflow_dispatch`. Ubuntu runner, `uv sync`,
   `uv run python -m content_radar run`. Secrets from repo settings.
   Concurrency group so two runs never overlap. Upload the run log as an
   artifact.
2. **CI on push** — `ruff check`, `ruff format --check`, `pytest` on every
   push and PR.
3. **`README.md`** — setup, every env var, how to run locally, how the
   schedule works, the full list of GitHub repo secrets, how to trigger a
   manual run.

**Deliverable for review:** a successful `workflow_dispatch` run visible in
the Actions tab.

---

## Phase 5 — Later

- **YouTube Data API v3 collector** — niche keyword search,
  `publishedAfter = now - 7d`, `order = viewCount`; cache results; hard cap
  of ~20 searches/day against the 10,000-unit quota. `kind="video"`.
- **Weekly digest** — Sunday run that summarizes the week's top ideas and
  what was produced.
- **Feedback loop** — pull performance of posted videos (starting with the
  YouTube Analytics API) and feed it back as a demand signal for the
  scorer.

---

## Decisions already made

- Windows 11 dev with `uv` + Python 3.12; production on GitHub Actions
  (Ubuntu). Cross-platform code only: `pathlib`, no shell-outs.
- `.gitattributes` forces LF so Windows and Linux diffs stay clean.
- Dedupe state lives in Notion, so the workflow never commits back to the repo.
- No scraping of X, TikTok or Instagram.
