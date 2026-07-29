#!/usr/bin/env bash
# Capture the designated Linux gate's black-box substrate tier as a committable
# evidence artifact.
#
# The provenance must describe the CODE COMMIT UNDER TEST, so every line is
# assembled in an EXTERNAL temporary file and copied into the tracked path only
# at the end. Redirecting into the tracked path first would dirty the worktree
# before `git status --porcelain` is measured, and the artifact would then
# record itself as a modification while claiming a clean capture.
#
# Refuses to run on a dirty worktree: provenance for an uncommitted tree
# names a commit whose content was not what ran.
#
# Usage: ./scripts/capture-substrate-evidence.sh
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

TIER="tests/test_access_sandbox_blackbox.py"
DEST="docs/gate-evidence/blackbox-substrate-tier.txt"
PY="${PYTHON:-$REPO/.venv/bin/python}"

porcelain="$(git status --porcelain)"
if [[ -n "$porcelain" ]]; then
    echo "refusing: worktree is dirty, so provenance would not describe the" >&2
    echo "commit under test. Commit or stash first:" >&2
    echo "$porcelain" >&2
    exit 1
fi

tmp="$(mktemp -t substrate-evidence.XXXXXX)"
trap 'rm -f "$tmp"' EXIT

{
    echo "# Designated Linux gate — black-box substrate tier"
    echo
    echo "## Provenance (captured BEFORE the run, from the clean commit under test)"
    echo "git rev-parse HEAD : $(git rev-parse HEAD)"
    echo "git status --porcelain:"
    echo "<empty — clean worktree>"
    echo "run-started-utc     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "## Host"
    echo "uname -srm: $(uname -srm)"
    echo "kernel.unprivileged_userns_clone: $(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || echo 'n/a')"
    echo "bwrap: $(command -v bwrap || echo 'MISSING') — $(bwrap --version 2>/dev/null || echo 'n/a')"
    if bwrap --unshare-all --die-with-parent --ro-bind / / true 2>/dev/null; then
        echo "prerequisite probe (bwrap --unshare-all --die-with-parent --ro-bind / / true): PASS (rc 0 — bwrap confines)"
    else
        echo "prerequisite probe (bwrap --unshare-all --die-with-parent --ro-bind / / true): FAIL (bwrap cannot confine on this host)"
    fi
    echo
    echo "## Six-test tier"
    echo '```'
} > "$tmp"

# Test output goes through the same temporary file; a failing tier still gets
# recorded, because evidence of a red tier is evidence.
set +e
"$PY" -m pytest "$TIER" -v --no-header -p no:cacheprovider >> "$tmp" 2>&1
rc=$?
set -e

{
    echo '```'
    echo
    echo "run-finished-utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$tmp"

cp "$tmp" "$DEST"
echo "wrote $DEST (tier exit $rc)"
exit "$rc"
