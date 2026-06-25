# Downloadable offline docs

Make the DOCS tab fetch the *current* published docs once (when online), cache
them, and read them offline thereafter — so a user always has the full, latest
documentation without internet, even if their installed version lags.

Built **/code-nerd**: reuse `modulatio.docs`, the tab's existing `r` action,
`config.CONFIG_DIR`, and the site deploy we already run. stdlib only
(`urllib` / `tarfile` / `hashlib` / `json`). No new module, no new dependency.

## Why / what's wrong

`src/modulatio/_docs/` ships **5 hand-written summary pages (~1,200 words)** — a
separate copy of the docs, with no sync to modulatio.ai (~29 pages, ~50k words).
Two problems: **coverage** (the tab has ~2% of the real docs) and **drift** (a
second hand-maintained copy rots). Both vanish if the offline docs are *the
website's docs*, downloaded and cached: one source of truth, always current.

## Design — glue over existing primitives

Three small pieces; the DOCS tab (`screens/docs.py`) barely changes because it
already reads through `docs.list_docs()` / `docs.read_doc()`.

### 1. `modulatio.docs` — read from cache, else the install baseline

The one structural change: `_DOCS_ROOT` (a constant) becomes `_docs_root()` (a
function). It returns the **cache** dir when it holds a valid `manifest.json`,
else the **install baseline** `_docs/`. `list_docs` / `read_doc` call it. The
tab is unchanged.

```
_CACHE_ROOT = config.CONFIG_DIR / "docs-cache"     # uninstaller-owned

def _docs_root() -> Path:
    return _CACHE_ROOT if (_CACHE_ROOT / "manifest.json").exists() else _DOCS_ROOT
```

### 2. `docs.update_docs() -> str` — fetch + verify + cache (same module)

The only real new code. Online → GET the remote `manifest.json`, compare its
`version` to the active manifest; if newer, download the bundle, **verify the
sha256** against the manifest, extract to a temp dir, and `os.replace` it into
`_CACHE_ROOT` (atomic swap — a half-download never replaces good docs). Returns
a status line for the tab (`"Updated to v0.9.9"` / `"Already current"` /
`"Offline — keeping the docs you have"`). Every failure path **keeps the
existing docs** and returns a message; it never raises into the UI or blanks the
reader.

- Transport: `urllib.request` with a short timeout (same pattern as `embed.py`,
  the GitHub-API calls).
- Integrity: `hashlib.sha256` over the downloaded bytes vs the manifest. HTTPS +
  a content checksum is the bar — **no signing/crypto** (YAGNI; the bundle is
  public docs, not code).
- Unpack guard: reject any tar member whose resolved path escapes the temp dir
  (the `attachments.py` / vault path-safety idiom — fail closed on traversal).

### 3. Build/release — generate the bundle from the site (`scripts/build_docs_bundle.py`)

One script, run at release time. Converts the site's `src/content/docs/*.mdx`
→ plain `.md`, writes:
- `src/modulatio/_docs/*.md` — the **install baseline** (so the tab works offline
  out of the box, pre-download), replacing today's hand-written pages.
- `manifest.json` + `docs-bundle.tar.gz` into the site's `public/docs/offline/`,
  which the existing `deploy_ftp.py` mirrors to `modulatio.ai/docs/offline/`.

**MDX→MD converter** (the focused part): the site MDX is plain markdown plus a
thin Starlight layer. Handle exactly what's used — strip the `---frontmatter---`
(keep `title` as the `#` heading) and `import …` lines, and turn
`<Aside type="…" title="X">…</Aside>` into a `> **X**` blockquote. Line-based,
no MDX/JSX parser. **Scope:** the substantive sections (getting-started,
concepts, architecture, operations, reference, troubleshooting, methodology,
overview). **Excluded:** the per-version `v0-*-*.mdx` release-notes (historical;
they stay online) and `roadmap` (lives online, changes constantly).

`manifest.json`:
```
{ "version": "0.9.9", "generated": "2026-…",
  "bundle": "docs-bundle.tar.gz", "bundle_sha256": "…",
  "pages": [ { "slug": "02-getting-started", "title": "…" }, … ] }
```

## Entry points (UX: button + key)

Both routes call one method (`docs.update_docs()` → reload nav → set the status
line); the button is the discoverable face, the key the fast path.

- A **`⟳ Update docs` Button** (`id="docs-update"`) in the list pane's controls
  area. `on_button_pressed` → `_do_update()`.
- The **`r` key** (today "Refresh") → the same `_do_update()`. Affordance text
  `r refresh` → `⟳ update`.
- A **status line** (a `Static`, reuse the existing `#docs-affordance` slot or a
  sibling) shows the one-line result: `Updated to v0.9.9 ✓` / `Docs are up to
  date.` / `Offline — showing the docs you have.`
- The download runs on a worker (`@work` / `run_worker`) so the UI never blocks;
  on done, reload `list_docs()` + set the status. `o open online` unchanged.
- `modulatio docs --update` — out of scope unless trivial via the existing CLI
  group; the button + key are enough.

## Security / fail-safe (the load-bearing bits)

- **Verified download** — sha256 vs manifest; mismatch → discard, keep current.
- **Atomic swap** — `os.replace` of a fully-extracted temp dir; no partial state.
- **Traversal-safe unpack** — reject tar members resolving outside the temp dir.
- **Offline-safe** — any network/parse error returns a status and leaves the
  reader on the cached/baseline docs; the tab never blanks.
- **Uninstaller** — `docs-cache/` under `CONFIG_DIR` is removed by
  `--remove-settings`/`--pristine` (already in scope), and passes `assert_safe`.

## Tests

- converter: frontmatter/import strip + `<Aside>`→blockquote on a fixture mdx.
- `_docs_root()` precedence: cache-with-manifest wins; else baseline.
- `update_docs()`: newer manifest → swaps; same version → no-op; **bad sha256 →
  keeps old docs + status**; offline (urlopen raises) → keeps old + status;
  traversal tar member → rejected.
- DOCS tab smoke: the `⟳ Update docs` button **and** the `r` key both run the
  update + reload without raising (offline path), and set the status line.

## Out of scope (YAGNI)

Auto-update on launch / background polling; incremental per-page sync (whole
bundle is small); a version picker or docs-history; signed bundles. Add only if
a real need shows up.

## Effort / sequencing

A real feature, not a patch — its own arc: build the script + converter, extend
`docs.py`, wire the tab, test, then a code cadre. Targets **0.9.9** (or folds
into the 1.0 docs push). The install baseline regen also fixes today's stale
5-page bundle as a side effect.
