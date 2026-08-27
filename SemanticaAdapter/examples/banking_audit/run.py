"""Run from the repository root: python examples/banking_audit/run.py"""

from pathlib import Path

from auditgraph.cli import main


if __name__ == "__main__":
    output = Path(__file__).with_name("output")
    raise SystemExit(main(["demo", "--output", str(output)]))
