"""Hero #81 code-review probe: does a FAIL-loop improve of a project-local WIN
skill graduate the win content into the SHARED library (R2 bypass)?"""
import json
from datetime import datetime, timezone

from modulatio import lessons, qc_history, skills, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project


def test_fail_improve_lifts_project_local_win_into_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared" / "skills")
    code = "LEAK"
    vault.init_project(code, "Leak", "obj", exist_ok=True)

    # 1. a WIN-codified, PROJECT-LOCAL skill (non-independent content, R2-contained)
    skills.create_skill(
        name="null-guard-inputs", description="win skill",
        prompt_template="WIN BODY\n\n## Learned (from recovery) — x\n\nnon-independent guidance",
        version="1", provenance="win",
        learned_from=("python_code|substantive|null-guard|code:add=S:rm=0:ctrl=+:lit=0",),
        project_code=code,
    )
    assert not (skills._SKILLS_ROOT / "null-guard-inputs.md").exists()

    # 2. three unconsumed FAILS; the Leader (fail loop) says: improve that skill
    for i in range(3):
        qc_history.append_verdict("code", code, qc_history.VerdictRecord(
            entry_id=f"f{i}",
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            task_id="T", producer_agent="p", qc_agent="qc",
            verdict="fail", defect_type="substantive", rationale=f"bad {i}",
            artifact_body="x"))
    decision = {"codifications": [{
        "action": "improve", "name": "null-guard-inputs",
        "recurring_problem": "fails", "evidence_ids": ["f0", "f1", "f2"],
        "capability_tags": [], "guidance": "FAIL GUIDANCE"}]}
    runners = {
        "leader": lambda p: f"```json\n{json.dumps(decision)}\n```",
        "planner": lambda p: "", "drafter": lambda p: "",
        "qc": lambda p: "", "researcher": lambda p: "",
    }
    pr = Project(code=code, name="Leak", objective="obj",
                 leader_model="stub", wiki_path=str(vault.project_dir(code)))
    o = Orchestrator(pr, runners)
    o._post_run_fail_codification(RunSummary(project=pr))

    # 3. did the project-local WIN content land in the SHARED library?
    shared = skills._SKILLS_ROOT / "null-guard-inputs.md"
    print("\nSHARED EXISTS:", shared.exists())
    if shared.exists():
        text = shared.read_text()
        print("SHARED CARRIES WIN BODY:", "non-independent guidance" in text)
        print("SHARED provenance line:", [ln for ln in text.splitlines() if "provenance" in ln])
        print("SHARED learned_from:", [ln for ln in text.splitlines() if "learned_from" in ln])
    assert not shared.exists(), "R2 BYPASS: win content graduated to shared via fail improve"
