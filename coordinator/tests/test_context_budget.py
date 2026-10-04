"""固定上下文预算——**冷启动的规模要能被拦**。

由来：`AGENT.md` §三 那张体积表是**手写的**，于是漂了——写"AGENT.md 约 1.3k tokens"
实测 ≈2.9k、写"角色 0.7~1.3k"实测 0.8~2.6k。按过期数字做预算的会话会误判自己还剩多少余地。
所以把政策（允许多大）与事实（现在多大）拆开：**政策进常量，事实每次现算**。

这里钉三件：
① 实测值确实在预算内（超了就是"冷启动在悄悄变大"）；
② **判据本身有齿**（合成一份超预算的数据，必须报出来——否则绿灯是空的）；
③ 文档不写实测值（写了必然漂）——`AGENT.md` §3.1 不得再出现具体的 k tokens 数字。
"""

from __future__ import annotations

import re
from pathlib import Path

from bg_coordinator import context as ctx

MAWS = Path(__file__).resolve().parents[2]


def test_cold_start_is_within_budget() -> None:
    """**冷启动（`AGENT.md` ＋ 任一角色文件）在预算内**——本体系当前的实测。"""
    over = ctx.violations(ctx.measure(MAWS))
    assert not over, "固定上下文超预算：\n  " + "\n  ".join(over)


def test_budget_check_has_teeth() -> None:
    """**负例**：合成一份超预算数据，判据必须报出来（防空绿）。"""
    fake = {
        "cold": {"AGENT.md": ctx.AGENT_MAX + 1},
        "roles": {"agents/x.md": ctx.ROLE_MAX + 1},
        "cold_start_max": ctx.COLD_START_MAX + 1,
        "cold_start_worst_role": "agents/x.md",
        "rules_total": ctx.RULES_TOTAL_MAX + 1,
    }
    over = ctx.violations(fake)
    assert len(over) == 4, over
    assert any("AGENT.md" in o for o in over)
    assert any("按需面" in o for o in over)
    # 边界：正好等于预算不算超（预算是上限，不是"必须小于"）
    edge = {
        "cold": {"AGENT.md": ctx.AGENT_MAX},
        "roles": {"agents/x.md": ctx.ROLE_MAX},
        "cold_start_max": ctx.COLD_START_MAX,
        "rules_total": ctx.RULES_TOTAL_MAX,
    }
    assert ctx.violations(edge) == []


def test_every_role_file_carries_a_matching_reading_table() -> None:
    """角色文件里的读表必须与 `context.ROLE_READS`（机器权威）**逐条一致**。

    「按角色拆」拆的是**导航**（谁读哪几节），不是判据——同一份判据只写一次。
    拆导航可以复制（每个角色一份），但复制出来的那份**必须能被对账**，
    否则三个月后没人知道哪份是对的。这条就是那次对账。
    """
    for role, reads in ctx.ROLE_READS.items():
        text = (MAWS / "agents" / f"{role}.md").read_text(encoding="utf-8")
        assert "读表（本角色读什么）" in text, f"{role}.md 没有读表"
        for rel, chapters in reads:
            assert rel in text, f"{role}.md 的读表漏了 {rel}"
            if chapters is not None:
                for ch in chapters:
                    assert f"§{ch}" in text, f"{role}.md 的读表漏了 {rel} §{ch}"
        # 反向：读表里不许出现机器表里没有的规则文件
        listed = set(re.findall(r"`(rules/[A-Za-z\-]+\.md)`", text))
        assert listed <= {rel for rel, _ in reads}, f"{role}.md 多列了：{listed - {r for r, _ in reads}}"


def test_per_role_read_budget_and_anchors() -> None:
    """每个角色的读面在预算内，且**读表的锚点都还在**（节号变了立刻红）。"""
    m = ctx.measure(MAWS)
    assert not m["bad_anchors"], m["bad_anchors"]
    assert not m["unread_files"], f"孤儿规则文件：{m['unread_files']}"
    over = [v for v in ctx.violations(m) if "读面" in v]
    assert not over, over


def test_agent_doc_does_not_hardcode_sizes() -> None:
    """**文档不写实测值**——写了必然漂，上一版就是证据。

    判据：§三 的体积表里不许再出现 "k tokens" 这类具体数字；
    体积只允许指向预算常量与那把闸。
    """
    text = (MAWS / "AGENT.md").read_text(encoding="utf-8")
    section = text.split("## 三、读什么", 1)[1].split("## 四、", 1)[0]
    assert "context_budget.py" in section, "§三 必须指向那把闸"
    assert "k tokens" not in section, "§三 又写回了会漂的实测数字"
