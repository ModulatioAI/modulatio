# Smoke: iteration mode (2026-05-30)

Durable smokes for the "improve an existing file" arc on
`fix/test-goal-wall-and-code-delivery`. Each is standalone; run with the
install venv:

```
cd scripts/smoke/iteration-mode
for s in smoke_*.py; do echo "== $s =="; ~/modulatio/.venv/bin/python "$s"; done
```

- `smoke_patch_preserves.py` — **increment 3**: a surgical SEARCH/REPLACE patch
  changes one line; every other byte (all controls) is preserved by the engine.
- `smoke_read_toolkit.py` — **increment 2a**: grep/tail/wc + read-only sed
  allowed and confined to the artifacts root; escape/write/exec forms rejected.
- `smoke_delivery_bundle.py` — **increment 2b + polish**: delivery dedups one
  file, replaces a pinned file in place, ships README.md beside game.py.

The full live evidence is in the Nemo hull letter; these reproduce the core
guarantees offline (no LLM calls).
