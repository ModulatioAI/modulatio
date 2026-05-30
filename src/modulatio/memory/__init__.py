# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio v2 memory layer (slice 4).

Two layers, per locked design (project_modulatio_v2_memory_architecture.md):

- ``agent_memory`` — per-agent private memory (carried from v1.3.1).
  Episodic + semantic JSON storage at
  ``<vault>/<code>/memory/<agent_id>/{episodic,semantic}.json``.
  Strict isolation — only the owning agent reads or writes its own.

- ``team_memory`` — team-shared QC pool (NEW, built on v2's qc_history
  substrate). Markdown at ``<vault>/<code>/team-memory/`` + LanceDB
  index at ``~/.cache/modulatio/team-memory/``. ACL: write only for
  ``tier in {"qc","leader"}``, read for all roster.

Pre-task consultation: orchestrator must call
``team_memory.recall(skill_names, artifact_kind, capability_tags,
task_description)`` before every producer dispatch and inject the result
via ``{team_memory_context}`` slot in the producer prompt.
"""

from modulatio.memory import agent_memory, team_memory

__all__ = ["agent_memory", "team_memory"]
