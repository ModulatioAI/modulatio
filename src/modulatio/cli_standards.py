"""CLI for reviewing QC-proposed team standards (slice #10).

Separate script entry (``modulatio-standards``) so the main single-command
``modulatio`` flow stays focused on kickoffs. Three subcommands:

- ``list``    — enumerate pending proposals for a project.
- ``approve`` — append the proposal's rule body to the project's
                domain standards file and delete the proposal.
- ``reject``  — delete the proposal without touching standards.

Business-harness level: no opinion about what the proposals say. The
CLI just moves approved rules into ``<project>/standards/<domain>.md``
so the next kickoff picks them up via the existing ``standards.load``
pathway.
"""

from __future__ import annotations

import typer

from modulatio import standards_proposals


app = typer.Typer(help="Review QC-proposed team standards rules.")


@app.command("list")
def list_cmd(
    code: str = typer.Option(..., help="3-letter project code (e.g. STA)"),
) -> None:
    """List pending proposals for a project. One line per proposal:
    id, domain, title. Empty when nothing is pending."""
    proposals = standards_proposals.list_proposals(code)
    ids = standards_proposals.list_ids(code)
    if not proposals:
        typer.echo(f"No pending proposals for project {code}.")
        return
    typer.echo(f"Pending standards proposals for {code}:")
    for pid, p in zip(ids, proposals):
        typer.echo(f"  [{pid}] ({p.domain}) {p.title}")
        if p.rationale:
            typer.echo(f"      rationale: {p.rationale}")


@app.command("show")
def show_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id (filename stem)"),
) -> None:
    """Show the full body of a single proposal so the human can read
    it before deciding. No side effects."""
    try:
        proposal = standards_proposals.load(proposal_id, project_code=code)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Proposal: {proposal_id}")
    typer.echo(f"Domain:   {proposal.domain}")
    typer.echo(f"Title:    {proposal.title}")
    if proposal.rationale:
        typer.echo(f"Rationale: {proposal.rationale}")
    if proposal.evidence_refs:
        typer.echo(f"Evidence:  {', '.join(proposal.evidence_refs)}")
    typer.echo("")
    typer.echo(proposal.rule_body)


@app.command("approve")
def approve_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id (filename stem)"),
) -> None:
    """Approve a proposal — append its rule body to the project's
    ``standards/<domain>.md`` and delete the proposal file. The
    next kickoff picks up the rule via the existing standards
    loader pathway (no rebuild needed)."""
    try:
        dest = standards_proposals.approve(proposal_id, project_code=code)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"approved {proposal_id} → appended to {dest}")


@app.command("reject")
def reject_cmd(
    code: str = typer.Option(..., help="3-letter project code"),
    proposal_id: str = typer.Argument(..., help="Proposal id (filename stem)"),
) -> None:
    """Reject a proposal — delete the proposal file without touching
    project standards."""
    try:
        standards_proposals.reject(proposal_id, project_code=code)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rejected {proposal_id}")


def main() -> None:
    import sys

    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(app))


if __name__ == "__main__":  # pragma: no cover
    main()
