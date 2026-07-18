# Durable Memory + Context Handling — Design

Status: **design only, not implemented.** Sequenced after the current release.
Scope: two related roadmap items — a durable memory layer, and reduced
context cost without loss of prompt fidelity. They share one substrate, so
they are designed together.

Guiding constraint: Modulatio ships to every user on any machine. No bundled
inference process, no assumed background daemon, no model-family assumptions,
no user-specific concepts baked into the product. "Learn from the
store→recall→consolidate pattern without importing its infrastructure."

---

## Part A — Durable memory layer

### Principle: unify what exists, do not build a new subsystem

Roughly 60% of the machinery already ships. The work is to unify three
existing fragments under one store→recall→consolidate spine and close two
specific gaps — not to introduce a parallel memory system.

Existing pieces:

| Piece | Module | Already does |
|---|---|---|
| Agent memory | `memory/agent_memory.py` | per-agent `episodic.json` (14-day decay, 100 cap) + `semantic.json` (50 cap, supersede/active-inactive), plain JSON |
| Team memory | `memory/team_memory.py` | markdown source-of-truth + LanceDB vector cache + embeddings + cosine recall + metadata pre-filter + propose→approve consolidation; orchestrator recalls before dispatch |
| QC history | `qc_history.py` | embedding-backed recall over the QC ledger |

The required embedding model (MiniLM / all-MiniLM-L6-v2, ~80MB, fastembed;
`config.get_embedding_model()`) already backs recall across all three.

### The gaps to close

1. **Leader conversational memory does not persist across threads.** The
   agent-memory machinery is producer/task-facing; the Leader-chat lane has no
   durable store of what it learned in conversation. `/new` resets everything.
2. **Recall does not survive compression.** A fact taught mid-conversation and
   held in-context is summarized away when the thread compresses; nothing
   re-anchors it.
3. **Cross-run agent memory rides the prompt** instead of being pulled on
   demand.

### The dreamer (consolidation / extraction)

Extraction is generative work — read raw material, judge salience, write the
distilled fact, decide what supersedes what. The embedding model cannot do it
(it vectorizes; it does not generate). Recall stays on the embedder; dreaming
needs a generative model.

- **Who.** Resolves through the existing capability router: a producer
  carrying a `memory-consolidation` skill if one exists, else the Leader (the
  only guaranteed generative seat). No new "dreamer" role — it is a skill any
  seat can carry. No operator pin knob: the roster the operator already manages
  *is* the selection. **Net-new config: zero.**
- **When.** At boundaries only — converse-end (including `/new`) and run-end.
  Never per-turn. The frequent operation (recall, every turn) stays on the
  cheap local embedder.
- **Cost ceiling (engine-bound, no loophole), role-dependent:**
  - **Leader dreams** → commits directly. No QC review (the Leader is the
    final authority; QC reviewing it would invert the hierarchy). **One call.**
  - **Producer dreams** → one terminal QC pass. **Two calls maximum.**
- **QC is a fixer, not a gate.** On a producer-drafted dream: approve →
  promote; disapprove → QC fixes in place → promote, or drops → gone. The
  verdict is terminal — **never handed back to the producer, no retries.** The
  no-handback rule is what makes the two-call ceiling a real invariant rather
  than a hope.
- **A dropped dream is not a loss.** The raw material persists in episodic +
  the audit trail; if the fact mattered, the next boundary re-surfaces it.
  Dreams get this stricter no-handback treatment (where deliverables keep their
  handback) precisely because a dream is a cheap, low-stakes candidate and its
  source persists.

The incentive falls out of the structure: the QC second call is exactly the
price of offloading the dream to a cheaper, unverified drafter. Pick the
Leader — trusted, one call, no tax.

### End state

One store→recall→consolidate spine: episodic (raw, decaying) → semantic
(durable, curated) with embedding-backed recall over both, plus the Leader's
conversational lane as a first-class client of the durable layer so what it
learns survives `/new`. Compression survival is addressed in Part B (the
durable store is where evicted content goes, retrievable verbatim).

---

## Part B — Context handling

### Two kinds of reduction; only one preserves fidelity

- **Lossy — summarize.** Crush old turns into a summary. Information is
  destroyed into the summary. This is today's compression path; it is the
  fidelity risk and is heavily tuned.
- **Lossless — relocate.** Move cooled content *out* of the resident prompt
  *into* the durable store, and pull it back verbatim only when a task touches
  it. Nothing is destroyed — only made non-resident.

"Reduce context without harming fidelity" = **relocate, do not summarize.** The
durable memory layer (Part A) is the retrievable store that makes lossless
context reduction possible. Compression becomes a rare last resort for the hard
window limit, not a frequent cost-saver — because (a) cache-stable ordering
removes the cost pressure, and (b) eviction-to-store removes the size pressure
losslessly.

### Cache-stable ordering ("stable up top, volatile at the end")

Providers cache a *prefix* of the prompt and reuse it up to the first token
that differs; everything after that first difference re-bills at full price.
Therefore: **stable content first, volatile content last.**

- Volatile blocks (fresh recall, latest turn) placed at the end keep the large
  stable prefix (system, skills, durable memory, prior turns) cached at the
  reduced read rate.
- The volatile-at-the-end placement is *also* attention-optimal: models attend
  hardest at the top and the very bottom ("lost in the middle"). Fresh recall
  wants to be read hard and wants to be cheap — both point at the end. No
  conflict.
- Recall does double duty: the full durable corpus sits cached-cold in the
  prefix, and the turn-relevant slice is re-surfaced into the salient tail.

Cost note: re-billing a small stable block (e.g. the cognitive discipline) at
the salient end is negligible — input tokens are far cheaper than output, and
the adherence gain avoids output-heavy rework. Placement of small important
blocks should favor salience over prefix-caching.

### Push → pull (feed the index, not the library)

Reduce tokens-in by changing *what* is sent, not by re-encoding it. Feed a seat
a compact pointer (`artifact://<id> §<n>`) and let it dereference on demand via
recall, rather than pasting the full payload. Most seats never load most
content because most seats do not need it. This is a protocol-layer move
(references, structured state, task-relevant slices), explicitly **not** an
encoding-layer move (no shorthand, no dense-image/optical text encoding —
lossy, vision-only, model-specific; rejected).

### The runbook-at-the-end economics

Moving the cognitive discipline to the salient tail is expected to: raise
first-pass adherence → reduce rework (avoided rework is output-token-heavy) →
reduce QC fixing pressure. The re-bill cost is small input tokens; the savings
are avoided output-heavy attempts. Net: fewer, better attempts; each attempt
slightly heavier (more diligent verification); **faster to a satisfying
deliverable** even if a single attempt in isolation is marginally slower.

The second-order effect — more diligent verification increases per-attempt
context load — is real and bounded, and is absorbed by eviction-to-store:
verification material is volatile (verify → keep the receipt → evict the blob →
recall only if challenged). The reorder creates diligence; eviction contains
it. The two halves compose.

---

## Current-state audit (read-only, 2026-07-18)

Prompts assemble as a five-layer sandwich (`_docs/17-working-memory.md`): the
terse, drift-gated skill template renders; volatile slots
(`{team_memory_context}`, `{team_state}`, `{repo_map}`, `{corrective_notes}`)
interpolate at fixed positions *inside* the template body; then compression
preflight, tool loop, tool-result summarization, and state write-back.

Findings:

1. **Volatile slots sit mid-prompt, interleaved with stable content.** In the
   producer template the recall pull and team-state are buried in the middle —
   both cache-busting and lost-in-the-middle.
2. **The cognitive discipline is mid-template, not in the salient tail.**
3. **These slot positions are inside the tuned, drift-gated templates.**
   Capturing the reorder therefore edits tuned structure that drift-gate tests
   pin — it is not a free external reorder.

Lower-risk target: the conversational-Leader runtime assembly
(`system + history + recall`) is concatenated at runtime and may sit outside
the drift-gated templates; if recall is prepended there, moving it after
history is a cleaner, lower-risk win. To be traced fully during planning.

---

## Risk posture — the headroom shock absorber

The engine was tuned to operate in tight (8–16K) windows, then given
appropriate ceiling over time — models run with slack while retaining the
frugal, "tunnel-crawling" discipline internalized under pressure. This absorbs
the change for three reasons:

1. **Size risk is eliminated.** The reorder moves blocks, it does not add them;
   with slack, no move overflows or forces an unplanned compression.
2. **The discipline is behavioral, not positional.** It is internalized in how
   the templates instruct, not in a slot's byte offset, so reordering within
   the window does not threaten it.
3. **The reorder direction is monotonically favorable** — mid-prompt → salient
   tail can only improve or leave adherence unchanged.

Caveat that remains: headroom removes the size risk and makes the behavioral
shift safer, but the shift is the point, so it earns validation.

---

## Hard boundary

- **Audit + marginal reordering, not prompt rewriting.** The *content* of every
  tuned prompt (kickoff/decompose, producer contracts, QC, terse-prose
  conventions) stays byte-for-byte unchanged. Only block/slot *order* changes.
- **Reordering changes attention, which changes behavior.** Every reorder is
  A/B-validated against the live-test corpus and the suite; drift-gates are
  updated deliberately, never bypassed.
- **Additive levers first.** Stable-prefix ordering and eviction-to-store sit
  beside the tuned compression path; they make it fire *less often* rather than
  re-opening its tuning.

## Sequencing (after the current release)

1. Full runtime prompt-assembly trace (extend the read-only audit to the
   Leader-converse lane and every runner).
2. Memory unification: one spine over the three existing fragments; Leader
   conversational lane as a durable-layer client (closes gap 1).
3. Eviction-to-store + recall-on-demand (closes gaps 2–3; enables lossless
   context reduction).
4. Cache-stable ordering + runbook-at-tail, per-template, each A/B-validated,
   drift-gates moved deliberately.
5. Push→pull pointer feeding where payloads are large and task-relevance is
   partial.

## Explicitly rejected

- A bundled background dreamer model (violates the no-infrastructure
  constraint; consolidation rides user-configured seats instead).
- Optical / image-encoded text context (lossy, vision-only, model-specific).
- Any user-specific domain concept as a first-class product notion (agnostic
  rule).
- Re-tuning or rewriting the tuned prompt content (boundary above).
