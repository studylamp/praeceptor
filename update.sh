#!/usr/bin/env bash
# update.sh — one-command deployment update for Praeceptor. Run on the Docker host
# from the repo checkout: ./update.sh [--no-pull]
#
# What it does, in order:
#   1. git pull --ff-only          (skipped with --no-pull; refuses if tracked files
#                                   have local edits, so a pull can't eat your changes)
#   2. docker compose build        (the OLD app keeps serving while the new image
#                                   builds — a failed build changes nothing)
#   3. docker compose stop praeceptor
#   4. cold-snapshot the data volume -> .backups/praeceptor-volume-<ts>.tgz
#      (the stop is what makes the raw -db/-wal/-shm copy consistent; see README
#      "Backups". Keeps the KEEP newest snapshots, pruning older ones.)
#   5. docker compose up -d        (recreates app + sandbox on the new image)
#   6. wait for a healthy /healthz and report the sandbox preflight
#
# If a step fails after the app is stopped but before `docker compose up -d` begins
# (e.g. the snapshot itself), the stopped container is started again on exit, so a
# failed backup doesn't leave the app down. Once `up -d` has begun, the old container
# may already be replaced — recovery is then best-effort, and a deployment that comes
# up unhealthy is left running for inspection; restore from the fresh snapshot if you
# need to roll data back.
#
# This updates an EXISTING deployment. First install: see README "Quick start".

set -euo pipefail

KEEP=5   # volume snapshots to retain in .backups/ (matches scripts/backup.py's default)

# CDPATH= : an inherited CDPATH could otherwise hijack a relative `cd` (and echo the path).
CDPATH= cd -- "$(dirname -- "$0")"

NO_PULL=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) NO_PULL=1 ;;
    *) echo "usage: $0 [--no-pull]" >&2; exit 2 ;;
  esac
done

[[ -f docker-compose.yml && -f .env ]] || {
  echo "error: run this from a configured Praeceptor checkout (docker-compose.yml and .env present)" >&2
  exit 1
}

# Fail loud if docker itself is unusable (daemon down, no permission, not installed)
# BEFORE the suppressed inspect below — those errors must not masquerade as "not deployed".
docker version >/dev/null

# Resolve the data volume from the existing container rather than guessing the
# compose project name (COMPOSE_PROJECT_NAME or the directory name could change it).
# Works on a stopped container too; failing here (nothing deployed yet) aborts
# before anything is touched.
VOLUME="$(docker inspect --type container praeceptor --format \
  '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
if [[ -z "$VOLUME" ]]; then
  echo "error: no existing 'praeceptor' container — this script updates a deployment." >&2
  echo "For a first install, follow the README quick start (docker compose up -d --build)." >&2
  exit 1
fi

# The inspect above matched by GLOBAL container name, but the `stop`/`up` below are
# PROJECT-scoped. If the deployment was created under a different compose project
# (renamed directory, or COMPOSE_PROJECT_NAME/-p at first deploy), `stop` would
# silently no-op and the tar would copy a LIVE database — refuse instead.
if [[ "$(docker compose ps -aq praeceptor)" != "$(docker inspect -f '{{.Id}}' --type container praeceptor)" ]]; then
  echo "error: the 'praeceptor' container belongs to a different compose project than" >&2
  echo "this checkout (directory renamed, or COMPOSE_PROJECT_NAME/-p used at deploy?)." >&2
  echo "Re-run from the project that deployed it, or redeploy under this one." >&2
  exit 1
fi

# --- 1. Pull ----------------------------------------------------------------
if (( ! NO_PULL )); then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "error: tracked files have local edits — commit/stash them, or rerun with --no-pull:" >&2
    git status --short --untracked-files=no >&2
    exit 1
  fi
  echo "==> git pull --ff-only"
  git pull --ff-only
fi

APP_VERSION="$(git describe --tags --always --dirty)"
export APP_VERSION
echo "==> updating deployment to ${APP_VERSION}"

# --- 2. Build the new image while the old app is still serving ---------------
# Both services tag the same praeceptor:latest, so building one serves both.
echo "==> docker compose build praeceptor"
docker compose build praeceptor

# Have the tar helper image cached BEFORE the downtime window — a cold cache would
# otherwise pull from Docker Hub while the app is stopped.
docker image inspect alpine >/dev/null 2>&1 || docker pull alpine

# From here on, a failure while the app is stopped brings it back up on the way
# out: `start` revives the old container (it exists until `up` replaces it); the
# `up -d` fallback covers a failure mid-`up`, best-effort.
APP_STOPPED=0
cleanup() {
  local rc=$?
  if (( rc != 0 && APP_STOPPED )); then
    echo "!! update failed while the app was stopped — bringing the app back up" >&2
    docker compose start praeceptor || docker compose up -d praeceptor || true
  fi
}
trap cleanup EXIT

# --- 3-4. Stop the app and snapshot the volume -------------------------------
BACKUP_DIR="${PWD}/.backups"
# UTC so the names' lexicographic order stays chronological even across a DST fall-back.
SNAPSHOT="praeceptor-volume-$(date -u +%Y%m%d-%H%M%S).tgz"
mkdir -p "$BACKUP_DIR"

echo "==> stopping the app for a consistent volume snapshot"
# Set BEFORE the stop: if `stop` itself dies mid-flight the trap must still fire.
# (`start` on a container that never actually stopped is a harmless no-op.)
APP_STOPPED=1
docker compose stop praeceptor
# Belt and braces for the one invariant that matters here: never tar a live database.
if [[ "$(docker inspect -f '{{.State.Running}}' --type container praeceptor)" != "false" ]]; then
  echo "error: 'praeceptor' is still running after 'docker compose stop' — refusing to snapshot" >&2
  exit 1
fi

echo "==> snapshotting volume '${VOLUME}' -> .backups/${SNAPSHOT}"
docker run --rm -v "${VOLUME}:/d:ro" -v "${BACKUP_DIR}:/b" alpine \
  tar czf "/b/${SNAPSHOT}" -C /d .

# Prune to the KEEP newest. Deletion is deliberately strict, like scripts/backup.py's:
# only names of the EXACT shape this script writes (praeceptor-volume-YYYYMMDD-HHMMSS.tgz)
# are eligible — never backup.py's hot snapshots or a hand-named …-volume-final.tgz —
# and the snapshot just written is protected outright, so a backward clock step can't
# evict it. UTC timestamps make the glob's lexicographic order oldest-first.
shopt -s nullglob
snaps=()
for f in "$BACKUP_DIR"/praeceptor-volume-*.tgz; do
  [[ "$(basename "$f")" =~ ^praeceptor-volume-[0-9]{8}-[0-9]{6}\.tgz$ ]] && snaps+=( "$f" )
done
if (( ${#snaps[@]} > KEEP )); then
  for old in "${snaps[@]:0:${#snaps[@]}-KEEP}"; do
    [[ "$old" == "${BACKUP_DIR}/${SNAPSHOT}" ]] && continue
    echo "    pruning old snapshot $(basename "$old")"
    rm -f -- "$old"
  done
fi

# --- 5. Bring everything up on the new image ---------------------------------
echo "==> docker compose up -d"
docker compose up -d
APP_STOPPED=0   # past the danger window — from here, leave the new deployment up

# --- 6. Verify ----------------------------------------------------------------
# The container's own healthcheck checks /healthz (with the image's Python); poll its
# verdict instead of reading BIND_ADDR/HOST_PORT out of .env. start_period + interval
# mean healthy typically lands within ~a minute; a definitive `unhealthy` (3 failed
# probes) fails fast instead of waiting out the deadline.
echo "==> waiting for the app to report healthy"
DEADLINE=$(( SECONDS + 120 ))
while :; do
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' praeceptor 2>/dev/null || echo unknown)"
  [[ "$STATUS" == "healthy" ]] && break
  if [[ "$STATUS" == "unhealthy" ]] || (( SECONDS >= DEADLINE )); then
    echo "error: app did not become healthy (status: ${STATUS}); recent logs:" >&2
    docker compose logs --tail 30 praeceptor >&2
    exit 1
  fi
  sleep 3
done

# The preflight line goes to the app's STDERR (as do uvicorn logs), and compose `logs`
# preserves the container's stream separation — so merge with 2>&1, don't discard it.
PREFLIGHT="$(docker compose logs --no-log-prefix praeceptor 2>&1 | grep -i 'sandbox preflight' | tail -n 1 || true)"
if [[ "$PREFLIGHT" == *"OK"* ]]; then
  echo "    ${PREFLIGHT}"
elif [[ -n "$PREFLIGHT" ]]; then
  echo "warning: sandbox preflight did not report OK:" >&2
  echo "    ${PREFLIGHT}" >&2
else
  echo "    (no sandbox preflight line yet — tools disabled, or re-check with:"
  echo "     docker compose logs | grep -i 'sandbox preflight')"
fi

echo "==> deployed /healthz:"
# Informational only — health was already verified above, so a transient exec hiccup
# must not turn a successful update into a nonzero exit.
docker compose exec -T praeceptor python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode())" \
  || echo "warning: could not query /healthz directly (container already verified healthy)" >&2

echo "==> update complete: ${APP_VERSION} (snapshot: .backups/${SNAPSHOT})"
