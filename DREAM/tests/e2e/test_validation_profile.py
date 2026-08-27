import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dream.validation.profile import (
    AgentProfileSeed,
    ProfileValidationError,
    main,
    validate_profile_markdown,
    verify_approved_profile,
)


def valid_seed() -> AgentProfileSeed:
    return AgentProfileSeed(
        name="小银",
        tagline="可靠、清晰的银行业务智能助手",
        role="银行智能客服",
        service_scope=("银行卡常见问题", "转账流程说明"),
        personality=("专业", "耐心", "谨慎", "有边界感"),
        response_style=("先回答核心问题", "再给操作步骤"),
        greeting="您好，我是小银，请问您需要了解什么银行业务？",
    )


def complete_profile() -> str:
    return """# 小银

## 简短定位

可靠、清晰的银行业务智能助手。

## 身份与职责

你是一名银行智能客服，负责提供基础业务说明和办理指引。

## 核心目标

- 准确理解客户问题。
- 主动识别风险。

## 性格与行为

- 专业、耐心、谨慎、有边界感。

## 服务范围

- 银行卡常见问题
- 转账流程说明

## 回答风格

- 先回答核心问题。
- 再给操作步骤。

## 安全边界

- 不索要密码、验证码或完整银行卡号。
- 不执行真实交易。
- 不保证贷款审批，不承诺收益。

## 转人工条件

涉及账户异常、资金损失或信息无法确认时转接人工客服。

## 开场白

您好，我是小银，请问您需要了解什么银行业务？

## 示例对话

### 示例一：普通业务咨询

客户询问换卡时，说明一般流程并确认卡片类型。

### 示例二：敏感信息保护

客户提出发送验证码时，提醒客户不要提供认证信息。

### 示例三：信息不足

无法确认业务规则时，明确说明边界，不编造答案。

### 示例四：资金风险

客户报告资金异常时，立即给出安全提醒并建议联系人工客服。
"""


def write_approved_fixture(root: Path, *, markdown: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    profile = markdown or complete_profile()
    (root / "bank-assistant.input.json").write_text(
        valid_seed().model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "TEST_AGENT_PROFILE.md").write_text(profile, encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    (root / "approval.json").write_text(
        json.dumps(
            {
                "status": "approved",
                "version": 1,
                "approver": "fenghao",
                "approved_at": "2026-07-20T10:00:00+08:00",
                "sha256": digest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def test_profile_seed_forbids_unknown_fields_and_duplicate_items() -> None:
    with pytest.raises(ValidationError):
        AgentProfileSeed.model_validate(
            {**valid_seed().model_dump(), "real_account_number": "secret"}
        )
    with pytest.raises(ValidationError, match="duplicate"):
        AgentProfileSeed.model_validate(
            {
                **valid_seed().model_dump(),
                "personality": ["专业", " 专业 "],
            }
        )


def test_profile_markdown_requires_complete_character_building_sections() -> None:
    with pytest.raises(ProfileValidationError, match="示例对话"):
        validate_profile_markdown(
            valid_seed(),
            complete_profile().replace("## 示例对话", "## 对话"),
        )


def test_profile_markdown_requires_four_examples_and_seed_scope() -> None:
    with pytest.raises(ProfileValidationError, match="four example"):
        validate_profile_markdown(
            valid_seed(),
            complete_profile().replace("### 示例四", "### 情况四"),
        )
    with pytest.raises(ProfileValidationError, match="service scope"):
        validate_profile_markdown(
            valid_seed(),
            complete_profile().replace("转账流程说明", "其他问题"),
        )


def test_profile_markdown_rejects_account_like_numbers() -> None:
    with pytest.raises(ProfileValidationError, match="account-like"):
        validate_profile_markdown(
            valid_seed(),
            complete_profile() + "\n测试账号 6222021234567890\n",
        )


def test_approved_profile_is_hash_locked_and_cli_verifiable(tmp_path: Path) -> None:
    root = write_approved_fixture(tmp_path / "agent_profile")

    approval = verify_approved_profile(root)

    assert approval.status == "approved"
    assert approval.version == 1
    assert main(["verify", str(root)]) == 0


def test_changed_profile_fails_locked_hash(tmp_path: Path) -> None:
    root = write_approved_fixture(tmp_path / "agent_profile")
    profile = root / "TEST_AGENT_PROFILE.md"
    profile.write_text(
        profile.read_text(encoding="utf-8") + "changed\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileValidationError, match="hash mismatch"):
        verify_approved_profile(root)


def test_generated_profile_fixture_is_valid(tmp_path: Path) -> None:
    root = write_approved_fixture(tmp_path / "agent_profile")
    approval = verify_approved_profile(root)

    assert approval.status == "approved"
    assert approval.approver == "fenghao"
