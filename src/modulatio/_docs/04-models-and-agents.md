# Models & agents

## Models (CONFIG → MODELS)

A **model** is a configured endpoint: provider, base URL, auth, and the model
id, registered as a preset. The registry on the left lists your models with a
live readiness status; the add flow on the right walks provider → auth → model.

**Keys** are managed under *Providers & keys*. A key lives in a shared pool for
its provider; you can **pin** a key to one model to isolate that model's spend,
or put it back on the pool. Keys are written to your vault, never echoed.

## Agents (CONFIG → AGENTS)

Your **roster** is the team: a **Leader**, a **QC**, and any number of
**producers**. Each agent is assigned one of your configured models. Leader and
QC are single seats (remove and re-add to change them); producers are as many as
you like.

### Fallback chains

Each seat can carry an ordered **fallback** chain — models tried top-to-bottom
when the seat's primary model is unavailable. Edit a seat's chain from the
**Fallbacks** flow: add, remove, and reorder.

## No fixed roles

Producers aren't pigeonholed. A producer *is* its skills: the engine matches a
task to the skill it needs and loads that skill onto the best-placed producer at
run time. Manage the skill pool in the **SKILLS** tab.
