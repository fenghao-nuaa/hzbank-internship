from pathlib import Path

from dream.memory.managers.skill_candidates import SkillManager
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.core.scope import ScopeIds, resolve_scope


def test_skill_manager_writes_reusable_skill_file(tmp_path: Path) -> None:
    ids = ScopeIds("bank-lab", "assistant", "user-1")
    paths = resolve_scope(tmp_path, ids)
    action = ReviewAction(
        kind=ArtifactKind.SKILL,
        tool_name="skill_manage",
        payload={
            "id": "employee-report-writing",
            "title": "Employee Report Writing",
            "scenario": "Prepare a monthly employee report.",
            "inputs": ["approved activity records"],
            "steps": ["Collect records", "Verify evidence", "Render report"],
            "output_template": "Summary, risks, next actions",
            "cautions": "Exclude unverified personal data.",
            "confidence": 0.9,
        },
        source_event_id="evt-skill",
    )

    SkillManager(paths).apply(action)

    content = (paths.skills_dir / "employee-report-writing.skill").read_text(
        encoding="utf-8"
    )
    assert "Employee Report Writing" in content
    assert "Collect records" in content
    assert "evt-skill" in content
