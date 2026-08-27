"""Validation and hash locking for the synthetic test Agent profile."""

import argparse
from datetime import datetime
import hashlib
from pathlib import Path
import re
import sys
from typing import Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


REQUIRED_HEADINGS = (
    "## 简短定位",
    "## 身份与职责",
    "## 核心目标",
    "## 性格与行为",
    "## 服务范围",
    "## 回答风格",
    "## 安全边界",
    "## 转人工条件",
    "## 开场白",
    "## 示例对话",
)
_SAFETY_CONCEPTS = ("验证码", "密码", "银行卡号", "真实交易", "贷款", "收益")
_ACCOUNT_LIKE = re.compile(r"(?<!\d)\d{12,19}(?!\d)")
_EXAMPLE_HEADING = re.compile(r"^### 示例", re.MULTILINE)


class ProfileValidationError(ValueError):
    """The validation profile is malformed without echoing its content."""


class AgentProfileSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=2, max_length=40)
    tagline: str = Field(min_length=4, max_length=120)
    role: str = Field(min_length=2, max_length=80)
    service_scope: tuple[str, ...] = Field(min_length=1, max_length=12)
    personality: tuple[str, ...] = Field(min_length=2, max_length=12)
    response_style: tuple[str, ...] = Field(min_length=1, max_length=12)
    greeting: str = Field(min_length=1, max_length=500)

    @field_validator("name", "tagline", "role", "greeting")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        rendered = value.strip()
        if not rendered or "\x00" in rendered:
            raise ValueError("profile text must be nonblank text")
        return rendered

    @field_validator("service_scope", "personality", "response_style")
    @classmethod
    def normalize_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        rendered = tuple(item.strip() for item in value)
        if any(not item or "\x00" in item for item in rendered):
            raise ValueError("profile list items must be nonblank text")
        normalized = tuple(item.casefold() for item in rendered)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate profile list item")
        return rendered


class ProfileApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved"]
    version: int = Field(ge=1)
    approver: str = Field(min_length=1, max_length=80)
    approved_at: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("approver")
    @classmethod
    def normalize_approver(cls, value: str) -> str:
        rendered = value.strip()
        if not rendered or "\x00" in rendered:
            raise ValueError("approver must be nonblank text")
        return rendered

    @field_validator("approved_at")
    @classmethod
    def require_timezone(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("approved_at must be ISO 8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("approved_at must include a timezone")
        return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_profile_markdown(seed: AgentProfileSeed, markdown: str) -> None:
    if not isinstance(markdown, str) or not markdown.strip() or "\x00" in markdown:
        raise ProfileValidationError("profile Markdown must be nonblank UTF-8 text")
    if len(markdown) > 12_000:
        raise ProfileValidationError("profile Markdown exceeds 12000 characters")
    if markdown.splitlines()[0].strip() != f"# {seed.name}":
        raise ProfileValidationError("profile title does not match the seed name")
    for heading in REQUIRED_HEADINGS:
        if heading not in markdown:
            raise ProfileValidationError(f"profile is missing required heading {heading}")
    if len(_EXAMPLE_HEADING.findall(markdown)) < 4:
        raise ProfileValidationError("profile must contain at least four example headings")
    for item in seed.service_scope:
        if item not in markdown:
            raise ProfileValidationError("profile is missing a seed service scope item")
    for concept in _SAFETY_CONCEPTS:
        if concept not in markdown:
            raise ProfileValidationError("profile is missing a required safety concept")
    if _ACCOUNT_LIKE.search(markdown):
        raise ProfileValidationError("profile contains an account-like number")


def verify_approved_profile(root: Path) -> ProfileApproval:
    try:
        seed = AgentProfileSeed.model_validate_json(
            (root / "bank-assistant.input.json").read_text(encoding="utf-8")
        )
        markdown = (root / "TEST_AGENT_PROFILE.md").read_text(encoding="utf-8")
        approval = ProfileApproval.model_validate_json(
            (root / "approval.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise ProfileValidationError("approved profile files are invalid") from exc
    validate_profile_markdown(seed, markdown)
    if sha256_text(markdown) != approval.sha256:
        raise ProfileValidationError("approved profile hash mismatch")
    return approval


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a locked test Agent profile")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="validate files and locked SHA-256")
    verify.add_argument("root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "verify":
        return 2
    try:
        approval = verify_approved_profile(args.root)
    except ProfileValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        "approved profile verified "
        f"version={approval.version} sha256={approval.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
