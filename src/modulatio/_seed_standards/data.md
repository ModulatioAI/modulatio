---
freshness_class: stable
assembler_skill: data-assembly
---
# Data standard (baseline)

Quality bar for structured-data artifacts (`artifact_kind: data` — datasets,
record sets, tabular/JSON output). QC enforces this; producers follow it. Shipped
BASELINE — grows from QC self-healing fixes and human feedback. Team/project
standards override anything here.

## What a data unit is

A self-contained, well-formed structured artifact — a JSON array of like-shaped
records, or a CSV with a header and rows. One unit is one file.

## Quality bar

- **Well-formed.** JSON parses; CSV has a consistent header and column count.
- **Homogeneous within a unit.** Every record in a unit shares the same shape
  (same JSON keys / same CSV columns), so a downstream merge is mechanical.
- **No fabricated rows.** Every record reflects the source the task names; absent
  or unverifiable data is surfaced, not invented.
- **Stable ordering** where the task implies it (sorted by a key, chronological);
  otherwise source order.

## Assembly

A multi-unit dataset is MERGED, not concatenated as prose — the `data-assembly`
family stacks like-shaped units (JSON arrays joined, CSV rows under one header).
Cross-schema joins, aggregations, or folds are a build/tool step, not a merge.
