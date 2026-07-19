#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
#
# Build the Modulatio .deb — self-contained, generic Debian/Ubuntu (amd64).
#
# The package carries its own CPython (a pinned python-build-standalone
# release) plus Modulatio and its dependencies installed into it, under
# /opt/modulatio. Nothing depends on the target's Python — the only runtime
# requirement is glibc — so one artifact installs identically across
# Debian/Ubuntu releases and derivatives.
#
# Layout installed by the package:
#   /opt/modulatio/python/          the bundled interpreter + site-packages
#   /usr/bin/modulatio{,-tui,-api}  thin shims into the bundle
#   /usr/lib/systemd/user/modulatio-api.service
#
# Usage: packaging/build_deb.sh [wheel]
#   wheel  path to a modulatio wheel; defaults to the newest in dist/.
# Produces: dist/modulatio_<version>_amd64.deb
#
# Runs on any Linux with bash, curl, tar, and dpkg-deb — a clean container
# or a workstation; the payload is fully specified (pinned interpreter,
# wheels from PyPI) so the build host leaves no fingerprint.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# --- pinned interpreter -------------------------------------------------------
PY_VERSION="3.12.13"
PY_RELEASE="20260623"
PY_TARBALL="cpython-${PY_VERSION}+${PY_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_TARBALL}"
PY_SHA256="9fa869d69be54f6b8eeae64272fbd9bb0646e0e1a8da9d80e51ba5a3bee48930"

# --- inputs -------------------------------------------------------------------
WHEEL="${1:-}"
if [ -z "$WHEEL" ]; then
  WHEEL="$(ls -t "$REPO_ROOT"/dist/modulatio-*-py3-none-any.whl 2>/dev/null | head -1 || true)"
fi
if [ -z "$WHEEL" ] || [ ! -f "$WHEEL" ]; then
  echo "error: no modulatio wheel found — build one first (python -m build --wheel)" >&2
  exit 1
fi
VERSION="$(basename "$WHEEL" | sed -E 's/^modulatio-([^-]+)-.*/\1/')"
echo "wheel:   $WHEEL"
echo "version: $VERSION"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/root"
OPT="$ROOT/opt/modulatio"
mkdir -p "$OPT" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/user" "$ROOT/DEBIAN" \
         "$ROOT/usr/share/doc/modulatio"

# --- interpreter --------------------------------------------------------------
CACHE="${MODULATIO_DEB_CACHE:-$STAGE}"
mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PY_TARBALL" ]; then
  echo "fetching $PY_TARBALL …"
  curl -fsSL -o "$CACHE/$PY_TARBALL" "$PY_URL"
fi
echo "$PY_SHA256  $CACHE/$PY_TARBALL" | sha256sum -c - >/dev/null
tar -xzf "$CACHE/$PY_TARBALL" -C "$OPT"   # extracts to $OPT/python
PYBIN="$OPT/python/bin/python3"
test -x "$PYBIN"

# --- modulatio + deps into the bundle ----------------------------------------
# Dependencies come from a hash-pinned lock, not a fresh lower-bound solve, so
# the shipped artifact's transitive graph is frozen and every wheel is verified
# against a known digest (--require-hashes). manylinux wheels only — the bundle
# carries no host-compiled artifacts. The project wheel installs WITHOUT
# re-resolving (--no-deps): the lock is the single source of truth for deps.
LOCK="$REPO_ROOT/packaging/requirements-web.lock"
"$PYBIN" -m pip install --quiet --no-cache-dir --only-binary=:all: \
  --require-hashes -r "$LOCK"
"$PYBIN" -m pip install --quiet --no-cache-dir --only-binary=:all: \
  --no-deps "$WHEEL"

# Fail closed on lock/wheel drift. --require-hashes verifies only the packages
# the lock names, and --no-deps skips the wheel's Requires-Dist check — so a
# stale lock (a new project dependency, a tightened bound) could otherwise
# install green with an incomplete graph. pip check validates that the bundled
# environment actually satisfies every installed package's metadata; a miss
# aborts the build before packaging.
"$PYBIN" -m pip check

# Rewrite entry-point shebangs to the INSTALLED interpreter path (pip wrote
# the staging path, which does not exist on a target machine).
for f in "$OPT/python/bin/"*; do
  [ -f "$f" ] || continue
  head -c2 "$f" | grep -q '#!' || continue
  sed -i "1s|^#!.*python.*|#!/opt/modulatio/python/bin/python3|" "$f"
done

# --- /usr/bin shims -----------------------------------------------------------
for cmd in modulatio modulatio-tui modulatio-api; do
  cat > "$ROOT/usr/bin/$cmd" <<SHIM
#!/bin/sh
exec /opt/modulatio/python/bin/$cmd "\$@"
SHIM
  chmod 755 "$ROOT/usr/bin/$cmd"
done

# --- systemd user unit --------------------------------------------------------
cat > "$ROOT/usr/lib/systemd/user/modulatio-api.service" <<'UNIT'
[Unit]
Description=Modulatio WebOS API (modulatio-api)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/modulatio-api
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT

# --- package metadata ---------------------------------------------------------
INSTALLED_KB="$(du -sk "$ROOT" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: modulatio
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: ${INSTALLED_KB}
Depends: libc6 (>= 2.17)
Maintainer: Modulatio AI <hello@modulatio.ai>
Homepage: https://modulatio.ai
Description: Orchestrate teams of LLM agents on real projects
 Modulatio is a TUI, CLI, and browser (WebOS) framework for running teams
 of LLM agents against project objectives — producers generate, QC reviews
 and patches, the Leader plans and verifies. This package is fully
 self-contained: it bundles its own Python interpreter and dependencies
 under /opt/modulatio and requires only glibc from the host. Architecture: amd64.
CONTROL

cat > "$ROOT/usr/share/doc/modulatio/README.Debian" <<'DOC'
Modulatio — Debian/Ubuntu package

Commands: modulatio (CLI) · modulatio-tui (terminal UI) · modulatio-api (WebOS)

To keep the WebOS running across logins/reboots (user-level, no sudo):
  systemctl --user enable --now modulatio-api
  sudo loginctl enable-linger $USER    # start at boot, before login
or equivalently: modulatio-api --install-service
DOC

# --- build --------------------------------------------------------------------
OUT="$REPO_ROOT/dist/modulatio_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group "$ROOT" "$OUT" >/dev/null
echo "built:   $OUT"
dpkg-deb --info "$OUT" | sed -n '1,8p'
