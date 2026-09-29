# Repository Guidelines

## Project Structure & Module Organization

JobHunt is a **flat-layout** Python project — all modules live at the top level:

```
jobhunt/                          # (repo root)
├── app.py                        # Flask entry point + routes
├── config.py                     # Configuration from env vars and profile
├── db.py                         # SQLite helpers & schema init
├── dedup.py                      # Duplicate detection rules
├── filters.py                    # Stage-0 relevance: region and age filtering
├── generation.py                 # CV and cover letter document rendering
├── ingest.py                     # Single database writer; owns all inserts
├── llm.py                        # LLM client abstraction (Anthropic / OpenAI)
├── scoring.py                    # Stage-1 fit: one LLM call per posting
├── scheduler.py                  # APScheduler cycle: fetch → score → notify
├── slackbot.py                   # Slack Socket Mode integration
├── sources/                      # One adapter per board (arbeitnow, arbeitsagentur, ats, manual)
│   ├── base.py                   # Abstract source interface
│   └── *.py                      # Concrete adapters
├── templates/                    # Jinja2 HTML templates for the web UI
├── tests/                        # Unit tests + fixtures/
├── scripts/                      # Utility scripts (preview, secret management)
├── requirements.txt              # Pinned production dependencies
└── deploy-to-pi.sh               # One-command deployment to Raspberry Pi
```

## Build, Test, and Development Commands

Requires **Python 3.13** and an OpenAI-compatible local model server (LM Studio or Ollama).

| Command | Purpose |
|---|---|
| `python3.13 -m venv venv` | Create a virtual environment |
| `./venv/bin/pip install -r requirements-dev.txt` | Install all dependencies (prod + dev) |
| `./venv/bin/python -m jobhunt.app` | Start the Flask app on port 5002 |
| `./venv/bin/python -m unittest discover -s tests -t ..` | Run the full test suite (no network) |
| `./venv/bin/pyflakes .` | Quick lint check across all Python files |

Set environment variables to control behaviour — see `README.md` for the full list (`HOST`, `PORT`, `JOBHUNT_PASSWORD`, etc.).

## Coding Style & Naming Conventions

- **Indentation**: 4 spaces, no tabs.
- **Naming**: modules and functions use `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`.
- **Imports**: standard library first, then third-party, then local — each group separated by a blank line.
- **Linting**: `pyflakes` is the only enforced linter (listed in `requirements-dev.txt`). Run it before committing to catch unused imports and undefined names.
- **No formatting tool** is configured; keep changes consistent with the surrounding code's style.

## Testing Guidelines

The project uses Python's built-in `unittest` framework — no external test runner.

- **Test files**: named `test_<module>.py`, placed in the `tests/` directory alongside `fixtures/`.
- **Naming convention**: test methods start with `test_`; use descriptive names that state the scenario, e.g., `test_dedup_matches_same_title_and_company()`.
- **No network calls**: adapters run against saved real responses in `tests/fixtures/`; ingest tests use stub adapters against a temporary database.
- **Run all tests**: `./venv/bin/python -m unittest discover -s tests -t ..`
- **Run a single file**: `./venv/bin/python -m unittest tests/test_dedup.py`

Aim to add or update tests whenever you modify logic in `dedup.py`, `ingest.py`, `filters.py`, `scoring.py`, or any source adapter — these have the highest bug surface.

## Commit & Pull Request Guidelines

- **Commit messages**: follow a concise imperative style, e.g., `fix: handle missing location field in arbeitnow response`.
- **Scope changes deliberately**: dependencies are pinned exactly — upgrade one line at a time and run the suite before deploying.
- **Pull requests** (when applicable): include a brief description of what changed, link any related issues, and note which tests cover the change. For UI changes, add a screenshot.

## Security & Configuration Tips

- Never commit `profile_local.json`, `.env` files, or database files — they are gitignored.
- The app refuses to bind on non-loopback addresses without `JOBHUNT_PASSWORD`.
- Secrets (API tokens, Slack credentials) live in the SQLite database or environment variables; nothing is committed.
