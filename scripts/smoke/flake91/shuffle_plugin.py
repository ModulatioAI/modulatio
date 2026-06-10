"""One-process full-suite order shuffler (#91 flake harness).

Unlike the xargs node-list approach, this keeps all ~3000 tests in ONE
pytest process (matching how CI and `pytest -q` actually run), so
in-process state pollution and event-loop pressure accumulate naturally.

Usage:
    SHUFFLE_SEED=<seed> pytest tests/ -p shuffle_plugin -q
"""

import os
import random


def pytest_collection_modifyitems(config, items):
    seed = os.environ.get("SHUFFLE_SEED")
    if not seed:
        return
    random.Random(seed).shuffle(items)
    for i, item in enumerate(items):
        if "team_only_default" in item.nodeid:
            print(f"\n[shuffle_plugin] seed={seed} target at {i + 1}/{len(items)}")
            break
