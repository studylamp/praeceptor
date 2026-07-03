# Praeceptor

**A self-hosted, parent-controlled AI tutor for homeschooling families.**

Each child logs in, picks one of *their* enrolled subjects, and chats with a
Claude-powered tutor that stays on that subject. A parent admin console configures
everything and can read every conversation. A classifier **gate** blocks off-topic
chat before the tutor is ever called, and per-student daily caps keep usage (and
cost) bounded. For math and science, the tutor can run **sandboxed Python** to
compute and verify answers exactly and draw real vector-graphics plots — instead of
doing the math in its head.

Runs on your own hardware, on your LAN, in Docker. Your keys, your data, your rules.

<!-- Screenshot placeholder: student chat view (subject picker + a math conversation with a rendered plot) -->
<!-- ![Student chat](docs/img/student-chat.png) -->

---

## Why Praeceptor?

Off-the-shelf AI chatbots weren't built for handing a kid unsupervised access. General
assistants wander off-topic, have no per-child scoping, no spend limits, and no way for
a parent to see what was actually said. Praeceptor is a thin, purpose-built harness
around a frontier model that adds exactly the controls a homeschooling parent needs:

- **Subject rails** — the tutor only engages with what the child is enrolled in, and
  each subject's **gate scope** dials how tightly (a Grade-5 arithmetic scope blocks
  algebra; a "any math" scope allows it).
- **Tutor, not answer key** — a per-subject **answer policy** and **tutoring style** let
  you decide how the tutor helps. Configure it to **walk a student through problem
  solving** rather than hand over finished answers, to coach writing instead of
  producing fully formed paragraphs and essays, or to be more direct where that fits the
  subject.
- **Parent oversight** — every message, including blocked attempts, is logged and reviewable.
- **Bounded cost** — hard daily caps on messages and tokens, per child.
- **Trustworthy math** — deterministic computation instead of model guesswork.
- **Your infrastructure** — LAN-only; the app sends your kids' chats to no one but the
  model provider you configure (frontend assets are vendored — nothing loads from a CDN).

---

## Features

### For the student
- **PIN login** — no email, no account, just a name and a PIN.
- **Subject picker** — only shows the subjects the parent enrolled them in.
- **Focused chat** — a Socratic-by-default tutor scoped to the chosen subject, with
  streaming replies and a "thinking…" indicator.
- **Rich rendering** — Markdown, real math typesetting (KaTeX), inline diagrams, and
  plotted function graphs.

### For the parent (admin console)
- **Students & PINs** — create children, set/reset argon2-hashed PINs, forced change
  off the default PIN.
- **Subjects** — per-child subjects with a **30-subject K–12 preset library** (math,
  science, English, history, creative writing, and more) to start from, plus fully
  editable name, grade, scope, tutoring style, and answer policy.
- **Curriculum context** — a fast-path editor to keep the tutor aligned with what the
  child is currently studying, without wading through the full subject form.
- **Educational framing** — optional parent-authored guidance (global and per-subject)
  that shapes how the tutor presents contested or worldview-sensitive material. You are
  the curriculum authority; the app's own built-in rules are aimed only at subject scope,
  age-appropriateness, and basic safety.
- **Daily caps** — per-child message and token limits (nullable = unlimited), with a
  one-click reset of today's usage from the student page when a capped child needs
  more time.
- **Full transcript review** — read any conversation, including off-topic attempts the
  gate blocked.
- **Built-in chat test** — an admin sandbox that runs the *full* student pipeline
  (gate → tutor) against any subject, with a per-turn debug panel (gate verdict, token
  counts, cache hits, tool rounds).

### Under the hood
- **The gate** — a fast classifier (Haiku by default) runs before the tutor on every
  message and labels it `on_subject`, `other_subject`, or `off_topic`. It **fails
  closed**: an unclassifiable message withholds the tutor rather than falling through.
  Off-topic attempts are refused politely and logged.
- **Computation tools (per subject; on by default)** — for math/science, the tutor can:
  - run **sandboxed Python** (sympy / numpy) to compute answers exactly,
  - draw **real matplotlib plots** rendered as crisp vector SVG, and
  - **verify** its own answers via a server-built symbolic check.

  Tool code is model-generated and therefore untrusted, so in Docker it runs in a
  **separate, network-isolated container** — no network, no secrets, no database — with
  the code itself still in a scrubbed-environment, resource-limited subprocess inside it.
  Docker is the boundary and the main app keeps its own guardrails. (Outside Docker it
  falls back to a local hardened subprocess with an optional OS wrapper.) The sandbox —
  not in-process restriction — is the security boundary.
- **Prompt caching** — the stable prompt prefix is cached, so multi-turn conversations
  are billed at a fraction of the naive input cost, and daily token caps track real
  dollar cost.
- **Provider-flexible** — all inference goes through [LiteLLM](https://github.com/BerriAI/litellm),
  so models are `provider/model` strings you set per subject. Anthropic by default;
  OpenAI, Gemini, Bedrock, Vertex, or local models work too.

---

## Architecture at a glance

```
Student message
   │
   ▼
enforce daily caps  →  gate (classifier)  →  branch
                                              ├─ on_subject     → tutor (per-subject model) → stream reply
                                              ├─ other_subject  → offer to switch subjects
                                              └─ off_topic      → friendly refusal (logged as a blocked attempt)
   │
   ▼
log everything (both prompts assembled from the DB at request time)
```

- **Backend:** Python + FastAPI, server-rendered Jinja2 + HTMX (no SPA, no build step).
- **Storage:** SQLite (single WAL file on a Docker-managed named volume) via plain `sqlite3`.
- **Model layer:** LiteLLM gateway behind `app/model_client.py`; the rest of the app is
  provider-agnostic.
- **Sandbox:** the tutor's computation tools run in a separate, network-isolated container
  (a local hardened subprocess outside Docker); see [`app/sandbox/`](app/sandbox/).
- **Auth:** hard-separated admin and student sessions; admin password from env (no admin
  table), student PINs argon2-hashed.

A full file/module map lives in [`CLAUDE.md`](CLAUDE.md#structure).

---

## Tech stack

| Layer        | Choice                                                                 |
|--------------|------------------------------------------------------------------------|
| Web          | FastAPI · Jinja2 · HTMX (vendored, no CDN)                              |
| Data         | SQLite (`sqlite3`, WAL mode)                                            |
| Models       | LiteLLM gateway — provider-flexible (`provider/model` strings)         |
| Computation  | sympy · numpy · matplotlib, in an isolated sandbox container           |
| Rendering    | markdown-it-py + nh3 sanitizer · KaTeX (vendored)                      |
| Packaging    | [uv](https://github.com/astral-sh/uv) · Docker                         |
| License      | MIT                                                                     |

---

## Local development

```bash
# 1. Install uv (one-time): https://docs.astral.sh/uv/getting-started/installation/

# 2. Install dependencies
uv sync

# 3. Configure secrets
cp .env.example .env      # then edit: set ANTHROPIC_API_KEY, ADMIN_PASSWORD, APP_SECRET

# 4. Run
uv run uvicorn app.main:app --reload
```

Then:
- Health check: <http://127.0.0.1:8000/healthz>
- Student app: <http://127.0.0.1:8000/>
- Admin console: <http://127.0.0.1:8000/admin>

On first run the database starts **empty** — nothing is auto-seeded. Create your students
and their subjects in the admin console; the dashboard warns about any student whose PIN is
left at the weak default `1234`. Everything — students, subjects, models, caps, framing —
is editable in admin with no redeploy.

> **Notes:** secrets live only in `.env` (gitignored); `data/` (the SQLite DB) is never
> committed. The boot guard refuses to start with a weak `APP_SECRET` or a default
> `ADMIN_PASSWORD`, so set real values before running.

---

## Deployment

Praeceptor is a single Docker image run with `docker compose` — SQLite on a Docker-managed
named volume, secrets via `env_file`, `restart: unless-stopped`.

### Quick start (Mode 1 — direct on your LAN)

On the server (needs Docker + the Compose plugin), from a clone of this repo:

```bash
# 1. Configure. Copy the template and fill it in.
cp .env.example .env
#    Edit .env and set at minimum:
#      ANTHROPIC_API_KEY   your provider key
#      ADMIN_PASSWORD      the parent/admin console password (not the default)
#      APP_SECRET          a long random string:
#                            python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#      BIND_ADDR           your server's LAN IP (see it with: hostname -I)
#      HOST_PORT           only if 8000 is already taken on this host (e.g. 8010)
#    Leave TOOLS_ENABLED=true — compute tools run in a separate, network-isolated
#    `sandbox` container that compose starts for you (no host tweaks needed).

# 2. Build and start. (The DB lives on a Docker-managed named volume — no chown needed.)
#    Passing APP_VERSION stamps the running build (git tag + commit) into the admin footer
#    and /healthz; it's optional (defaults to "dev") but handy for support/"am I up to date".
APP_VERSION=$(git describe --tags --always --dirty) docker compose up -d --build

# 3. Verify it's healthy, then browse to it (use your HOST_PORT if you changed it).
curl -fsS http://<server-lan-ip>:8000/healthz     # {"status":"ok","version":"…",...}
#    Student app:   http://<server-lan-ip>:8000/
#    Admin console: http://<server-lan-ip>:8000/admin

# 4. Confirm the compute-tool sandbox came up (see "The compute-tool sandbox" below).
docker compose logs | grep -i "sandbox preflight"   # want: "sandbox preflight OK"
```

A fresh install starts **empty** — no students or subjects. Log into the admin console and
create your children and their subjects; everything (students, subjects, models, caps,
framing) is configured there, no redeploy needed. Set a real PIN for each student — the
dashboard warns about any student left on the weak default `1234`.

Updating: `git pull && APP_VERSION=$(git describe --tags --always --dirty) docker compose up -d --build`
— your data persists on the named volume across rebuilds. (Dropping the `APP_VERSION=…`
prefix still works; the footer/`/healthz` version just shows `dev`.)

### The two deployment modes

It's designed to be stood up two ways, both LAN-only:

- **Directly on your LAN by IP** — publish the container port to your server's LAN
  address and browse to `http://<server-ip>:8000`. No reverse proxy required.
- **Behind a reverse proxy** (e.g. [Nginx Proxy Manager](https://nginxproxymanager.com/))
  for HTTPS / Let's Encrypt and a friendly hostname on your local network. Copy
  `docker-compose.override.yml.example` to `docker-compose.override.yml` (it's gitignored and
  Compose merges it automatically) and set your proxy's Docker network name there — no need to
  hand-edit `docker-compose.yml`. Point a proxy host at `http://praeceptor:8000`, set
  `SESSION_HTTPS_ONLY=true` in `.env`, and raise the proxy's read timeout to ~300s so a slow
  first token on a long turn survives. To serve *only* through the proxy, also comment out the
  `ports:` block in `docker-compose.yml`. To get a publicly-trusted certificate without opening
  an inbound port, use a **DNS-01** challenge.

Your data persists on the named volume across image rebuilds, and everything —
students, subjects, models, caps — is configured in the admin console, not in the image.

### Backups

The database is a Docker-managed named volume (`praeceptor_data` — the name is
`<compose-project>_data`, usually `praeceptor_data`; `docker volume ls` confirms, and
`docker volume inspect praeceptor_data` shows its host path). Snapshot it with the app
stopped, so SQLite's WAL is flushed for a consistent copy:

```bash
# Back up:
docker compose stop praeceptor
docker run --rm -v praeceptor_data:/d -v "$PWD":/b alpine tar czf /b/praeceptor-db.tgz -C /d .
docker compose start praeceptor

# Restore (overwrites the volume's contents):
docker compose stop praeceptor
docker run --rm -v praeceptor_data:/d -v "$PWD":/b alpine tar xzf /b/praeceptor-db.tgz -C /d
docker compose start praeceptor
```

Drop the first command in a cron job to keep periodic snapshots.

### The compute-tool sandbox

The tutor's math/plot tools run **model-generated (therefore untrusted) code**, so it runs
in a **separate `sandbox` container** that `docker compose` starts alongside the app. That
container has **`network_mode: none`** (zero network — no route to your LAN or the
internet), **no secrets and no database mount** (it literally can't read them), and runs
**non-root**. The app hands code to the worker over a Unix-domain socket on a private
volume and gets back results (values, plots as SVG). Because Docker itself provides the
isolation, there are **no host changes, no `sysctl`, and no `security_opt`** — and the main
app keeps its own default seccomp/apparmor guardrails. It just works with
`docker compose up -d`.

**Confirm it's healthy** — both containers should be up, and the app runs a **preflight**
on boot:
```bash
docker compose ps                                    # praeceptor + praeceptor-sandbox, both healthy
docker compose logs | grep -i "sandbox preflight"    # → "sandbox preflight OK"
# or check directly:
docker compose exec praeceptor python -c "from app.sandbox import preflight; print(preflight())"
```
The admin dashboard also shows a banner (with a live **Re-check** button) if the sandbox
ever isn't reachable. Don't want the tools? Set `TOOLS_ENABLED=false` and the worker is
never contacted (you can also drop the `sandbox` service from compose).

**Running outside Docker** (bare `uvicorn` on a dev/LAN box) has no worker container: leave
`SANDBOX_SERVER` unset and tool code runs in a local `runner.py` subprocess. That's fine on
a **trusted loopback/LAN** box (you'll see a one-line warning); otherwise set a local
`SANDBOX_WRAPPER` (nsjail/firejail/bwrap) or `TOOLS_ENABLED=false`. The app **refuses to
boot** with tools enabled on a non-loopback bind and neither a worker nor a wrapper
configured. See [`app/sandbox/__init__.py`](app/sandbox/__init__.py)
for the full security model.

---

## Troubleshooting

**First stop: the admin Chat test.** `/admin` → **Chat test** runs the *full* student
pipeline (gate → tutor → tools) against any subject and shows a per-turn **debug panel** —
the gate verdict and reason, token counts, and tool rounds. It's the fastest way to see
*why* a turn behaved the way it did, and it doesn't touch a student's real conversation or
their caps. If a reply comes back as an error notice, expand that panel: the `gate_reason`
shows the actual underlying error (e.g. `gate model call failed (… invalid x-api-key …)`).

| Symptom | Likely cause & where to look |
|---|---|
| Student sees *"something went wrong on my end… ask your parent"* | The gate/tutor **model call is failing**. Most often a bad `ANTHROPIC_API_KEY` (`invalid x-api-key` = wrong / expired / truncated key, or a stray quote/space in `.env`). Check the Chat-test debug panel, and `docker compose logs praeceptor \| grep "gate model call failed"`. Also verify the model string is one your account can reach. |
| Student sees *"I had trouble understanding that…"* repeatedly | The gate reached the model but couldn't classify. Usually a too-narrow subject **gate scope**, or genuinely off-topic input. Check the verdict/reason in the debug panel. |
| Math/plots don't run; no tool rounds | The **sandbox worker**. `docker compose ps` (is `praeceptor-sandbox` healthy?), `docker compose logs \| grep -i "sandbox preflight"`, and the admin dashboard's sandbox banner (with its **Re-check** button). |
| Container won't start: *"port is already allocated"* | Another service holds the host port — set `HOST_PORT` in `.env` to a free one and `docker compose up -d`. |
| *"Praeceptor refuses to start…"* on boot | The boot guard: fix `APP_SECRET` (≥32 random chars) / `ADMIN_PASSWORD` (not the default), or configure sandbox isolation (the `sandbox` container's `SANDBOX_SERVER`, or a `SANDBOX_WRAPPER`) if tools are on with a non-loopback bind. |
| Admin console blank / "No students yet" | Expected on a fresh install — it starts empty. Create students and subjects in admin. |

---

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture, module map, conventions, and working
  agreements. **Start here to understand the project.**
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute, plus the CLA.
- In-code: [`app/sandbox/__init__.py`](app/sandbox/__init__.py) documents the
  tool-sandbox security model.

---

## Important notes for parents

Praeceptor puts guardrails around a large language model; it does not change what a
language model fundamentally is. Please deploy it with realistic expectations:

- **AI tutors make mistakes.** The gate, answer policies, and computation tools reduce —
  but cannot eliminate — wrong, confusing, or unexpected responses. Praeceptor is a
  supervision *aid*, not a substitute for it: review your children's transcripts (the
  admin console keeps every conversation, including blocked attempts) and stay involved.
- **You run it; you're responsible for your deployment.** This is self-hosted software
  provided **"as is"**, without warranty of any kind, under the [MIT License](LICENSE).
  That responsibility includes all charges your model-provider API key incurs — the
  daily caps are enforced by this app, not by your provider.
- **Your model provider's terms apply to the chats.** Conversations go to whatever
  provider you configure and are handled under that provider's terms of service, privacy
  policy, and usage policies — including any requirements they set for use by minors.
  Review them before handing the app to a child.
- **No affiliation.** Praeceptor is an independent project — not affiliated with,
  endorsed by, or sponsored by Anthropic or any other model provider. Provider and model
  names are used only to identify compatible services.

---

## Author

Created by **Brandon Staggs**.

## License

MIT — see [`LICENSE`](LICENSE). Copyright © 2026 StudyLamp Software LLC.
