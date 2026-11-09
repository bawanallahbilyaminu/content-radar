Read CLAUDE.md fully before doing anything. It defines the project, architecture, sources, data models and the mandatory git workflow.

We're building **Content Radar**: a daily Researcher Agent that collects signals from tech news, Reddit, Hacker News, GitHub, Hugging Face, arXiv and security feeds, scores them, and uses Claude to turn the strongest ones into ranked video ideas with a Nigerian/African angle, saved to a Notion database.

## Step 0 — Preflight (do this first, then report)
1. Check that these are installed and working: `git`, `gh` (and `gh auth status`), `python3 --version` (need 3.11+), `uv` (optional).
2. If `gh` isn't authenticated, stop and tell me to run `gh auth login`. Don't continue until it works.
3. Confirm `git config user.name` and `user.email` are set; if not, ask me for them.

## Step 1 — Repo setup
1. Create the project folder `content-radar` and initialize git on branch `main`.
2. Add: `CLAUDE.md` (the one I gave you), `README.md` (short placeholder), `.gitignore` (Python, `.env`, `.venv`, caches), `.env.example`, `pyproject.toml` (deps: anthropic, httpx, feedparser, pydantic, pyyaml, python-dotenv, notion-client, praw; dev: pytest, ruff), and an empty `config.yaml` skeleton.
3. Create a **private** GitHub repo with `gh repo create content-radar --private --source=. --remote=origin`, make the first commit (`chore: initial project scaffold`) and push.
4. Show me the repo URL.

## Step 2 — Plan
Write a short implementation plan in `docs/PLAN.md` covering the phases below, commit it (`docs: add implementation plan`) and push. Then show me the plan and **wait for my go-ahead** before building.

- **Phase 1:** models, config loader, collectors (Hacker News, Reddit, GitHub, Hugging Face, arXiv, CISA KEV, RSS), dedupe, CLI with `--dry-run`. Tests with fixtures.
- **Phase 2:** scoring + LLM synthesis producing validated `Idea` objects. Prompts in `prompts/`.
- **Phase 3:** Notion sink + `scripts/create_notion_db.py`.
- **Phase 4:** GitHub Actions daily workflow (05:00 UTC) + manual trigger; README with setup steps and the list of repo secrets.
- **Phase 5 (later):** YouTube Data API collector; weekly digest; feedback loop from my own posts' performance.

## While building
- Commit and push after every meaningful edit, exactly as CLAUDE.md describes. Run ruff and pytest before each commit.
- When you need an API key or account (Anthropic, Reddit app, Notion integration, GitHub token, YouTube), stop and tell me exactly what to create, where, and which env var it belongs in.
- At the end of each phase, run `python -m content_radar run --dry-run` and show me a sample of the output so I can judge whether the ideas are actually useful before we continue.
