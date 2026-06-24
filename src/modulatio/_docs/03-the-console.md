# The CONSOLE

The CONSOLE is where you drive the work. It flips between two live views:

- **LEADER** — the Leader's prose and action stream, with a chat composer
  below. Talk to the Leader here; it answers, and reports a verdict back here
  when a job finishes.
- **MOD SQUAD** — the factory floor: the squad working live (producers drafting
  in parallel, QC reviewing), with a **KICK OFF** box to launch a job.

A row of **status lamps** sits above the flip, readable from either view:

```
● leader   ◇ N mods · M qc   ▸ running   ⚑ N tickets   ⛁ tok   ◷ elapsed
```

The lamps track the run you started in this session. When the Leader has a
verdict for you, or a ticket is logged while you're watching the factory floor,
the relevant lamp **blinks** to catch your eye — and rests once you flip back to
LEADER and read it.

## Talking vs. launching

Conversation lives on LEADER; job launches live on MOD SQUAD. They're kept
apart on purpose, so a kickoff is never a fat-finger away mid-conversation. The
Leader can also launch a job itself from the chat when you ask for work that
warrants the full squad.

## Stopping a run

Use the stop key to signal a running job to halt. It stops at the next safe
point — finishing the in-flight step rather than corrupting partial output.
