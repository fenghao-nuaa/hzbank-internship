"""AuditGraph command-line entry points."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from auditgraph.core.models import Approval, SourceDocument
from auditgraph.pipeline import AuditPipeline


def demo_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            "file:policy.txt",
            "file",
            "ENTITY|POL-RISK-001:1.0|Policy|Manual Review Policy\n"
            "TRIPLE|POL-RISK-001:1.0|source_ref|policy.txt#article-3",
        ),
        SourceDocument(
            "web:regulation",
            "web",
            "ENTITY|REG-1|Regulation|Credit Risk Regulation\n"
            "RELATION|POL-RISK-001:1.0|implements|REG-1",
        ),
        SourceDocument(
            "database:applications",
            "database",
            "ENTITY|A-1|LoanApplication|Application A-1\n"
            "TRIPLE|A-1|risk_score|82\n"
            "TRIPLE|A-1|risk_level|medium",
        ),
        SourceDocument(
            "api:risk-system",
            "api",
            "ENTITY|A-ALIAS|LoanApplication|Application A-1\n"
            "EVENT|risk_assessed|A-1|2026-08-24\n"
            "TRIPLE|A-1|risk_level|high",
        ),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auditgraph")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="run the complete local banking audit pipeline")
    demo.add_argument("--output", type=Path, required=True, help="directory for JSON, Turtle, and HTML outputs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        result = AuditPipeline().run(
            demo_documents(),
            output_dir=args.output,
            approval=Approval(
                decision_id="pending",
                approver="demo_risk_manager",
                method="email",
                context="demo evidence review",
            ),
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
