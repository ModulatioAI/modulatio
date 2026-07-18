# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
#
# The Modulatio container — the same self-contained .deb the packaging
# pipeline ships, installed into a minimal Debian base, with an sshd whose
# sessions ARE the TUI and runtime auto-detection of a mounted Claude for Clay.
#
# Build (from the repo root, with the .deb already in dist/):
#   docker build -f packaging/Dockerfile --build-arg DEB=dist/modulatio_<ver>_amd64.deb -t modulatio .
#
# Run: see packaging/docker-compose.yml — host networking is the default so
# local model servers (Ollama / LM Studio on the host's localhost) and the
# OAuth loopback work unchanged; state lives in mounted volumes.

FROM debian:stable-slim

ARG DEB=dist/modulatio_amd64.deb

# ca-certificates: outbound TLS (cloud providers). curl: healthchecks.
# openssh-server: the TUI-over-SSH door. git: Clay and the solo-Leader coding
# lane expect it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-server \
 && rm -rf /var/lib/apt/lists/*

# Modulatio itself — the identical artifact the .deb channel ships.
COPY ${DEB} /tmp/modulatio.deb
RUN apt-get update \
 && apt-get install -y /tmp/modulatio.deb \
 && rm /tmp/modulatio.deb \
 && rm -rf /var/lib/apt/lists/*

# Clay (Claude Code) is NOT baked into the image — the image never downloads or
# runs a vendor installer at build time. Instead the container auto-detects an
# EXISTING Claude at runtime: mount your host's Claude install (the shipped
# compose does this by default) or a sidecar's, and the entrypoint points
# MODULATIO_CLAUDE_BIN at the newest version it finds under /opt/claude. No
# Claude present → Clay is simply unavailable; everything else runs. This keeps
# Clay bring-your-own — your Claude, your version, your model — never pinned.
RUN useradd --create-home --uid 1000 --shell /bin/bash modulatio

# TUI-over-SSH: key-only, single user, and the session IS the TUI —
# ForceCommand drops the client straight into modulatio-tui; closing the TUI
# closes the session. Run with the `ssh` command (the compose ssh service does);
# authorized_keys arrives via the state volume — no key, no login.
# Loopback-bound by default: the daemon listens only on 127.0.0.1, so the door
# is reachable from the docker host (the realistic path) but not exposed on the
# network. DisableForwarding closes port/agent/X11/tunnel channels — otherwise
# `ssh -N -L/-R/-D` would tunnel past ForceCommand (and, under host networking,
# reach host-loopback services). ForceCommand uses the absolute shim path
# (PATH begins with the user-writable ~/.local/bin).
RUN mkdir -p /run/sshd && printf '%s\n' \
      'Port 2222' \
      'ListenAddress 127.0.0.1' \
      'AllowUsers modulatio' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PermitRootLogin no' \
      'DisableForwarding yes' \
      'ForceCommand env TERM=xterm-256color /usr/bin/modulatio-tui' \
      > /etc/ssh/sshd_config.d/modulatio-tui.conf

COPY packaging/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

USER modulatio
WORKDIR /home/modulatio
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
