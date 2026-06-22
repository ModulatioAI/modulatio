#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Modulatio standalone uninstaller.
#
# Independent of the Python package — works even if `modulatio` won't import.
# Mirrors `modulatio uninstall`. Best-effort: keeps going past individual
# failures so a partially-broken install still ends up clean.
#
#   Always removes : install footprint (vector cache + embedded-model cache
#                    + daemon process/pid/log) and the package (pipx/pip).
#   Opt-in         : --remove-settings      ~/.config/modulatio (settings + secrets)
#                    --remove-projects      the vault (your work + secret .env)
#                    --remove-deliverables  ~/Documents/Modulatio
#                    --remove-pandoc        pandoc (system pkg needs sudo)
#   --pristine     : reset to never-installed — remove EVERYTHING above + clear
#                    the pip build cache. Tiers still confirmed unless --yes.
#   Never touched  : ~/modulatio-backups, ~/.claude, ~/.codex, ~/.grok
#
# Usage: ./uninstall.sh [--pristine] [--remove-settings] [--remove-projects]
#          [--remove-deliverables] [--remove-pandoc] [--keep-package] [--yes]
#
# SOURCE-SAFE: sourcing (or eval-ing) this file only DEFINES its functions — the
# uninstaller runs ONLY when the script is executed directly. The guard at the
# bottom uses a `return` probe (true only when actually sourced, regardless of a
# caller-controlled $0) and refuses eval/injected input (empty BASH_SOURCE).
# `set -u` lives inside main() so sourcing never mutates the caller's shell.

# --- safe_rm: the catastrophic-path guard (never $HOME / root / too-shallow /
#     a source checkout) and NEVER follows a symlink out -----------------------
safe_rm() {
  local p="${1:-}"
  [ -z "$p" ] && return 0
  # Resolve for VALIDATION only — removal acts on the ORIGINAL path below.
  local rp; rp="$(readlink -f "$p" 2>/dev/null || echo "$p")"
  case "$rp" in
    "/"|"$HOME"|"$HOME/") echo "  refusing unsafe path: $rp"; return 0 ;;
  esac
  # Refuse a top-level dir ("/", "/tmp", "/home", ...): slash-depth < 2.
  # $HOME itself has depth 2 but is caught by the explicit case above, so a
  # legit depth-2 cache like /tmp/fastembed_cache stays removable.
  local depth; depth="$(awk -F/ '{print NF-1}' <<<"$rp")"
  if [ "$depth" -lt 2 ]; then echo "  refusing top-level path: $rp"; return 0; fi
  # Never delete a SOURCE / CODE CHECKOUT (carries .git or a packaging manifest).
  if [ -d "$rp" ] && { [ -e "$rp/.git" ] || [ -e "$rp/pyproject.toml" ] || [ -e "$rp/setup.py" ]; }; then
    echo "  refusing source/repo dir: $rp"; return 0
  fi
  # Never FOLLOW a symlink: a cache symlink pointing at unrelated work must not
  # delete the work. Unlink the link itself; the target is left untouched.
  if [ -L "$p" ]; then rm -f -- "$p" && echo "  removed symlink: $p"; return 0; fi
  if [ ! -e "$p" ]; then echo "  absent: $p"; return 0; fi
  rm -rf -- "$p" && echo "  removed: $p" || echo "  FAILED: $p"
}

resolve_vault() {
  local def="$CONFIG_DIR/defaults.json"
  if [ -f "$def" ] && command -v python3 >/dev/null 2>&1; then
    python3 -c "import json;print(json.load(open('$def')).get('vault_root',''))" 2>/dev/null
  fi
}

stop_daemon() {
  local pidf="$CONFIG_DIR/daemon.pid" pid=""
  if [ -f "$pidf" ]; then
    pid="$(cat "$pidf" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null; sleep 1
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
      echo "  daemon stopped (pid $pid)"
    fi
    rm -f "$pidf"
  else
    echo "  no daemon running"
  fi
}

backup_userdata() {
  local items=()
  [ "$REMOVE_SETTINGS" = 1 ] && [ -e "$CONFIG_DIR" ] && items+=("$CONFIG_DIR")
  # Only back up the vault if we'll actually remove it (Modulatio-owned).
  [ "$REMOVE_PROJECTS" = 1 ] && vault_owned "$VAULT" && [ -e "$VAULT" ] && items+=("$VAULT")
  [ "$REMOVE_DELIVERABLES" = 1 ] && [ -e "$DELIVERABLES" ] && items+=("$DELIVERABLES")
  [ "${#items[@]}" -eq 0 ] && return 0
  local dest="$HOME/modulatio-uninstall-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  if tar -czf "$dest" "${items[@]}" 2>/dev/null; then
    echo "Backed up your data -> $dest"
  fi
}

remove_pandoc() {
  local path; path="$(command -v pandoc 2>/dev/null)"
  if [ -z "$path" ]; then echo "  pandoc: not found"; return 0; fi
  local real; real="$(readlink -f "$path" 2>/dev/null || echo "$path")"
  if command -v dpkg >/dev/null 2>&1 && dpkg -S "$real" >/dev/null 2>&1; then
    echo "  removing pandoc via apt (sudo)"; sudo apt-get remove -y pandoc
  elif command -v rpm >/dev/null 2>&1 && rpm -qf "$real" >/dev/null 2>&1; then
    echo "  removing pandoc via dnf (sudo)"; sudo dnf remove -y pandoc
  elif printf '%s' "$real" | grep -q "/Cellar/\|/homebrew/"; then
    echo "  removing pandoc via brew"; brew uninstall pandoc
  else
    echo "  removing pandoc binary: $real"; rm -f "$real"
  fi
}

remove_package() {
  if command -v pipx >/dev/null 2>&1 && pipx uninstall modulatio >/dev/null 2>&1; then
    echo "  removed package via pipx"; return 0
  fi
  local pip; pip="$(command -v pip 2>/dev/null || command -v pip3 2>/dev/null)"
  if [ -n "$pip" ] && "$pip" uninstall -y modulatio >/dev/null 2>&1; then
    echo "  removed package via pip"; return 0
  fi
  echo "  package: could not auto-remove — run 'pipx uninstall modulatio' yourself"
}

clear_pip_cache() {
  local pip; pip="$(command -v pip 2>/dev/null || command -v pip3 2>/dev/null)"
  if [ -n "$pip" ]; then
    "$pip" cache remove "modulatio*" >/dev/null 2>&1
    echo "  cleared pip wheel cache for modulatio (fresh rebuild next install)"
  fi
}

# A vault is Modulatio-owned only if an EXACT path component is named 'modulatio'
# (case-insensitive) — matching the Python guard. A substring like
# 'customer-modulatio-notes' is the USER's folder and is never auto-deleted.
vault_owned() {
  local resolved; resolved="$(readlink -f "$1" 2>/dev/null || echo "$1")"
  local IFS='/' part rc=1
  # set -f for the split: the unquoted $resolved must word-split on '/' WITHOUT
  # pathname-expanding a component that contains a glob char (e.g. '*' or '[').
  # Restore globbing after; readlink -f already resolved the real path.
  set -f
  for part in $resolved; do
    if [ "$(printf '%s' "$part" | tr 'A-Z' 'a-z')" = "modulatio" ]; then
      rc=0; break
    fi
  done
  set +f
  return "$rc"
}

# confirm <prompt> <default 0|1> -> echoes 0 or 1. Honors the pre-set default
# (so --pristine pre-checks Yes) while still letting the operator opt out.
confirm() {
  local hint="[y/N]"; [ "$2" = 1 ] && hint="[Y/n]"
  local ans; read -rp "$1 $hint " ans
  if [ -z "${ans:-}" ]; then echo "$2"
  elif [[ "$ans" =~ ^[Yy] ]]; then echo 1; else echo 0; fi
}

# remove the fastembed env-override cache ONLY when its BASENAME is exactly a
# fastembed-owned cache name (parity with the Python guard). A substring or a
# parent component containing "fastembed" is NOT ownership — an arbitrary work
# dir pointed at FASTEMBED_CACHE_PATH is never auto-removed.
remove_fastembed_env_cache() {
  [ -z "${FASTEMBED_CACHE_PATH:-}" ] && return 0
  local base; base="$(basename "$FASTEMBED_CACHE_PATH" | tr 'A-Z' 'a-z')"
  case "$base" in
    fastembed|fastembed_cache) safe_rm "$FASTEMBED_CACHE_PATH" ;;
    *) echo "  skipping non-fastembed FASTEMBED_CACHE_PATH: ${FASTEMBED_CACHE_PATH}" ;;
  esac
}

# --- main: the destructive flow. Runs ONLY when executed directly (see the
#     source-guard at the bottom), never on source/eval. --------------------
main() {
  set -u
  REMOVE_SETTINGS=0; REMOVE_PROJECTS=0; REMOVE_DELIVERABLES=0
  REMOVE_PANDOC=0; REMOVE_PACKAGE=1; PRISTINE=0; YES=0
  for arg in "$@"; do
    case "$arg" in
      --remove-settings)     REMOVE_SETTINGS=1 ;;
      --remove-projects)     REMOVE_PROJECTS=1 ;;
      --remove-deliverables) REMOVE_DELIVERABLES=1 ;;
      --remove-pandoc)       REMOVE_PANDOC=1 ;;
      --keep-package)        REMOVE_PACKAGE=0 ;;
      --pristine)            PRISTINE=1 ;;
      --yes|-y)              YES=1 ;;
      -h|--help)             sed -n '3,20p' "$0"; exit 0 ;;
      *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
  done
  # --pristine = reset to never-installed: remove everything + clear the pip cache.
  if [ "$PRISTINE" = 1 ]; then
    REMOVE_SETTINGS=1; REMOVE_PROJECTS=1; REMOVE_DELIVERABLES=1; REMOVE_PANDOC=1
  fi

  # --- Paths (match config.py: CONFIG is ~/.config, cache/data honor XDG) ----
  CONFIG_DIR="$HOME/.config/modulatio"
  CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/modulatio"
  DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/modulatio"
  DEFAULT_VAULT="$DATA_HOME/projects"
  VAULT="$(resolve_vault)"
  [ -z "$VAULT" ] && VAULT="$DEFAULT_VAULT"
  DELIVERABLES="${MODULATIO_DELIVERY_DIR:-$HOME/Documents/Modulatio}"

  echo "Modulatio uninstaller${PRISTINE:+ — pristine (reset to never-installed)}"
  if [ "$YES" != 1 ]; then
    REMOVE_SETTINGS=$(confirm "Remove your SETTINGS ($CONFIG_DIR)?" "$REMOVE_SETTINGS")
    [ "$VAULT" != "$DEFAULT_VAULT" ] && \
      echo "  ⚠ Your vault is a CUSTOM folder (e.g. an Obsidian vault) — real work: $VAULT"
    REMOVE_PROJECTS=$(confirm "Remove your PROJECT FOLDER ($VAULT)?" "$REMOVE_PROJECTS")
    REMOVE_DELIVERABLES=$(confirm "Remove your DELIVERABLES ($DELIVERABLES)?" "$REMOVE_DELIVERABLES")
    if command -v pandoc >/dev/null 2>&1; then
      REMOVE_PANDOC=$(confirm "Remove pandoc?" "$REMOVE_PANDOC")
    fi
    echo
    [ "$(confirm "Proceed with uninstall?" 0)" = 1 ] || { echo "Aborted — nothing removed."; exit 1; }
  fi

  echo "Stopping daemon..."; stop_daemon
  backup_userdata
  echo "Removing install footprint..."
  safe_rm "$CACHE_DIR"
  safe_rm "$CONFIG_DIR/daemon.log"
  remove_fastembed_env_cache
  safe_rm "$HOME/.cache/fastembed"
  safe_rm "${TMPDIR:-/tmp}/fastembed_cache"
  [ "$REMOVE_SETTINGS" = 1 ]     && safe_rm "$CONFIG_DIR"
  if [ "$REMOVE_PROJECTS" = 1 ]; then
    safe_rm "$DATA_HOME"  # Modulatio's namespace — always ours
    if vault_owned "$VAULT"; then
      safe_rm "$VAULT"
    else
      echo "  ⚠ Vault $VAULT looks like YOUR folder (not Modulatio's) — NOT deleting it. Remove by hand if you mean to."
    fi
  fi
  [ "$REMOVE_DELIVERABLES" = 1 ] && safe_rm "$DELIVERABLES"
  [ "$REMOVE_PANDOC" = 1 ]       && remove_pandoc
  [ "$REMOVE_PACKAGE" = 1 ]      && remove_package
  [ "$PRISTINE" = 1 ]            && clear_pip_cache

  echo "Done. Never auto-touched: ~/modulatio-backups, ~/.claude ~/.codex ~/.grok."
}

# Source-guard: execute main ONLY on direct invocation.
#   * sourced (incl. `bash -c 'source "$0"' …` $0-spoof): the `return` probe
#     succeeds → skip main. A caller-controlled $0 cannot defeat this.
#   * eval / injected (empty BASH_SOURCE): skip main.
#   * direct execution: run main.
if (return 0 2>/dev/null); then
  :
elif [ "${#BASH_SOURCE[@]}" -eq 0 ]; then
  :
else
  main "$@"
fi
