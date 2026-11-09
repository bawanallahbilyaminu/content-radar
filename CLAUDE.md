# CLAUDE.md — Content Radar

## What this project is
A daily **Researcher Agent** that finds what's trending and what people want in
Bilyaminu's niche: **AI / ML / Data / Cybersecurity** and related tech. It turns
those findings into **ranked video ideas** (news, tutorials, explainers) and stores
them in a **Notion database**. That database becomes his post-idea backlog.

Owner: Bilyaminu Bawan Allah (Sokoto, Nigeria). He posts on Facebook, X, TikTok,
Instagram and YouTube. All output ideas are for **video**.

## The core principle (read this before writing any prompt or scoring logic)
1. **Demand over trends.** Trending = what's new. Demand = what people are asking,
   are confused about, or are watching. Track both. Tutorials and explainers come
   from demand signals; news comes from trends.
2. **The angle is the edge.** He can't beat big channels on speed. Every idea must
   answer: *"What's the Nigerian / African angle?"* Examples: does this new model
   handle Hausa or Yoruba; what this breach means under the Nigeria Data Protection
   Act (NDPA); whether a tool works on Nigerian bandwidth and budgets; local data
   or local incidents. If there's no honest local angle, the idea can still be
   included, but it scores lower and the `angle` field should say so.
3. **Few, ranked ideas beat many ideas.** The daily run should produce a short,
   scored list, not a firehose. Default: **max 7 new ideas per run**, plus a
   "breaking" flag for urgent items.

## Architecture
```
collectors/  ->  normalize  ->  dedupe  ->  score/cluster  ->  synthesize (LLM)  ->  sinks/
 (sources)       (Item)        (seen)      (signals)          (Idea JSON)          (Notion)
```
- **Language:** Python 3.11+. Use `uv` if available, otherwise `venv` + `pip`.
- **Layout:**
  ```
  src/content_radar/
    collectors/      # one module per source, each returns list[Item]
    models.py        # pydantic: Item, Idea
    pipeline.py      # orchestrates collect -> dedupe -> score -> synthesize -> sink
    scoring.py       # deterministic signal scoring (no LLM)
    synthesize.py    # LLM call(s), structured JSON output validated by pydantic
    sinks/notion.py  # write ideas to Notion, dedupe against existing rows
    config.py        # loads config.yaml + env vars
  config.yaml        # sources, keywords, subreddits, feeds, limits, model names
  tests/
  .github/workflows/daily.yml
  ```
- **Every collector** is independent and fails safely: catch errors, log them,
  return `[]`. One broken source must never kill the run.
- **CLI:** `python -m content_radar run [--dry-run] [--sources hn,reddit] [--no-sink]`.
  `--dry-run` prints ideas as JSON and writes nothing.

## Sources
**Phase 1 (open, reliable, free):**
- Hacker News (Algolia API)
- Reddit via OAuth (script app), e.g. r/MachineLearning, r/LocalLLaMA, r/artificial,
  r/datascience, r/dataengineering, r/cybersecurity, r/netsec, r/Nigeria (tech posts).
  Capture **question posts** ("how do I…", "help") separately; they are demand signals.
- GitHub: recently created repos sorted by stars (Search API)
- Hugging Face: trending models and daily papers
- arXiv: cs.AI, cs.LG, cs.CL, cs.CR (titles and abstracts only)
- CISA Known Exploited Vulnerabilities JSON feed
- RSS: BleepingComputer, The Hacker News (security), Krebs on Security, TechCrunch AI,
  The Verge AI, TechCabal, Techpoint Africa, plus any feeds listed in `config.yaml`

**Phase 2:** YouTube Data API v3. Search niche keywords with `publishedAfter = now - 7d`
and `order = viewCount`. **Quota warning:** search costs 100 units and the daily
limit is 10,000. Cache results and keep searches to ~20/day max.

**Out of scope (do not build):** scraping X, TikTok or Instagram. Their terms forbid
it and they block scrapers, so it would be fragile. If a source needs scraping
HTML, prefer an RSS feed or official API instead and ask before scraping.

## Data models (pydantic)
- **Item:** `source, url, title, summary, published_at, engagement (dict), kind
  ("trend" | "question" | "paper" | "vuln" | "repo" | "video"), niche_tags`
- **Idea:** `title, hook (first 3 seconds of the video), angle (Nigeria/Africa angle
  or "none"), format ("news" | "tutorial" | "explainer"), length ("short" | "long"),
  niche ("ai_ml" | "data" | "cyber" | "other"), why_now, demand_signal, sources
  (list of URLs), score (0-100), effort ("low" | "med" | "high"), evergreen (bool),
  breaking (bool)`

## Scoring
Deterministic first, LLM second. `scoring.py` computes signals per item/cluster:
recency, engagement velocity, **cross-source count** (same topic on 2+ sources is
a strong signal), question volume. The LLM then writes ideas only from the top
clusters and assigns `score` using this rubric (keep it in the prompt):
timeliness 25, demand evidence 25, local angle 20, cross-source 15, effort (lower is
better) 15. Every idea must cite at least one source URL from the collected items;
never invent sources.

## LLM usage
- Claude API via the `anthropic` Python SDK. Key in `ANTHROPIC_API_KEY`.
- Model names live in `config.yaml`, never hardcoded. Defaults:
  `claude-haiku-5-5` for cheap per-item filtering/tagging,
  `claude-sonnet-5-5` for the final idea synthesis.
- Ask for **JSON output**, validate with pydantic, retry once on a validation error.
- Keep prompts in `src/content_radar/prompts/` as `.md` files, not inline strings.
- Log token usage per run so cost stays visible.

## Notion sink
- Database ID in `NOTION_IDEAS_DB_ID`, token in `NOTION_TOKEN`.
- Properties: Title, Hook, Angle, Format, Length, Niche, Why Now, Demand Signal,
  Sources, Score, Effort, Evergreen, Breaking, Status (New / Shortlisted / Scripting /
  Produced / Posted / Rejected), Date Found.
- New rows always get **Status = New**. Never edit or delete rows a human has
  changed; the sink only creates.
- Dedupe against existing rows by normalized title + primary source URL.
- If the database doesn't exist yet, provide `scripts/create_notion_db.py` to
  create it under a parent page ID given by the user.

## Scheduling
GitHub Actions (`.github/workflows/daily.yml`): cron daily at **06:00 Africa/Lagos
(05:00 UTC)**, plus `workflow_dispatch` for manual runs. Secrets come from GitHub
repo secrets. Dedupe state lives in Notion, so the workflow doesn't need to commit
files back to the repo.

## Git and GitHub workflow (mandatory)
- The repo is on GitHub, created with the `gh` CLI, **private** by default.
- **Commit and push after every meaningful edit**: a working collector, a new
  model, a passing test suite, a config change, a doc update. Don't batch a whole
  phase into one commit, and don't commit broken code to `main`.
- Before each commit: run `ruff check`, `ruff format`, and `pytest`. Fix failures
  first.
- **Conventional commits:** `feat:`, `fix:`, `refactor:`, `test:`, `docs:`,
  `chore:`, `ci:`. Subject under 72 chars, imperative mood.
  Example: `feat(collectors): add Hacker News collector with Algolia API`
- After each commit: `git push`. If a push fails, stop and report; never force-push.
- **Secrets never enter git.** `.env` is in `.gitignore`; keep `.env.example`
  current with every variable name and no values. Check `git diff --staged` for
  keys or tokens before every commit.

## Working rules
- Small, testable steps. Each collector gets a unit test using a saved fixture
  (recorded JSON/XML in `tests/fixtures/`); tests must never hit the network.
- Respect rate limits and terms of service; set a descriptive User-Agent; add
  backoff on 429s.
- When something needs a key or account Bilyaminu hasn't provided, stop and tell
  him exactly what to create and which env var it goes in.
- Keep `README.md` current: setup, env vars, how to run locally, how the schedule
  works.
- Dev environment is Windows 11 (PowerShell / Git Bash) using uv and Python 3.12.
  Production runs on GitHub Actions (Ubuntu), so keep code cross-platform: use
  pathlib, no shell-specific commands in Python.
