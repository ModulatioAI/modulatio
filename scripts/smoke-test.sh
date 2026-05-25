#!/usr/bin/env bash
# Pre-tag manual smoke test for Modulatio.
#
# Walks a fresh-clone-style install end-to-end against an isolated
# vault + config so it doesn't touch your real Modulatio state.
# Exits non-zero on any check failure.
#
# Usage:
#   ./scripts/smoke-test.sh               # uses repo at $PWD
#   ./scripts/smoke-test.sh /path/to/repo # uses specific clone
#
# The script does NOT exercise the setup wizard (interactive) or any
# real LLM provider (no API keys required). It runs `modulatio kickoff
# --stub` which uses canned stub runners — offline-safe, deterministic,
# and the same code path the test suite uses for end-to-end coverage.

set -euo pipefail

# ── Setup ──────────────────────────────────────────────────────────────
REPO_ROOT="${1:-$(pwd)}"
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"

if [[ ! -f "$REPO_ROOT/pyproject.toml" ]]; then
  echo "✗ $REPO_ROOT does not look like a Modulatio repo (no pyproject.toml)"
  exit 1
fi

echo "Smoke-testing Modulatio at $REPO_ROOT"
echo

# Isolated vault + config — never touches the user's real state.
SMOKE_DIR="$(mktemp -d /tmp/modulatio-smoke-XXXXXX)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
export XDG_CONFIG_HOME="$SMOKE_DIR/config"
export XDG_DATA_HOME="$SMOKE_DIR/data"
export XDG_CACHE_HOME="$SMOKE_DIR/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"

PASS=0
FAIL=0

check() {
  local label="$1"
  shift
  if "$@" > "$SMOKE_DIR/last.log" 2>&1; then
    echo "  ✓ $label"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $label"
    echo "    ── log tail ──"
    sed 's/^/      /' "$SMOKE_DIR/last.log" | tail -10
    FAIL=$((FAIL + 1))
  fi
}

# ── Step 1: Python version ─────────────────────────────────────────────
echo "[1/6] Python version compatibility"
PYTHON="${PYTHON:-/usr/bin/python3.12}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3.12 || command -v python3)"
fi
check "Python ≥3.11, <3.14" bash -c "
  $PYTHON -c 'import sys; v = sys.version_info; assert (3,11) <= (v.major, v.minor) < (3,14), f\"got {v}\"'
"
echo

# ── Step 2: Venv + install ─────────────────────────────────────────────
echo "[2/6] Fresh venv + editable install"
VENV="$SMOKE_DIR/.venv"
check "create venv" "$PYTHON" -m venv "$VENV"
# Use the venv's Python going forward.
VENV_PY="$VENV/bin/python"
VENV_PIP="$VENV/bin/pip"
check "upgrade pip" "$VENV_PIP" install --quiet --upgrade pip
check "install modulatio[dev]" "$VENV_PIP" install --quiet -e "$REPO_ROOT[dev]"
echo

# ── Step 3: Console scripts on PATH ────────────────────────────────────
echo "[3/6] Console scripts present"
PATH="$VENV/bin:$PATH"
for cmd in modulatio modulatio-tui modulatio-standards modulatio-memory; do
  check "$cmd --help" "$VENV/bin/$cmd" --help
done
echo

# ── Step 4: Doctor + version ────────────────────────────────────────────
echo "[4/6] CLI sanity"
check "modulatio --version" "$VENV/bin/modulatio" --version
# `modulatio doctor` will report missing wizard config (expected on a
# pre-wizard machine); we only check it exits 0 and prints something.
check "modulatio doctor runs" bash -c "$VENV/bin/modulatio doctor | grep -q ."
echo

# ── Step 5: Stub kickoff (offline end-to-end) ──────────────────────────
echo "[5/6] Offline kickoff via stub runners"
# `modulatio kickoff --stub` exercises the full plan/dispatch/QC/reflect
# pipeline with canned responses — no API keys, deterministic, and the
# same path the test suite's end-to-end coverage uses.
check "modulatio kickoff --stub" "$VENV/bin/modulatio" kickoff \
  --code SMK \
  --objective "smoke test artifact" \
  --stub
echo

# ── Step 6: Test suite ──────────────────────────────────────────────────
echo "[6/6] Pytest"
check "pytest -q (full suite)" bash -c "cd '$REPO_ROOT' && '$VENV/bin/pytest' -q"
check "ruff check src/" bash -c "cd '$REPO_ROOT' && '$VENV/bin/ruff' check src/"
echo

# ── Summary ─────────────────────────────────────────────────────────────
echo "─────────────────────────────────"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "─────────────────────────────────"
if [[ "$FAIL" -gt 0 ]]; then
  echo "✗ Smoke test FAILED. See logs above."
  echo "  Last log: $SMOKE_DIR/last.log (saved for inspection)"
  trap - EXIT
  exit 1
else
  echo "✓ Smoke test passed. Ready for tag."
  exit 0
fi
