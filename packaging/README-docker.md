# Modulatio in Docker

The container runs the complete Modulatio: the **WebOS** (browser interface),
the **TUI** (in your terminal, locally or over SSH), the **CLI**, and every
provider path — cloud APIs, **local model servers on the host** (Ollama /
LM Studio), and **Clay** (Claude Code subscription seats). The image is the
same self-contained `.deb` the package channel ships, installed into a
minimal Debian base.

Everything below is enabled by the default `docker-compose.yml`. Nothing
needs a rebuild — all configuration is volumes, keys, and sign-ins.

---

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/ModulatioAI/modulatio/main/packaging/docker-compose.yml
docker compose up -d
docker exec -it modulatio modulatio setup     # first-run wizard
```

Then open the WebOS at `http://localhost:8787/`.

Two services come up:

| Service | What it is |
|---|---|
| `modulatio` | the WebOS server (`modulatio-api`, loopback-bound by default) |
| `modulatio-ssh` | the TUI-over-SSH door on port 2222 (inert until you authorize a key — see below) |

Both share one named volume (`modulatio-home`) holding all state: your
config, projects, credentials, and Clay's sign-in. `docker compose down`
never loses your work; `docker volume rm modulatio-home` is the only thing
that does.

---

## The WebOS (browser)

Served on port **8787**, **bound to loopback by default** (the compose file
uses host networking, so that's the host's own `127.0.0.1:8787`). From the
docker host, open `http://localhost:8787/` — nothing else needed.

To reach it from **another machine**, bind all interfaces explicitly — set the
service command to `["api", "--host", "0.0.0.0"]`. Two things to know before you
do: the transport is **plain HTTP**, so the bearer token and your project
traffic cross the network unencrypted — put a **TLS reverse proxy in front**
for any real remote use. And the server's Host allowlist admits the bound name
(e.g. `0.0.0.0`), which does not match an ordinary browser `Host:` header — so
remote browsers reach it by IP/host only when you add that host to the
allowlist. Loopback + an SSH tunnel, or a TLS proxy, is the recommended remote
path.

The bearer token for non-loopback access is printed to the container log on
first bind (`docker logs modulatio`).

Change the port by editing the service command: `command: ["api", "--port", "9000"]`.

## The TUI

Two doors, both always available:

**From the docker host:**

```bash
docker exec -it modulatio modulatio-tui
```

**Over SSH — the session IS the TUI.** Authorize your key once:

```bash
docker exec -i modulatio sh -c 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub
```

then from anywhere that can reach the box:

```bash
ssh -p 2222 modulatio@<host>
```

You land directly in the TUI; closing it ends the session. Key-only
authentication (passwords are disabled); with no key authorized the door
accepts nobody.

## Cloud model providers

Work out of the box — add models and keys in the wizard, the TUI CONFIG tab,
or the WebOS CONFIG tab exactly as on a host install. Keys live in the
volume, not the image.

## Local model servers (Ollama, LM Studio, llama.cpp)

With the default **host networking**, the container sees the host's
localhost: `http://localhost:11434` (Ollama) and `http://localhost:1234`
(LM Studio) work unchanged in the provider config.

**Docker Desktop (macOS/Windows)** has no host networking: switch the
services to the bridge block commented in the compose file, and point local
provider URLs at `http://host.docker.internal:<port>` instead of localhost.

## Clay — Claude Code subscription seats

The image bundles the `claude` binary, so Clay seats work in the container
after a **one-time sign-in** (state persists in the volume). Two ways:

- **Interactive** (host networking lets the login's loopback callback work):

  ```bash
  docker exec -it modulatio claude
  ```

- **Headless**: run `claude setup-token` on any machine with a browser, then
  put the token in the compose file's `CLAUDE_CODE_OAUTH_TOKEN` environment
  entry and `docker compose up -d` again.

Prefer not to sign in? Anthropic models also run through plain API keys
(Anthropic direct, or OpenRouter) — configure them like any other provider.

## In-app OAuth sign-ins (xAI, OpenAI subscription)

The add-model picker's in-app sign-in flows work in the container:

- **OpenAI (device code)** — needs nothing special; the picker shows a code
  and a URL you open anywhere.
- **xAI (loopback consent)** — the consent page redirects to a loopback port
  on the machine running your browser, so complete it from a browser **on
  the docker host** (host networking makes the callback land). On a remote
  or Desktop setup, use the `api_key` method instead.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

State rides the volume; the new image picks it up where the old one left off.

## Building the image yourself

From a repo checkout with the `.deb` built (`packaging/build_deb.sh`):

```bash
docker build -f packaging/Dockerfile --build-arg DEB=dist/modulatio_<version>_amd64.deb \
  -t ghcr.io/modulatioai/modulatio:latest .
```
