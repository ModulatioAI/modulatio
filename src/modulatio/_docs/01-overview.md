# Overview

**Modulatio** is a multi-model agent orchestration tool. You give it an
objective; a **Leader** agent plans the work, breaks it into tasks, and hands
each to a **producer** agent best suited to it. A **QC** agent reviews the
output and, when it falls short, fixes it in place rather than throwing it away.
The result is assembled into the artifacts you asked for.

Everything runs locally if you want it to — Modulatio is built to drive local
models with no internet — so this documentation ships **bundled in the install**
and is readable offline, right here in the DOCS tab.

## The shape of the system

- **Leader** — orchestrates a run: plans goals and tasks, assigns them, and
  reviews the result. It's also a standalone coding/answering agent you can just
  talk to on the CONSOLE.
- **Producers** — do the work. There are no fixed roles; a producer *is* its
  skills. The engine capability-matches each task to a skill and loads it onto
  whatever producer is best placed to run it.
- **QC** — reviews finished work against the task's contract and recovers it
  when it misses, instead of leaving it dead.
- **The Mod Squad** — the collective name for your agent team.

## Where things live

Your work is stored in a **project vault** on disk. A project holds its agent
roster, skills, memory, and one folder per **run** (a "job"): each run keeps its
own objective, goals, tasks, decisions, tickets, research, artifacts, and
reports. Memory persists at the project level, accruing across runs.

See **Getting started** next, then **The CONSOLE** for how to drive a run.
