import json

from auditgraph.cli import main


def test_demo_cli_prints_summary_and_writes_exports(tmp_path, capsys) -> None:
    exit_code = main(["demo", "--output", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["audit_chain_valid"] is True
    assert payload["compliant"] is True
    assert payload["stage_counts"]["approvals"] == 1
    assert payload["stage_counts"]["documents"] == 4
    assert {path.suffix for path in tmp_path.iterdir()} == {".json", ".ttl", ".html"}
