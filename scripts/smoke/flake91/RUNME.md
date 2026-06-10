# #91 flake harness — one-process full-suite order shuffle

The xargs-node-list approach silently splits ~3000 node ids into multiple
pytest processes (ARG_MAX chunking), so it never reproduces in-process
accumulation. This plugin shuffles the collected items inside ONE process,
matching how CI and `pytest -q tests/` actually run.

```bash
cd <repo-root>
SHUFFLE_SEED=<any-string> PYTHONPATH=scripts/smoke/flake91 \
    .venv/bin/python -m pytest tests/ -p shuffle_plugin -p no:randomly -q
```

It prints the target test's position for the run. Seeds used in the
2026-06-10 investigation: h3 (reproduced the test_20 warn-once collision
pre-fix; 2993-green post-fix), h4 (green).

Verdict trail for #91 itself: 40/40 isolated runs under CPU load + five
suite-scale shuffles all green. The identified mechanism (Pilot.pause()
wait_for_idle CPU-time heuristic returning early under machine load, with
row population depending entirely on the on_show refresh) is load-, not
order-, dependent — hence hardened at the source (MemoryScreen.set_project
now populates immediately) rather than chased for a repro.
