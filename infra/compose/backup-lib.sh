#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# The pre-deploy rollback dump of deploy.sh, as functions to source (docs/ops/compose.md, "The
# pre-deploy dump"). Not run on its own and not a backup strategy: the newest few dumps stay so a
# failed update can be rolled back, older ones are deleted.
#
#   backup_settings             check MOWDECK_BACKUP_KEEP and the two directories
#   backup_write_dump FILE CMD...   write and verify one dump (the "write and verify" function)
#   backup_protect_env ENV COPY     keep a previous .env apart from the dumps (secrets)
#   backup_rotate DIR PREFIX SUFFIX KEEP   keep the newest KEEP files, delete the rest (rotate)
#
# Settings (environment):
#   MOWDECK_BACKUP_DIR      dumps, default ~/mowdeck-backups (mode 0700, dumps 0600)
#   MOWDECK_ENV_BACKUP_DIR  previous .env copies, default ~/.mowdeck-env-backups (0700 / 0600);
#                           must not be inside the dump directory, nor the other way round
#   MOWDECK_BACKUP_KEEP     how many dumps (and .env copies) to keep, default 3
#
# File names carry a UTC stamp (<prefix><version>-<YYYYmmddTHHMMSSZ><suffix>); the order of the
# stamps decides what is "newest". Only files of exactly that name shape are ever deleted, and
# nothing is deleted before the new dump has been written and verified. No function prints a
# value of .env: messages carry file names and counts only.

BACKUP_DUMP_MARKER='PostgreSQL database cluster dump complete'

# Resolved settings, set by backup_settings.
backup_dir=""
env_backup_dir=""
backup_keep=3

backup_settings() {
  backup_dir="${MOWDECK_BACKUP_DIR:-$HOME/mowdeck-backups}"
  env_backup_dir="${MOWDECK_ENV_BACKUP_DIR:-$HOME/.mowdeck-env-backups}"
  backup_keep="${MOWDECK_BACKUP_KEEP:-3}"
  if ! [[ "$backup_keep" =~ ^[0-9]+$ ]] || [ "$backup_keep" -lt 1 ] || [ "$backup_keep" -gt 100 ]; then
    echo "backup: MOWDECK_BACKUP_KEEP must be a number from 1 to 100" >&2
    return 1
  fi
  # The point of two directories is that copying the dumps off the host never takes the keys that
  # decrypt them along. Compared as strings with a trailing slash; ".." and links are the
  # operator's business.
  local a="${backup_dir%/}/" b="${env_backup_dir%/}/"
  if [ "$a" = "$b" ] || [[ "$a" == "$b"* ]] || [[ "$b" == "$a"* ]]; then
    echo "backup: MOWDECK_ENV_BACKUP_DIR must not be inside MOWDECK_BACKUP_DIR (or the reverse)" >&2
    return 1
  fi
}

# A private directory: created 0700, an existing one is tightened.
backup_private_dir() {
  if ! (umask 077 && mkdir -p "$1") || ! chmod 700 "$1"; then
    echo "backup: cannot create or protect the directory $1 (mode 0700)" >&2
    return 1
  fi
}

# Writes the dump of "$@" (a command that prints a pg_dumpall to stdout) as gzip to FILE: first to
# FILE.partial, checked (gzip intact, not empty, ends with the "cluster dump complete" line), then
# moved into place with mode 0600. On any failure the partial file is removed and 1 is returned;
# no older file is touched.
backup_write_dump() {
  local file="$1" partial="$1.partial"
  shift
  backup_private_dir "$(dirname "$file")" || return 1
  rm -f "$partial"
  if ! (umask 077 && "$@" | gzip >"$partial"); then
    rm -f "$partial"
    echo "backup: the database dump command failed" >&2
    return 1
  fi
  if [ ! -s "$partial" ] || ! gzip -t "$partial" 2>/dev/null ||
    ! gzip -dc "$partial" | tail -n 5 | grep -q "$BACKUP_DUMP_MARKER"; then
    rm -f "$partial"
    echo "backup: the dump is empty or incomplete (no \"cluster dump complete\" at its end)" >&2
    return 1
  fi
  if ! chmod 600 "$partial" || ! mv -f "$partial" "$file"; then
    rm -f "$partial"
    echo "backup: cannot finish $file" >&2
    return 1
  fi
}

# Keeps a copy of ENV (the live .env) as COPY, in the private directory of COPY. The copy is the
# only place a rollback finds the secrets that match an older dump, so it is never written next to
# a dump (backup_settings forbids that) and never printed.
backup_protect_env() {
  local env_file="$1" copy="$2"
  backup_private_dir "$(dirname "$copy")" || return 1
  if ! (umask 077 && cp "$env_file" "$copy") || ! chmod 600 "$copy"; then
    echo "backup: cannot copy the environment file to $copy" >&2
    return 1
  fi
}

# Older versions of deploy.sh wrote env-before-*.env next to the dumps. Moves them (not deletes)
# into the private env directory, so the keys leave the dump directory on the next deploy and the
# rotation there treats them like newer copies.
backup_move_legacy_env() {
  local from="$1" to="$2" file moved=0
  [ -d "$from" ] || return 0
  for file in "$from"/env-before-*.env; do
    [ -f "$file" ] || continue
    backup_private_dir "$to" || return 1
    mv -n "$file" "$to/" && chmod 600 "$to/$(basename "$file")" || return 1
    moved=$((moved + 1))
  done
  [ "$moved" -eq 0 ] || echo "backup: moved $moved earlier .env copies from $from to $to" >&2
}

# Deletes everything named DIR/PREFIX<version>-<stamp>SUFFIX except the newest KEEP (by stamp, the
# last dash-separated part of the name; ties by name). Other files, partial files and
# sub-directories stay. Safe to run again: a second run deletes nothing.
backup_rotate() {
  local dir="$1" prefix="$2" suffix="$3" keep="$4" file name stamp index=0 removed=0
  [ -d "$dir" ] || return 0
  while IFS=$'\t' read -r stamp file; do
    index=$((index + 1))
    [ "$index" -gt "$keep" ] || continue
    rm -f -- "$file" || return 1
    removed=$((removed + 1))
  done < <(
    for file in "$dir/$prefix"*"$suffix"; do
      [ -f "$file" ] || continue
      name="${file##*/}"
      name="${name%"$suffix"}"
      stamp="${name##*-}"
      [[ "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
      printf '%s\t%s\n' "$stamp" "$file"
    done | LC_ALL=C sort -r
  )
  [ "$removed" -eq 0 ] || echo "backup: removed $removed old file(s) from $dir (keeping $keep)" >&2
}
