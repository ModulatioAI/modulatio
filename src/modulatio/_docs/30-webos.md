# The WebOS

The WebOS is the TUI's layout rendered in a browser — one new surface
over the same engine, hooked straight to the seams the terminal already
uses. Where the TUI has a tab, the web has a page; where the TUI
receives a pushed event, the web reads a Server-Sent-Events stream.

## Install and launch

The web server is an opt-in extra, so the base install stays lean:

```bash
pip install "modulatio[web]"
modulatio-api            # serves http://127.0.0.1:8787
```

Without the extra, `modulatio-api` prints the install hint and exits —
it never tracebacks at launch.

## What you get

- **The Console** — the centerpiece. The status lamp row, the
  LEADER / MOD SQUAD flip (F4), the live TV fed by the engine's
  activity events (the same glyph + verb vocabulary as the terminal),
  the run-telemetry rail (task gauge, QC tally, context tokens), and
  the composer. `/kickoff … /end` brackets are the only job trigger —
  the **Kick off** button just pre-fills them. **F8** stops a run,
  with confirmation.
- **The pages** — JT Library, Tickets, Artifacts (with previews),
  Skills, Memory, Jobs, Cron, Logs and Docs, each a list + detail over
  the same data the TUI tabs read.
- **Approvals** — when the Leader asks permission for an out-of-scope
  action, the request lands as a modal. No decision within the window
  means **deny**: approvals fail closed, exactly like the terminal.

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
