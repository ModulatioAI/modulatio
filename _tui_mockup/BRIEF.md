# Brief — reimagined Modulatio CONSOLE mockup (Textual)

Generate ONE runnable Textual mockup of the Modulatio "CONSOLE" screen, in the aesthetic and
layout below. **Static fake data only** — this is a *look-and-feel* mockup to view the design, not
wired to anything. Python 3.12, the `textual` library, no network, no external deps beyond textual.

## Hard aesthetic — non-negotiable
- **Pure black background** (`#000000`) everywhere — the black IS the breathing room.
- **Thin SINGLE-line borders only.** Use Textual `border: round <color>` or `border: solid <color>`
  — NEVER `heavy`, `double`, `thick`, or block-fill panels. Frames must read as fine 1px lines.
- **Monochrome accent on black.** One accent color live at a time; hierarchy comes from
  **brightness** (bright accent = focal, dim/grey = secondary), not from many colors.
- Monospace, small uppercase-ish labels for secondary text; lots of dim grey text; a few bright
  accent elements only.

## Theme system (the one interactive bit)
Three phosphor themes, swap the accent only (black + thin lines constant). Bind a key (`t`) to
**cycle** them, and show the active theme name somewhere small:
- **amber** (DEFAULT) — warm amber/gold, e.g. accent `#FFB000`, dim `#7a5a12`.
- **green** — phosphor green, e.g. accent `#33FF66`, dim `#1f7a3a`.
- **cyan** — cool cyan, e.g. accent `#33E0FF`, dim `#1f7a8a`.
Implement via Textual CSS variables (a `$accent` / `$accent-dim` var set you reassign on theme
change), so the swap is one source of truth — NOT hardcoded per widget.

## Layout (feng-shui composed — match this structure)
A single screen, thin-framed, top to bottom:

1. **Header bar** (one thin row): `MODULATIO` (accent, left) · ` anthology · goal 2 of 4 ` (dim,
   center) · ` 14:38 ` (dim, right).
2. **Status lamp row** (dim, glyph+WORD each — never color alone):
   `● leader   ◇ 3 mods · 1 qc   ▸ running   ⚑ 1 ticket   ⛁ 18.2k tok   ◷ 02:41 elapsed`
3. **Flip-tab indicator** (thin): `LEADER ╶╴ mod squad` — LEADER active (accent), mod squad dim.
4. **Body = two columns** under a thin divider:
   - **LEFT rail (narrow, ~22 cols) — "RUN" telemetry**, all dim except current values:
     `goal ▰▰▱▱ 2/4`, `tasks ▰▰▰▱ 6/8`, `qc ▰▱▱▱ 1/3`, then `tokens 18.2k`, `cost $0.04`,
     `model glm-4.6`, then a `─ producers ─` list: `◆ nemo draft`, `◆ ren draft`, `◆ ada idle`,
     `◈ qc wait` (glyph + name + state word).
   - **CENTER (wide) — the streaming "TV" = THE FOCAL POINT / DOORWAY.** Brightest text on screen.
     A few lines of mock Leader prose then action lines, then a live block cursor:
     ```
     I'll open with the framing essay, then fan the six pieces out to the
     squad in parallel. Drafting the outline now.

     ▸ decompose · 6 tasks queued
     ▸ assign · pieces 1–6 → producers
     ▎
     ```
     Surround it with generous empty black rows (breathing room). Divider between rail and TV is a
     LIGHT/dim thin rule, not a heavy one.
5. **Status line** (one thin row, dim): `› leader is drafting the outline…            ▏ 00:42`
6. **Input box** (thin frame): an Input placeholder `talk to the leader…`, and below it a dim
   affordance line: `enter ▸ send   ·   /kickoff <objective> ▸ run a job   ·   ⇥ flip to mod squad`.

## Feng-shui rules to honor
- ONE doorway: the center stream is the single brightest thing; everything else dim. Spend the
  accent sparingly (focal stream + the one "running" lamp).
- Clean top→bottom eye path; left rail is a calm sidebar, not a competitor.
- Generous black breathing room around the focal stream.
- Asymmetric balance: heavy bright stream vs calm dim rail + open black.
- Every state = glyph + word (accessibility: readable with color off).

## Output
Write the runnable mockup to **`/home/cknox/modulatio/_tui_mockup/console_mockup.py`** (CSS inline
via `CSS = """..."""` is fine, or a sibling `console_mockup.tcss`). It must launch with
`python /home/cknox/modulatio/_tui_mockup/console_mockup.py` and render the screen above with
working theme-cycling on `t` and quit on `q`. Keep it ONE focused file; no TODOs, no placeholders —
real (if fake-data) widgets. After writing, confirm the file exists on disk.
