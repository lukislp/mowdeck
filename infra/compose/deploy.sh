#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Updates the Compose stack to a release: the documented procedure of docs/ops/compose.md in one
# script, with the deploy maintenance window around the restart (ADR 0055).
#
#   ./deploy.sh [--dry-run] [--window-minutes N] [--no-maintenance] <version>
#
# Steps (each failure stops the script before anything later runs):
#   1. git fetch --tags
#   2. rollback dump (backup-lib.sh): a gzip'd pg_dumpall in ${MOWDECK_BACKUP_DIR:-~/mowdeck-backups}
#      (directory 0700, file 0600), whose last lines must say "PostgreSQL database cluster dump
#      complete", and a copy of .env in ${MOWDECK_ENV_BACKUP_DIR:-~/.mowdeck-env-backups}, a
#      different directory; then only the newest ${MOWDECK_BACKUP_KEEP:-3} of each are kept. Nothing
#      is deleted when the new dump failed.
#   3. git checkout --detach v<version>  (refuses a checkout with local changes)
#   4. ./verify-release.sh <version>      (fails closed: nothing is pulled when it fails), then
#      ./nats-passwords.sh --check .env   (the release's NATS passwords, ADR 0074)
#   5. MOWDECK_VERSION=<version> in .env
#   6. docker compose pull
#   7. open the maintenance window in Notifications, so the restart of the Device-Gateway sends
#      no "mower offline / back online" pair (best effort: the first deploy of a build that
#      already has the window runs the old notifications image, which has no window; the script
#      then warns and goes on)
#   8. when NATS is running: reload its users and permissions (nats/, ADR 0074; SIGHUP) and wait up
#      to ${MOWDECK_NATS_RELOAD_SECONDS:-10} s for its "Reloaded server configuration" (a running
#      NATS keeps the permissions it read at its start; a refused reload stops here), then
#      docker compose up -d --no-build --wait
#   9. close the window (also when step 8 fails: a real outage must be reported), then print the
#      state of every service and fail when one is not running and healthy.
#
# --dry-run prints the steps and runs nothing. --window-minutes sets the length of the window
# (default 15; it expires by itself, so a forgotten window cannot silence alerts). --no-maintenance
# skips steps 7 and 9. A stack under another project name: COMPOSE_PROJECT_NAME=<name> ./deploy.sh.
#
# Secrets: the script never prints .env or any value of it; it only prints the file names. The
# .env copy lives apart from the dumps, so a dump copied off the host does not bring the keys that
# decrypt it along (docs/ops/compose.md, "The pre-deploy dump").
set -euo pipefail

# The whole script is one function that runs after bash has read it completely: the checkout in
# step 3 replaces this very file with the one of the release.
opened=0 # global: the EXIT trap reads it

main() {
  local here dry_run=0 maintenance=1 minutes=15 version=""
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here"
  # Sourced before step 3, like the rest of this script: the checkout replaces the file on disk.
  # shellcheck source=backup-lib.sh
  . "$here/backup-lib.sh"

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
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_settings || die "invalid backup settings (see above)"
  local env_copy="$env_backup_dir/env-before-$version-$stamp.env"
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

  step "2/9 rollback dump to $backup_dir (the .env copy to $env_backup_dir)"
  if [ "$dry_run" -eq 1 ]; then
    echo "deploy: [dry-run] pg_dumpall | gzip > $dump; check the dump; cp .env $env_copy"
    echo "deploy: [dry-run] keep the newest $backup_keep dumps and .env copies"
  else
    [ -n "$(docker compose ps --status running --quiet postgres)" ] ||
      die "postgres is not running in this project (set COMPOSE_PROJECT_NAME for another one)"
    backup_write_dump "$dump" docker compose exec -T postgres pg_dumpall -U postgres ||
      die "the database backup failed; no older backup was deleted"
    backup_protect_env .env "$env_copy" || die "the .env copy failed"
    # Only now, with the new pair complete, older ones may go.
    backup_move_legacy_env "$backup_dir" "$env_backup_dir" || die "cannot move the old .env copies"
    backup_rotate "$backup_dir" mowdeck-before- .sql.gz "$backup_keep" || die "rotation failed"
    backup_rotate "$env_backup_dir" env-before- .env "$backup_keep" || die "rotation failed"
    step "backup ok: $dump, $env_copy"
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

  # NATS reads nats/ (users and permissions, ADR 0074) only when it starts or reloads, and `up`
  # recreates it only when its service definition changed. A running NATS therefore keeps the
  # permissions it read at its start, and the services of the new release are refused what the
  # release newly allows them (the v0.69.4 deploy: "permissions violation for publish to
  # $JS.API.CONSUMER.CREATE..."). Reloading is cheap and keeps every connection, so it happens on
  # every deploy, not only when nats/ changed: a deploy run again after a failed one, or after a
  # checkout by hand, finds the new files already checked out. A NATS that is not running reads
  # them when `up` starts it.
  reload_nats() {
    if [ "$dry_run" -eq 1 ]; then
      echo "deploy: [dry-run] reload the running NATS (SIGHUP) and wait for its confirmation"
      return 0
    fi
    if [ -z "$(docker compose "${files[@]}" ps --status running --quiet nats)" ]; then
      step "NATS is not running; up starts it with the release's nats/"
      return 0
    fi
    step "reload the users and permissions of the running NATS"
    local since log waited=0 limit="${MOWDECK_NATS_RELOAD_SECONDS:-10}"
    since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker compose "${files[@]}" kill -s HUP nats || die "could not signal NATS to reload"
    while :; do
      log="$(docker compose "${files[@]}" logs --no-color --since "$since" nats 2>&1 || true)"
      case "$log" in
        *"Failed to reload"*)
          die "NATS refused the new configuration (docker compose logs nats); no service was restarted"
          ;;
        *"Reloaded server configuration"*)
          step "NATS reloaded its users and permissions"
          return 0
          ;;
      esac
      [ "$waited" -lt "$limit" ] ||
        die "NATS did not confirm the reload within $limit s (docker compose logs nats); no service was restarted"
      sleep 1
      waited=$((waited + 1))
    done
  }

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

  step "8/9 reload NATS, up --wait"
  reload_nats
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
  [ "$bad" -eq 0 ] || die "a service is not running and healthy (see above); the dump is in $backup_dir"
  step "done: MowDeck v$version is running"
}

main "$@"
exit
