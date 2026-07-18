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
# Installed into a NON-volume image path (/opt/claude) and symlinked onto PATH,
# so the binary survives the state-volume mount over /home/modulatio and image
# upgrades. Only login state (~/.claude) rides the volume. Sign in once with
# `docker exec -it <name> claude`, or supply CLAUDE_CODE_OAUTH_TOKEN from a
# `claude setup-token` run done on any machine with a browser.
# The installer drops the binary at ~/.local/share/claude/versions/<ver> with
# ~/.local/bin/claude an absolute symlink to it. Move the app tree to /opt
# (non-volume, world-readable so the modulatio user can exec it) and symlink
# the versioned binary onto PATH; the ~/.local leftover is discarded.
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && mv /root/.local/share/claude /opt/claude \
 && ln -sf "$(ls -d /opt/claude/versions/* | head -1)" /usr/local/bin/claude \
 && chmod -R a+rX /opt/claude \
 && rm -rf /root/.local \
 && /usr/local/bin/claude --version
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
