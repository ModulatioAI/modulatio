# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
#
# The Modulatio container — the same self-contained .deb the packaging
# pipeline ships, installed into a minimal Debian base, plus Claude Code
# (for Clay seats) and an opt-in sshd whose sessions ARE the TUI.
#
# Build (from the repo root, with the .deb already in dist/):
#   docker build -f packaging/Dockerfile --build-arg DEB=dist/modulatio_<ver>_amd64.deb -t modulatio .
#
# Run: see packaging/docker-compose.yml — host networking is the default so
# local model servers (Ollama / LM Studio on the host's localhost) and the
# OAuth loopback work unchanged; state lives in mounted volumes.

FROM debian:stable-slim

ARG DEB=dist/modulatio_amd64.deb

# ca-certificates: outbound TLS (cloud providers). curl: the Claude Code
# installer + healthchecks. openssh-server: the opt-in TUI-over-SSH door
# (not started unless the compose ssh profile runs it). git: Clay and the
# solo-Leader coding lane expect it.
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

# Claude Code (native build, self-contained) so Clay seats work in-container.
# Installed for the runtime user below; login state persists in the mounted
# /home/modulatio/.claude volume (or supply CLAUDE_CODE_OAUTH_TOKEN from a
# `claude setup-token` run done on any machine with a browser).
RUN useradd --create-home --uid 1000 --shell /bin/bash modulatio
USER modulatio
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && /home/modulatio/.local/bin/claude --version
ENV PATH="/home/modulatio/.local/bin:${PATH}"
USER root

# TUI-over-SSH (opt-in): key-only, single user, and the session IS the TUI —
# ForceCommand drops the client straight into modulatio-tui; closing the TUI
# closes the session. Enabled only when the compose `ssh` profile runs this
# image with the `ssh` command; authorized_keys arrives via the state volume.
RUN mkdir -p /run/sshd && printf '%s\n' \
      'Port 2222' \
      'AllowUsers modulatio' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PermitRootLogin no' \
      'ForceCommand env TERM=xterm-256color modulatio-tui' \
      > /etc/ssh/sshd_config.d/modulatio-tui.conf

COPY packaging/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

USER modulatio
WORKDIR /home/modulatio
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
