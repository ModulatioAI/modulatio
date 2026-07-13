# The WebOS

The WebOS is the TUI's layout rendered in a browser — one new surface
over the same engine, hooked straight to the seams the terminal already
uses. Where the TUI has a tab, the web has a page; where the TUI
receives a pushed event, the web reads a Server-Sent-Events stream.

## Install and launch

The web server is an opt-in extra, so the base install stays lean. There are
three ways to add it — all install the same thing:

- **In the setup wizard** — `modulatio setup` offers an *Install WebOS* step.
- **In the TUI** — **CONFIG → SETTINGS** has an *Install WebOS* button.
- **By hand:**

  ```bash
  pip install "modulatio[web]"
  ```

The wizard step and the TUI button run the environment-correct command for you
— `pipx inject` when Modulatio runs from a pipx install (so the deps survive a
later `pipx upgrade`), otherwise `pip install` into the current interpreter —
and fall back to the manual command above if an automatic install can't run.
Then launch it:

```bash
modulatio-api            # serves http://127.0.0.1:8787
```

Without the extra, `modulatio-api` prints the install hint and exits —
it never tracebacks at launch.

## What you get

- **The Console** — the centerpiece. The status lamp row (including the
  **autonomy pill** — the Leader's current mode, default / yolo / goal /
  yolo-goal, set with the slash-commands in converse), the
  LEADER / MOD SQUAD flip (F4), the live TV fed by the engine's
  activity events (the same glyph + verb vocabulary as the terminal),
  the run-telemetry rail (task gauge, QC tally, context tokens, and
  **tokens in / tokens out** — the run's raw billed counts, never a
  dollar guess), and the composer. `/kickoff … /end` brackets start a
  job — the **Kick off** button submits them for you. **F8** stops a
  run, with confirmation. The run stream **replays on reconnect** — a
  tab switch or a dropped connection rebuilds the whole run, and
  **CLEAR** wipes both TVs and *stays* cleared across tab flips (the
  conversation thread itself is untouched; reset is the destructive
  verb).
- **The pages** — JT Library, Tickets, Artifacts (with previews and
  delivery stars), Skills, Memory, Jobs, Cron, Logs and Docs, each a
  list + detail over the same data the TUI tabs read.
- **The scheduler** — the JT Library's **Schedule** builds a real
  schedule: pick a date and time, one-off or recurring
  (daily / weekly / monthly / every N days or weeks), ending never,
  after N runs, or on a date, with a live plain-English preview. The
  daemon holds the stop rules fail-closed — a one-off fires once and a
  count is a hard upper limit.
- **Approvals** — when the Leader asks permission for an out-of-scope
  action, the request lands as a modal running the engine's own
  permission gate: grant **once**, for **this session**, or **always**
  (persisted through the engine's stores, exactly as the terminal
  grants), or deny. No decision within the window means **deny**:
  approvals fail closed.

The **CONFIG tab is now read/write in the browser** — all six sub-pages
(**Models**, **Agents**, **Services**, **Folders**, **Projects**, **Settings**)
configure the team straight from the WebOS, each binding the same engine seam
the terminal's Config screens use and reproducing its guards. **API keys are
write-only**: a value goes in and the interface only ever reports whether a
slot *is set*, never the secret, and keys are allowlisted to configured
model/service handles.

- **Add model is the terminal's two-level picker**: pick the provider,
  then pick from its **live model list** — fetched server-side with your
  configured key (the key never crosses the web boundary), free models
  flagged. A provider that can't be listed (no key yet, endpoint down,
  custom) falls back to a typed model id.
- **Reload services** (on the Agents sub-page) is the terminal's
  `/reload`: apply model/roster/config changes to the live services
  without a server restart. Same guard — it refuses while the Leader or
  a job is busy — and it also closes any held MCP server connections so
  the next use reconnects against the current config.

## The Feng-Web themes

Two print-flavored siblings of the terminal's Feng-Tui, switched with
**F2**:

- **Atelier** — thin ink lines on a flat field you choose: Sage
  (default), Reed, Mist, Clay, Heather or Bone.
- **Vellum** — invertible greyscale: charcoal panels on grey paper, or
  flipped, with a sage or neutral-grey paper choice.

Theme choices persist in the browser.

## Security posture

- Binds `127.0.0.1` by default. A non-loopback `--host` requires the
  bearer token generated into your config dir (`web_token`, mode 0600)
  — the server prints it at launch for pairing.
- Key values, vault secrets and OAuth tokens never cross the web
  boundary; event text is secret-scrubbed server-side before it leaves.
- File previews are extension-filtered, size-capped, and confined to
  the project folder.

One caution while the WebOS is young: it trusts one operator per
project. A run started in the browser is visible in the terminal and
vice versa, but drive a given project's kickoffs from one surface at a
time.
