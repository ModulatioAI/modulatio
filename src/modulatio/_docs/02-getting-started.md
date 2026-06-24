# Getting started

## 1. Configure a model

Open the **CONFIG** tab → **MODELS**. Pick **+ Add model**, choose a provider,
supply its key (or use a local endpoint that needs none), and pick a model. The
provider, endpoint, and auth are filled in for you — you only paste the key.
Keys are stored in your vault, never in plain presets.

Running fully local? Point a model at your local endpoint (e.g. an OpenAI-
compatible server) — no key required.

## 2. Build your team

Go to **CONFIG → AGENTS**. Assign a configured model to the **Leader** and
**QC** seats, and add a **producer** or two. Each seat can carry a fallback
chain (models tried in order when the primary is unavailable).

## 3. Kick off a job

On the **CONSOLE**, flip to **MOD SQUAD** and use the **KICK OFF** box: type an
objective and launch. Watch the squad work the job live. When it finishes, the
Leader reports back on the **LEADER** view.

Prefer to just talk? Stay on the **LEADER** view and chat — the Leader answers
directly, and can launch a job for you when the work calls for it.

## 4. Find your output

The **ARTIFACTS** tab lists the files a run produced (drafts, reports, research)
and can export them. The **JOBS** tab is the run-folder browser — every job's
full folder, browsable and deletable when you're done with it.
