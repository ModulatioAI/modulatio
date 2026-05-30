# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""CLI for reviewing QC-proposed team-memory entries (slice 9-finish).

Mirrors ``cli_standards.py``. QC's verdict prompt has an OPTIONAL
``proposed_team_memory`` field; when populated, the orchestrator
stages a pending proposal under ``<vault>/<code>/team-memory/_proposals/``.
This CLI is the human review surface:

- ``list``    — enumerate pending proposals for a project
- ``show``    — full body of a single proposal for inspection
- ``approve`` — write the proposal to the team-memory pool, attributed
                to the original proposer
- ``reject``  — delete the proposal without writing

Separate script entry (``modulatio-memory``) so the main ``modulatio``
flow stays focused on kickoffs.
"""
from __future__ import annotations

import typer

from modulatio.memory import team_memory


app = typer.Typer(help="Review QC-proposed team-memory entries.")


@app.command("list")
def list_cmd(
    code: str = typer.Option(..., help="3-letter project code (e.g. STA)"),
) -> None:
    """List pending team-memory proposals. One line per proposal:
    id, proposer, first 60 chars of body."""
    proposals = team_memory.list_proposals(code)
    if not proposals:
        typer.echo(f"No pending team-memory proposals for project {code}.")
        return
    typer.echo(f"Pending team-memory proposals for {code}:")
    for p in proposals:
        snippet = (p.body[:60] + "…") if len(p.body) > 60 else p.body
        typer.echo(f"  [{p.proposal_id}] proposer={p.proposer_id} — {snippet}")
        if p.rationale:
            typer.echo(f"      rationale: {p.rationale}")


@app.command("show")
def show_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id"),
) -> None:
    """Print the full body of a single proposal so the human can read
    it before deciding. No side effects."""
    proposals = team_memory.list_proposals(code)
    matches = [p for p in proposals if p.proposal_id == proposal_id]
    if not matches:
        typer.echo(f"error: proposal {proposal_id!r} not found for {code}", err=True)
        raise typer.Exit(code=1)
    p = matches[0]
    typer.echo(f"Proposal: {p.proposal_id}")
    typer.echo(f"Proposer: {p.proposer_id}")
    typer.echo(f"Timestamp: {p.timestamp}")
    if p.skill_tags:
        typer.echo(f"Skill tags: {', '.join(p.skill_tags)}")
    if p.artifact_kind:
        typer.echo(f"Artifact kind: {p.artifact_kind}")
    if p.capability_tags:
        typer.echo(f"Capability tags: {', '.join(p.capability_tags)}")
    if p.rationale:
        typer.echo(f"Rationale: {p.rationale}")
    typer.echo("")
    typer.echo(p.body)


@app.command("approve")
def approve_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id"),
    approver_id: str = typer.Option(
        "human-admin",
        help=(
            "Identifier recorded as the writer of the resulting team-"
            "memory entry. Default 'human-admin' since the CLI is the "
            "human review surface; pass an agent id to attribute to a "
            "specific reviewer."
        ),
    ),
    approver_tier: str = typer.Option(
        "leader",
        help=(
            "Tier of the approver (must be 'qc' or 'leader' — the ACL "
            "for direct team-memory writes). Default 'leader' for the "
            "human review path."
        ),
    ),
) -> None:
    """Approve a proposal — write its body to the team-memory pool and
    delete the proposal file. The next kickoff's recall() picks up the
    new entry automatically.
    """
    try:
        entry = team_memory.approve_proposal(
            proposal_id,
            project_code=code,
            approver_id=approver_id,
            approver_tier=approver_tier,
        )
    except (KeyError, FileNotFoundError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except team_memory.PermissionDenied as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(
        f"approved {proposal_id} → wrote team-memory entry "
        f"{entry.entry_id} (proposed by {entry.proposed_by or 'unknown'})"
    )


@app.command("reject")
def reject_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id"),
) -> None:
    """Reject a proposal — delete the proposal file without writing
    to team memory."""
    if not team_memory.reject_proposal(proposal_id, project_code=code):
        typer.echo(
            f"error: proposal {proposal_id!r} not found for {code}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"rejected {proposal_id}")


def main() -> None:
    import sys

    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(app))


if __name__ == "__main__":  # pragma: no cover
    main()
