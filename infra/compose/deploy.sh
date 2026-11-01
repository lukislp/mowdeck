#!/usr/bin/env bash
# Updates the Compose stack to a release: the documented procedure of docs/ops/compose.md in one
# script, with the deploy maintenance window around the restart (ADR 0055).
#
#   ./deploy.sh [--dry-run] [--window-minutes N] [--no-maintenance] <version>
#
# Steps (each failure stops the script before anything later runs):
#   1. git fetch --tags
#   2. backup into ${MOWDECK_BACKUP_DIR:-~/mowdeck-backups}: a copy of .env and a gzip'd
#      pg_dumpall, whose last lines must say "PostgreSQL database cluster dump complete"
#   3. git checkout --detach v<version>  (refuses a checkout with local changes)
#   4. ./verify-release.sh <version>      (fails closed: nothing is pulled when it fails), then
#      ./nats-passwords.sh --check .env   (the release's NATS passwords, ADR 0074)
#   5. MOWDECK_VERSION=<version> in .env
#   6. docker compose pull
#   7. open the maintenance window in Notifications, so the restart of the Device-Gateway sends
#      no "mower offline / back online" pair (best effort: the first deploy of a build that
#      already has the window runs the old notifications image, which has no window; the script
#      then warns and goes on)
#   8. docker compose up -d --no-build --wait
#   9. close the window (also when step 8 fails: a real outage must be reported), then print the
#      state of every service and fail when one is not running and healthy.
#
# --dry-run prints the steps and runs nothing. --window-minutes sets the length of the window
# (default 15; it expires by itself, so a forgotten window cannot silence alerts). --no-maintenance
# skips steps 7 and 9. A stack under another project name: COMPOSE_PROJECT_NAME=<name> ./deploy.sh.
#
# Secrets: the script never prints .env or any value of it; it only prints the file names.
set -euo pipefail

# The whole script is one function that runs after bash has read it completely: the checkout in
# step 3 replaces this very file with the one of the release.
opened=0 # global: the EXIT trap reads it

main() {
  local here dry_run=0 maintenance=1 minutes=15 version=""
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here"

  usage() {
    echo "usage: $0 [--dry-run] [--window-minutes N] [--no-maintenance] <version>" >&2
    exit 2
  }
  die() {
    echo "deploy: $*" >&2
    exit 1
  }
  step() { echo "deploy: $*"; }

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) dry_run=1 ;;
      --no-maintenance) maintenance=0 ;;
      --window-minutes)
        [ "$#" -ge 2 ] || usage
        minutes="$2"
        shift
        ;;
      -h | --help) usage ;;
      -*) usage ;;
      *)
        [ -z "$version" ] || usage
        version="$1"
        ;;
    esac
    shift
  done
  [ -n "$version" ] || usage
  version="${version#v}"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+.][0-9A-Za-z.-]+)?$ ]] ||
    die "not a version: $version (expected 1.2.3)"
  if ! [[ "$minutes" =~ ^[0-9]+$ ]] || [ "$minutes" -lt 1 ] || [ "$minutes" -gt 60 ]; then
    die "--window-minutes must be 1..60"
  fi

  local files=(-f docker-compose.yml -f docker-compose.release.yml)
  local backup_dir="${MOWDECK_BACKUP_DIR:-$HOME/mowdeck-backups}"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local env_copy="$backup_dir/env-before-$version-$stamp.env"
  local dump="$backup_dir/mowdeck-before-$version-$stamp.sql.gz"

  run() {
    if [ "$dry_run" -eq 1 ]; then
      echo "deploy: [dry-run] $*"
    else
      "$@"
    fi
  }

  [ -f .env ] || die ".env not found in $here"

  step "1/9 fetch tags"
  run git fetch --tags

  step "2/9 backup to $backup_dir"
  if [ "$dry_run" -eq 1 ]; then
    echo "deploy: [dry-run] cp .env $env_copy; pg_dumpall | gzip > $dump; check the dump"
  else
    [ -n "$(docker compose ps --status running --quiet postgres)" ] ||
      die "postgres is not running in this project (set COMPOSE_PROJECT_NAME for another one)"
    (umask 077 && mkdir -p "$backup_dir")
    (umask 077 && cp .env "$env_copy")
    (umask 077 && docker compose exec -T postgres pg_dumpall -U postgres | gzip >"$dump") ||
      die "the database backup failed"
    gzip -dc "$dump" | tail -n 5 | grep -q 'PostgreSQL database cluster dump complete' ||
      die "the backup $dump is incomplete (no \"cluster dump complete\" at its end)"
    step "backup ok: $env_copy, $dump"
  fi

  step "3/9 checkout v$version"
  if [ "$dry_run" -eq 0 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    die "the checkout has local changes to tracked files; commit or stash them first"
  fi
  run git checkout --detach "v$version"

  step "4/9 verify the release images and the secrets it needs"
  run ./verify-release.sh "$version"
  # A release may need secrets an older one did not (ADR 0074: one NATS password per service).
  # Checked before .env or a container changes; the script names what is missing, never a value.
  if [ -f ./nats-passwords.sh ]; then
    run bash ./nats-passwords.sh --check .env ||
      die "the NATS passwords are missing in .env: run ./nats-passwords.sh .env, then deploy again"
  fi

  step "5/9 set MOWDECK_VERSION=$version in .env"
  if [ "$dry_run" -eq 1 ]; then
    echo "deploy: [dry-run] set MOWDECK_VERSION=$version in .env"
  elif grep -q '^MOWDECK_VERSION=' .env; then
    sed -i "s/^MOWDECK_VERSION=.*/MOWDECK_VERSION=$version/" .env
  else
    printf 'MOWDECK_VERSION=%s\n' "$version" >>.env
  fi

  step "6/9 pull"
  run docker compose "${files[@]}" pull

  close_window() {
    if [ "$opened" -eq 1 ]; then
      opened=0
      docker compose exec -T notifications python -m notifications.cli maintenance close ||
        echo "deploy: warning: could not close the maintenance window; it expires by itself" >&2
    fi
  }
  trap close_window EXIT

  if [ "$maintenance" -eq 1 ]; then
    step "7/9 open the maintenance window ($minutes min)"
    if [ "$dry_run" -eq 1 ]; then
      echo "deploy: [dry-run] notifications maintenance open --minutes $minutes"
    elif docker compose exec -T notifications python -m notifications.cli maintenance open \
      --minutes "$minutes" --reason "deploy $version"; then
      opened=1
    else
      echo "deploy: warning: no maintenance window (old notifications image?); the restart may" \
        "send a short offline/online pair" >&2
    fi
  else
    step "7/9 maintenance window skipped (--no-maintenance)"
  fi

  step "8/9 up --wait"
  run docker compose "${files[@]}" up -d --no-build --wait

  step "9/9 close the maintenance window and check the services"
  close_window
  if [ "$dry_run" -eq 1 ]; then
    echo "deploy: [dry-run] docker compose ps; check every service"
    return 0
  fi
  local bad=0 service state health code
  while IFS='|' read -r service state health code; do
    [ -n "$service" ] || continue
    printf 'deploy: %-24s %-10s %s\n' "$service" "$state" "${health:--}"
    if [ "$state" = exited ] && [ "$code" = 0 ]; then
      continue # a finished migration or bootstrap job
    fi
    if [ "$state" != running ] || [ "$health" = unhealthy ] || [ "$health" = starting ]; then
      bad=1
    fi
  done < <(docker compose "${files[@]}" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}')
  [ "$bad" -eq 0 ] || die "a service is not running and healthy (see above); the backup is in $backup_dir"
  step "done: MowDeck v$version is running"
}

main "$@"
exit
