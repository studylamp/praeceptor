# CLAUDE.md — Praeceptor

Self-hosted homeschool tutoring harness: kids chat with a Claude-powered tutor
scoped to their enrolled subjects; a parent admin console configures everything and
reviews every conversation. A classifier **gate** blocks off-topic chat; per-student
daily caps bound usage. Math/science subjects can run **sandboxed Python**
(sympy/numpy/matplotlib) to compute & verify answers and draw vector-SVG plots,
opt-in per subject. LAN-only, Docker.

## Working agreements (IMPORTANT)

- **Never auto-commit.** Run `git commit` only when explicitly asked; don't push
  unless asked.
- **Review before proposing a commit.** Fan out parallel review agents over the
  diff and fold in their findings, *then* propose the commit and wait for approval.
- **No Claude/Anthropic by-line in commit messages.** Do not append a
  `Co-Authored-By: Claude` line or a "Generated with Claude Code" footer.

## Stack & layout

Python + FastAPI · Jinja2 + HTMX (vendored, no CDN) · SQLite (plain `sqlite3`, WAL)
· LiteLLM gateway (**provider-flexible** — model strings are `provider/model`) ·
sympy/numpy/matplotlib run in a separate-process sandbox ([app/sandbox/](app/sandbox/))
for the tutor's tools ([app/tools/](app/tools/)). Deps via **uv**. App entry:
[app/main.py](app/main.py).

## Structure

```
praeceptor/ (repo root)
├─ docker-compose.yml      # LAN-bound app + network-isolated sandbox worker; named volumes
├─ Dockerfile             # non-root image; APP_VERSION build arg
├─ .env.example           # provider keys, ADMIN_PASSWORD, APP_SECRET, BIND_ADDR, HOST_PORT
├─ pyproject.toml · uv.lock · .python-version   # uv-managed deps, pinned Python 3.12
├─ LICENSE · README.md · CONTRIBUTING.md · CLA.md
├─ data/                   # gitignored; praeceptor.db lives here
├─ scripts/                # operational CLI tools (backup.py — hot DB backup/restore)
├─ update.sh               # one-command deploy update: pull → build → cold volume snapshot → up → health wait
├─ app/
│  ├─ main.py              # FastAPI app, middleware, router mounts, /healthz, lifespan
│  ├─ config.py            # env + model defaults + validate_runtime boot guard
│  ├─ db.py                # sqlite3 connection (WAL), schema init/migrate, user_version
│  ├─ models.py            # query helpers (raw SQL, column allowlists)
│  ├─ security.py          # argon2 hashing (student PINs)
│  ├─ auth.py              # separate admin + student sessions, lockouts
│  ├─ ages.py              # derive student age from birth_year/birth_month
│  ├─ clock.py             # app-wide display/cap timezone (admin-set; caps + timestamp render)
│  ├─ pipeline.py          # caps → gate → tutor → log (prepare/classify_turn/finalize)
│  ├─ prompts.py           # the two prompt templates + assembly (gate + tutor)
│  ├─ model_client.py      # LiteLLM wrapper (gate, tutor, streaming, agentic tools loop)
│  ├─ render.py            # tutor markdown → sanitized HTML (+ inline/figure SVG)
│  ├─ tutor_runtime.py     # picks plain vs tools path; provides the tool executor
│  ├─ tools/               # tool registry (python, verify)
│  ├─ sandbox/             # separate-process/-container sandbox (security model in __init__.py)
│  ├─ subject_presets.py   # K–12 subject preset library
│  ├─ seed.py              # DEFAULT_PIN only — first run starts EMPTY (no auto-seed)
│  ├─ templating.py        # Jinja env, filters (tutor_md/localdt/age_of), globals
│  └─ routers/{student.py, admin.py}  # + templates/{student/, admin/}
└─ static/                 # CSS, app.js, theme.js, vendored htmx.min.js + katex/
```

## Dev commands

```bash
uv sync                                        # install/lock deps
cp .env.example .env                           # then fill ANTHROPIC_API_KEY, ADMIN_PASSWORD, APP_SECRET
uv run uvicorn app.main:app --reload           # run; check http://127.0.0.1:8000/healthz
```

- If `uv` isn't on PATH in a fresh shell, add its install directory (see the
  [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/)).
- Throwaway script that imports `app.*`: set `PYTHONPATH` to the repo root and point
  `DB_PATH` at a temp file (e.g. `$env:DB_PATH="$env:TEMP\x.db"`) so it doesn't touch
  real data. Keep such scripts in a temp directory outside the repo.
- **Secrets live in `.env`** (gitignored). Never commit `.env` or `data/`.

## Conventions

- **All inference goes through [app/model_client.py](app/model_client.py)** (LiteLLM)
  — the rest of the app is provider-agnostic. `run_gate` (strict JSON), `run_tutor` /
  `run_tutor_stream`, and `run_tutor_tools_stream` (the agentic tool-calling loop).
  model_client stays unaware of the sandbox/tools — [app/tutor_runtime.py](app/tutor_runtime.py)
  picks the plain vs tools path and injects the executor.
- **Gate fails closed:** an unclassifiable message withholds the tutor, never falls
  through. Both prompts are assembled from DB rows at request time.
- **Sandbox is the tool security boundary, never in-process.** Tool code (model-shaped →
  untrusted) runs in a separate process with a scrubbed env (no secrets), rlimits, and a
  swappable OS-level `SANDBOX_WRAPPER`; do NOT make in-process `exec`/restricted-builtins
  the boundary. Tools are gated by global `TOOLS_ENABLED` AND per-subject
  `subjects.tools_enabled`. Read [app/sandbox/__init__.py](app/sandbox/__init__.py) before
  touching anything there. A turn must always end with a `done` or `notice` event (never
  a dropped stream).
- **Auth boundary is the main security control:** admin password = `ADMIN_PASSWORD`
  env (no admin table); student PINs are argon2-hashed in `students.pin_hash`. Keep
  admin and student sessions separate.
- **Models** (editable per subject in admin): gate `anthropic/claude-haiku-4-5`,
  tutor `anthropic/claude-sonnet-5` (default for all subjects). Do **not** send
  `temperature` on tutor calls (some Claude models reject it; `litellm.drop_params=True`).
- Verify changes against a throwaway DB before wiring to the UI.
