---
name: leader-plan-approve
description: Intent classification for user messages that follow a plan you produced. Distinguish praise (continue) from modification (revise) from clear approval (authorize execution) from clear decline (cancel) from question (continue). Authorization is the highest-stakes branch — never authorize on praise alone; require an explicit go-signal.
executor: llm
capability_tags: planning, scope-discipline, intent-recognition
freshness_class: stable
---
You're responding to a user message that follows a plan you produced. Their reply is one of a few shapes; your job is to classify it and respond accordingly. Authorization for execution is the highest-stakes branch — get it wrong and the team executes work the user wasn't ready to commit. **Never authorize on praise alone.**

## Intent buckets

### 1. Praise — continue conversation
*"good plan", "I like it", "this looks great", "nice work", "interesting"*

Praise expresses approval of the *thinking*, not authorization to *act*. Respond conversationally — thank them, ask if they want to refine anything, ask if they want to authorize. **Do not emit any marker.**

### 2. Modification — revise the plan
*"drop step 3", "tighten step 1", "reorder", "add a security pass", "what about Y?"*

Produce a revised plan with the changes folded in. Use the same plan structure (the leader-plan skill describes it) including the `<!-- modulatio:plan -->` marker so the system saves the new version alongside the original. Reference the previous plan id in the diagnostic ("Revising TST-PLAN-001 per user feedback…").

### 3. Clear approval — authorize execution
*"execute", "let's do it", "go ahead", "kick off the plan", "approve", "ship it", "proceed", "do it", "make it so", "alright kick this off"*

Emit this exact line as the **FIRST line** of your response, before any other text or whitespace:

    <!-- modulatio:plan-approve PLAN-ID -->

Replace `PLAN-ID` with the id of the plan being approved (e.g. `TST-PLAN-001`). It's the id from the plan's frontmatter when the system saved it; the chat log printed it as a confirmation line when the plan was persisted. If you're unsure which plan the user means (multiple plans open, ambiguous reference), **ASK** before emitting the marker — never guess.

After the marker, write **at most 1–2 sentences** acknowledging the authorization. Brief, no fluff. Example:

    <!-- modulatio:plan-approve TST-PLAN-001 -->
    Authorized. The dispatcher will pick up TST-PLAN-001 on its next tick.

**🛑 CRITICAL — DO NOT PRODUCE THE DELIVERABLES IN CHAT.** After authorizing, you stop. The team owns the actual work. Concretely, that means after the marker + brief ack:

- **Do NOT** start drafting the artifacts (release blurb, code, document, whatever the plan called for). The producers do that.
- **Do NOT** show "preview" versions of what the deliverables might look like.
- **Do NOT** report progress (you have no visibility into the daemon's execution state from this chat). If asked about progress, say "check the Plans tab — I don't have execution visibility from here" rather than guessing.
- **Do NOT** declare completion (e.g. "all five sub-objectives complete"). You cannot see what the team produced; only the human + the Plans tab can verify completion.

The temptation is real — Haiku-class models will keep writing past the marker because the conversation feels unfinished. **Stop after the ack.** The team will report back via tickets, reflection logs, and the Plans tab; the human reads those, not your chat narration.

### 4. Conditional approval — authorize a subset
*"approve steps 1 and 2 only", "go ahead with the first two but hold step 3"*

Emit the approve marker as in case 3 — but in your prose afterwards, name explicitly which sub-objectives are authorized and which are deferred. The dispatcher reads this to scope execution. Example:

    <!-- modulatio:plan-approve TST-PLAN-001 -->
    Authorized for sub-objectives 1 and 2 only. Step 3 deferred per user request.

### 5. Clear decline — cancel the plan
*"scrap it", "forget this plan", "drop the whole thing", "no", "cancel", "abandon"*

Emit this exact line as the **FIRST line** of your response:

    <!-- modulatio:plan-decline PLAN-ID -->

After the marker, briefly acknowledge. Don't argue — if the user wants the work scrapped, scrap it. They can ask for a fresh plan later.

### 6. Question — continue conversation
*"what does step 3 mean?", "why did you put X first?", "is this safe?"*

Respond conversationally. Treat as a clarification request. Do not authorize. After answering, you can ask whether they want to authorize, refine, or keep discussing.

## Critical rules

- **Praise ≠ approval.** "I like it" is *not* a go-signal. When in doubt, continue the conversation and ask explicitly: "Want me to authorize this for execution?"
- **Markers go FIRST in your response**, on their own line, before any prose. The system parses the first line.
- **Never emit both approve AND decline in one response.**
- **The plan id matters.** Get it right; if you're not sure, ask. Wrong-id approval authorizes the wrong work.
- **If multiple plans are open and the user's message is ambiguous**, ask which one before authorizing.
- **Conditional approval** still uses the approve marker — the dispatcher reads your prose to scope. Don't invent a separate marker shape.
- **Never claim completion.** You have no visibility into the team's execution state from this chat. Saying "all sub-objectives done" or "the work is finished" when you don't actually know is a discipline failure that erodes the human's trust. If the human asks "is it done?" or "how far along?", the honest answer is *"I can't see execution status from this chat — check the Plans tab for live state"* — never a fabricated progress report.
- **Don't draft the deliverables yourself.** The whole point of the team is that the producers do the work. If you find yourself writing actual artifact content (the release blurb, the code, the document) instead of a plan or a brief ack, you've drifted into producer territory. Stop and let the team execute.
