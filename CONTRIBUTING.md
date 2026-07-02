# Contributing to Praeceptor

Thanks for your interest in Praeceptor! It's a self-hosted homeschool tutoring
harness, and contributions — bug reports, fixes, features, docs — are welcome.

Please read this document before opening a pull request. The most important part is
the **Contributor License Agreement** below: we can't merge non-trivial code
contributions until it's signed.

---

## Contributor License Agreement (required)

**Praeceptor is maintained by StudyLamp Software LLC, which holds copyright in the
project's original code.**
Before your first code contribution can be merged, you'll be asked to sign our
**Contributor License Agreement (CLA)** — see [`CLA.md`](CLA.md).

In short, the CLA:

- lets you **keep the copyright** to your own contribution,
- grants StudyLamp Software LLC a broad license to use and **relicense** that
  contribution — including under future commercial or dual-license terms, and
- in return, conditions that relicensing right on your contribution **also being
  licensed under the project's open-source license** that was in effect when you
  submitted it (MIT today) (CLA §2.3).

**Why we ask for this:** it keeps the project's licensing in one pair of hands.
Contributors own their own patches either way; without the CLA, the project could
never be relicensed or offered commercially without tracking down and getting sign-off
from every past contributor — a well-known trap that permanently freezes many open
projects. The CLA collects that permission once, up front, so Praeceptor stays free to
evolve.

**How to sign:** when you open your first pull request, the **CLA Assistant** bot will
post a link; signing is a one-click GitHub authorization and takes a minute. The bot
records it, so you only sign once. If you're contributing on behalf of an employer,
make sure you have the authority to do so (your employer may own your work by default) —
and if your employer claims rights in the work, **contact us before submitting**, since
an individual signature may not be enough.

Maintainers may, at their discretion, accept changes too small to be copyrightable — a
typo fix, a one-line config tweak — without a signature, but the bot will still ask, and
signing is always the fastest path. When in doubt, sign it.

---

## Ways to contribute

- **Bug reports & feature ideas:** open a GitHub Issue. Include what you expected, what
  happened, your environment, and steps to reproduce. For the tutor/gate behavior,
  include the subject config and (redacted) message that triggered it — never paste real
  student data, API keys, or `.env` contents.
- **Code & docs:** open a pull request (see below). For anything non-trivial, please
  open an issue first so we can agree on the approach before you invest time.

**Security issues:** please do **not** file a public issue for a vulnerability. Report it
privately to bstaggs@studylamp.com so it can be fixed before disclosure. Praeceptor
runs model-generated code in a sandbox and handles children's conversations, so security
reports are taken seriously.

---

## Development setup

Praeceptor is Python + FastAPI, server-rendered Jinja2 + HTMX, SQLite, with dependencies
managed by [uv](https://github.com/astral-sh/uv).

```bash
# 1. Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync                                   # install/lock deps

# 2. Configure secrets (never commit this file)
cp .env.example .env                      # set ANTHROPIC_API_KEY, ADMIN_PASSWORD, APP_SECRET

# 3. Run
uv run uvicorn app.main:app --reload      # http://127.0.0.1:8000/healthz
```

Project-wide conventions and the module map live in [`CLAUDE.md`](CLAUDE.md) — **read it
first.** The tool-sandbox security model is documented at the top of
[`app/sandbox/__init__.py`](app/sandbox/__init__.py).

---

## Conventions & expectations

Please match the surrounding code and these project rules:

- **All model inference goes through [`app/model_client.py`](app/model_client.py)** (the
  LiteLLM wrapper). The rest of the app is provider-agnostic — don't call a provider SDK
  directly, and keep model strings as `provider/model`.
- **The gate fails closed.** An unclassifiable student message must withhold the tutor,
  never fall through to it.
- **The sandbox is the security boundary for tool code.** Model-generated tool code runs
  in a **separate process** with a scrubbed environment, resource limits, and a swappable
  OS-level wrapper — never make in-process `exec`/restricted-builtins the boundary. Read
  [`app/sandbox/__init__.py`](app/sandbox/__init__.py) before touching anything there.
- **Auth boundary is the main in-app security control.** Keep admin and student sessions
  hard-separated; student PINs are argon2-hashed; the admin password comes from env.
- **Never commit secrets or data.** `.env` and `data/` are gitignored — keep it that way.
  Don't hard-code keys, and don't send `temperature` on tutor calls.
- **Untrusted text stays escaped.** Student/tutor/curriculum text is autoescaped (or run
  through the sanitizing render path) and never marked `| safe`.
- **Verify before you propose.** Test changes against a throwaway SQLite DB (point
  `DB_PATH` at a temp file) rather than your real data. The
  project leans on offline `TestClient` checks; describe how you tested in your PR.

---

## Pull request process

1. **Branch** from `main` (or the current working branch if noted) for your change.
2. Keep PRs **focused** — one logical change per PR is far easier to review.
3. **Sign the CLA** when the bot prompts you (first PR only).
4. In the PR description, explain **what changed and why**, and **how you verified it**
   (what you ran, what you observed). Note any change to the data model, prompts, or the
   sandbox explicitly — those get extra scrutiny.
5. Update [`CLAUDE.md`](CLAUDE.md) (or the relevant inline docs/comments) in the same PR
   if your change alters a documented convention, the module map, or a security-relevant
   design.
6. Be responsive to review feedback. Maintainers may request changes, especially on
   security-sensitive paths (sandbox, auth, prompts, rendering).

---

## License

Praeceptor is released under the [MIT License](LICENSE), Copyright © 2026 StudyLamp
Software LLC. Code contributions are accepted under the [CLA](CLA.md) described above:
you license your contribution to StudyLamp Software LLC, which licenses it to everyone
else under the project's MIT license (CLA §2.3).
