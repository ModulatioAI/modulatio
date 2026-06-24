# Memory, jobs & scheduling

## Memory (MEMORY tab)

Memory persists **per project**, accruing across runs. It has three layers,
shown in one list with a **LAYER** column:

- **episodic** — recent, full-context entries that auto-decay.
- **semantic** — promoted, durable facts (curated).
- **team** — the QC-validated shared pool.

Select an agent to see its episodic + semantic memory; the team pool always
shows. You can **add** a durable note, **edit** or **delete** an entry, and
**export** an agent's memory as markdown. The team pool is QC-curated, so an
edit there is **proposed** for QC approval rather than changed directly.

## Jobs (JOBS tab)

A **job** is one run — its own folder under the project. The JOBS tab is the
run-folder browser: it lists your runs, shows a folder card (objective, the
per-subdir contents, total size) for the selected one, opens the folder, and
**deletes** a whole run you no longer want. A delete is permanent (run output is
ephemeral) and is refused while a job is in flight.

## Scheduling (CRON tab)

The CRON tab manages **scheduled jobs**: a job runs headless on a schedule
(e.g. `daily 09:00`, `weekly mon 09:00`). A schedule can run a raw objective or
a bound **Job Template**. The detail pane shows exactly what a schedule runs —
the template and its parameters, or the objective — plus its next and last run.

You can schedule a template straight from the **JT LIBRARY** tab: highlight a
template and schedule it as a recurring cron job.
