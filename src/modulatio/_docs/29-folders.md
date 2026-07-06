# Folders — named operator folders for job runs

Register folder locations the team can use during a run: a share full of
documents to process, a drop-folder for the finished product, an application
directory to examine. Registered folders reach **every seat** — the Leader,
QC, and producers alike — with the access mode you chose, and kickoff
directions reference them **by name**:

> Process the document files in folder **contracts** one at a time and
> produce a summary for each.

Open **CONFIG → FOLDERS** in the TUI to manage the registry. Each folder has
a name (or an easy default — the folder's own name), an absolute path, and a
mode.

## The three modes

| Mode | The team can | The engine may |
|---|---|---|
| **read-only** | read files (`read_file` by absolute path) | — |
| **output** | read files | deliver the job's finished product here |
| **read-write** | read, create, and modify files live mid-run | — |

Exactly one **output**-mode folder can be picked as the **job-output
destination** (`o` in the tab): the run's finished products and Product
Quality Report land there instead of `~/Documents/Modulatio` — useful on
systems with no Documents folder, or when the product belongs on a share.
Precedence: **picked folder → `MODULATIO_DELIVERY_DIR` → `~/Documents/Modulatio`**.
If the picked folder is unreachable at delivery time (an unmounted share),
the run delivers to the default location and says so in the run report —
never a silent reroute, never a failed run.

## Network shares (smb / cifs / nfs / anything the OS mounts)

A registered folder is a **path**. Mount the share with your OS (fstab, your
file manager, `mount.cifs`, autofs — whatever you use) and register the
mount point; Modulatio treats it like any local folder. The tab shows a
per-folder **reachability status**, and a dead mount never hangs the app —
every check is bounded by a timeout. An unreachable folder is simply left
out of the team's access for that run.

## The permission model — decided at registration, silent at run time

The FOLDERS tab **is** the permission decision. A registered folder never
fires a runtime permission prompt: the mode you chose is enforced by the
engine for every seat, every run, until you change or remove the
registration. Guardrails that stay on regardless:

- Broad system directories, your home directory itself, and anything
  overlapping Modulatio's own vault or delivery trees are **refused at
  registration** (and re-checked at use time, so a hand-edited config file
  can't sneak one in).
- Dotfiles (`.env`, `.ssh`, …) inside a registered folder are refused to
  the team even in read-write mode — the standing secret floor.
- A read-only or output folder can never be written by a seat: writes are
  physically absent from the sandbox binds and refused by the file tools.

## Two honest limits

- **Reading with shell commands**: inside the run sandbox, `/home` is
  masked, so `run_shell` cannot read a *read-only* folder that lives under
  your home directory — `read_file` is the read path the team is told to
  use. Read-write folders are bound into the sandbox and work everywhere.
- **Mid-run edits are live**: a read-write folder is edited in place — a
  run that fails halfway may leave files partially modified. If the job is
  "process these, produce that", prefer a read-only source folder plus an
  output folder; reserve read-write for jobs whose whole point is editing
  the files where they sit.
