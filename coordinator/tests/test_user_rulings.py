"""**用户口径台账**——判据在 `bg_coordinator/rulings.py`（工具与测试同源）。

由来（真实事故）：用户 2026-10-03 定的「回复风格」七条，在本体系里**整段掉了**——
把散稿收成 `AGENT.md` ＋ `rules/` ＋ `agents/` 时丢的。**没有任何闸会响**：
丢的不是代码，是散文；代码有测试兜着，散文没有。

用户口径（2026-10-04）：「规则性的，最好用工具来解决，才能够真正量化，
单纯靠文字，约束力太小了。」——所以每条口径都要回答**谁核它**，欠账有**上限**。
跑 `tools/rule_coverage.py` 看全表。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coordinator import rulings as rul

MAWS = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("token", "path", "judge"), rul.RULINGS)
def test_user_ruling_is_still_in_the_system(token: str, path: str, judge: str) -> None:
    """口径还在它该在的文档里，且**兑现者存在**（是闸就得能复跑）。"""
    doc = MAWS / path
    assert doc.is_file(), f"落点不存在：{path}"
    assert token in doc.read_text(encoding="utf-8"), (
        f"用户口径丢了：`{token}` 不在 {path} 里。\n"
        f"要么补回文档，要么挪进 rulings.NOT_OURS 并写明理由。"
    )
    if judge != rul.TEXT:
        assert (MAWS / judge).is_file(), f"`{token}` 的兑现者不存在：{judge}"


def test_text_only_rulings_are_capped() -> None:
    """**纯文字口径有上限**（棘轮）：加一条新的，就得先给旧的一条配闸。"""
    rep = rul.report(MAWS)
    assert not rep["gaps"], rep["gaps"]
    names = [r["token"] for r in rep["text_only"]]
    assert len(names) <= rul.TEXT_ONLY_MAX, (
        f"只有文字、没有闸的口径有 {len(names)} 条（上限 {rul.TEXT_ONLY_MAX}）：{names}\n"
        "新加纯文字口径前先把旧的一条变成闸（tools/rule_coverage.py 看全表）"
    )


def test_not_ours_entries_carry_a_reason() -> None:
    """"本体系不管"必须带理由——**不许用沉默表达不管**。"""
    for token, reason in rul.NOT_OURS:
        assert token and reason, "NOT_OURS 每一项都要 token 与理由"
        assert len(reason) >= 8, f"理由太短，等于没说：{token}"
