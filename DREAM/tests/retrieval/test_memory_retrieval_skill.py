from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path

from dream.retrieval.context_builder import ContextBuilder
from dream.retrieval.models import (
    MemoryKind,
    MemoryRecord,
    RankedMemory,
    RetrievalQuery,
    RetrievalResult,
)


TENANT_ID = "tenant-a"
AGENT_ID = "agent-a"


def _skill(home: Path):
    module = importlib.import_module("dream.retrieval.skill")
    return module.MemoryRetrievalSkill(
        home=home,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
    )


def _agent_root(home: Path) -> Path:
    return home / "tenants" / TENANT_ID / "agents" / AGENT_ID


def _write_user_memory(home: Path, user_id: str, entries: list[str]) -> None:
    path = _agent_root(home) / "users" / user_id / "USER.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n§\n".join(entries) + "\n", encoding="utf-8")


def _write_decision_card(
    home: Path,
    *,
    card_id: str,
    content: str,
    confidence: float = 0.9,
) -> None:
    path = _agent_root(home) / "decision-cards" / f"{card_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "---\n"
            f'id: "{card_id}"\n'
            "status: active\n"
            f"confidence: {confidence}\n"
            'updated_at: "2026-07-23T01:00:00+00:00"\n'
            "source_event_ids:\n"
            '  - "evt-1"\n'
            "---\n\n"
            f"# {card_id}\n\n{content}\n"
        ),
        encoding="utf-8",
    )


def test_retrieves_risk_persona_without_unrelated_writing_or_coding(
    tmp_path: Path,
) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-domain: crypto_investment -->\n"
                "<!-- dream-persona-confidence: 0.95 -->\n"
                "用户资金有限，进行加密货币高风险投资时偏好现货、低杠杆和明确止损。"
            ),
            (
                "<!-- dream-persona-domain: writing -->\n"
                "用户写论文时偏好先列文献综述提纲。"
            ),
            (
                "<!-- dream-persona-domain: coding -->\n"
                "用户学习 Python 时偏好十行以内的完整示例。"
            ),
        ],
    )
    _write_user_memory(
        tmp_path,
        "user-002",
        ["用户愿意使用高杠杆追求短期收益。"],
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="我要进行ETH高风险投资",
    )

    contents = tuple(memory.content for memory in response.memories)
    assert any("低杠杆" in content for content in contents)
    assert all("论文" not in content for content in contents)
    assert all("Python" not in content for content in contents)
    assert all("高杠杆追求" not in content for content in contents)


def test_retrieves_supplier_account_decision_experience(tmp_path: Path) -> None:
    _write_decision_card(
        tmp_path,
        card_id="supplier-account-verification",
        content=(
            "供应商收款账户变更必须暂停付款，使用合同中的独立联系方式核验，"
            "完成审批后才能继续。"
        ),
    )
    _write_decision_card(
        tmp_path,
        card_id="python-debugging",
        content="Python 报错时先定位异常行，再提供最小修改。",
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="供应商账户变更怎么办",
    )

    assert response.memories
    assert response.memories[0].memory_id == "supplier-account-verification"
    assert "独立联系方式核验" in response.context
    assert "Python 报错" not in response.context


def test_coding_query_does_not_return_financial_rules(tmp_path: Path) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-domain: coding -->\n"
                "用户是 Python 初学者，希望逐行解释报错并给出最小可运行示例。"
            )
        ],
    )
    _write_decision_card(
        tmp_path,
        card_id="financial-transfer-verification",
        content="银行转账前必须核验收款账户、金额和审批链。",
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="Python代码报错怎么修改",
    )

    assert any("Python 初学者" in item.content for item in response.memories)
    assert all("银行转账" not in item.content for item in response.memories)


def test_domain_label_without_content_evidence_does_not_pollute_results(
    tmp_path: Path,
) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-domain: crypto_investment -->\n"
                "用户是加密货币初学者，进行 ETH 合约交易时偏好低杠杆。"
            ),
            (
                "<!-- dream-persona-domain: crypto_investment -->\n"
                "用户旅行时每天只安排两个景点并预留午休时间。"
            ),
        ],
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="ETH合约风险",
        task_context={"domain": "crypto"},
    )

    assert any("低杠杆" in item.content for item in response.memories)
    assert all("景点" not in item.content for item in response.memories)


def test_loader_splits_mixed_topics_inside_one_legacy_persona_entry(
    tmp_path: Path,
) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-id: mixed-persona -->\n"
                "<!-- dream-persona-domain: crypto_investment -->\n"
                "When handling dispute transactions, separate confirmed facts "
                "from pending verification.\n"
                "User is a cryptocurrency beginner with limited capital who "
                "prefers ETH spot and low leverage.\n"
                "Additional durable requirements:\n"
                "- Explain maximum loss before potential gains\n"
                "- Use actual U amounts"
            )
        ],
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="ETH合约风险",
        task_context={"domain": "crypto"},
    )

    assert any("cryptocurrency beginner" in item.content for item in response.memories)
    assert all("dispute transactions" not in item.content for item in response.memories)
    assert "maximum loss" in response.context


def test_default_retrieval_returns_at_most_five_of_one_hundred_memories(
    tmp_path: Path,
) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-domain: coding -->\n"
                f"Python debugging preference number {index}."
            )
            for index in range(100)
        ],
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="Python debugging",
    )

    assert 0 < len(response.memories) <= 5


def test_external_agent_receives_serializable_context(tmp_path: Path) -> None:
    _write_user_memory(
        tmp_path,
        "user-001",
        [
            (
                "<!-- dream-persona-domain: crypto_investment -->\n"
                "用户资金有限，偏好低杠杆和逐仓模式，并要求先说明最大可能损失。"
            )
        ],
    )

    response = _skill(tmp_path).retrieve(
        user_id="user-001",
        query="ETH合约交易风险分析",
        task_context={"domain": "crypto"},
    )
    payload = response.to_dict()

    assert payload["memories"][0]["type"] == "persona"
    assert "最大可能损失" in payload["context"]
    assert payload["query"] == "ETH合约交易风险分析"


def test_context_builder_deduplicates_equivalent_memories() -> None:
    query = RetrievalQuery(
        text="concise answers",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
    )
    first = MemoryRecord(
        memory_id="persona-a",
        kind=MemoryKind.USER_PERSONA,
        content="User prefers concise answers.",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
        confidence=0.9,
    )
    duplicate = MemoryRecord(
        memory_id="persona-b",
        kind=MemoryKind.USER_PERSONA,
        content="User prefers concise answer.",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
        confidence=0.8,
    )

    context = ContextBuilder().build(
        RetrievalResult(
            query=query,
            matches=(RankedMemory(first, 1.0), RankedMemory(duplicate, 0.9)),
        )
    )

    assert len(context.included_memory_ids) == 1
    assert context.included_memory_ids == ("persona-a",)


def test_context_builder_prefers_newer_conflicting_memory() -> None:
    query = RetrievalQuery(
        text="response language",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
    )
    old = MemoryRecord(
        memory_id="language-old",
        kind=MemoryKind.USER_PERSONA,
        content="User prefers English responses.",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
        confidence=0.95,
        updated_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        metadata={"conflict_key": "response_language"},
    )
    current = MemoryRecord(
        memory_id="language-current",
        kind=MemoryKind.USER_PERSONA,
        content="User requires Chinese responses.",
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        user_id="user-001",
        confidence=0.9,
        updated_at=datetime.now(timezone.utc).isoformat(),
        metadata={"conflict_key": "response_language"},
    )

    context = ContextBuilder().build(
        RetrievalResult(
            query=query,
            matches=(RankedMemory(old, 1.0), RankedMemory(current, 0.9)),
        )
    )

    assert context.included_memory_ids == ("language-current",)
    assert "Chinese responses" in context.markdown
    assert "English responses" not in context.markdown
