"""**用户定口径不得静默丢失**——一份带判据的清单。

由来（真实事故）：用户 2026-10-03 定的「回复风格」七条，在本体系里**整段掉了**——
把散稿收成 `AGENT.md` ＋ `rules/` ＋ `agents/` 时丢的。**没有任何闸会响**：
丢的不是代码，是散文；代码有测试兜着，散文没有。

所以这里把"本体系声称携带的用户口径"列成可核的行：**token 必须出现在 path 里**。
找不到就红——逼人当场决定到底哪种：
① 文档该补（那就是丢了）；② 本体系不该管它（那就在下面 `NOT_OURS` 写一句理由，
而不是让它悄悄消失）。

加一条只需加一行。**这份清单本身也是资产**：它回答"用户定过的事，体系里还在不在"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

MAWS = Path(__file__).resolve().parents[2]

#: （口径 token, 权威落点）——本体系**声称携带**的用户口径（机制面）。
#: token 取原文里有辨识度的一小段，避免"写了个同义词也算过"。
CARRIED: tuple[tuple[str, str], ...] = (
    # 回复风格（2026-10-03，全角色适用）——丢过一次，故列在第一位
    ("## 九、回复风格", "AGENT.md"),
    ("禁截断语义", "AGENT.md"),
    # 功能条目验收必须含闭环路径（2026-10-01）
    ("闭环路径", "rules/COORDINATION.md"),
    # 调研亦须条目化（2026-10-01）
    ("调研亦须条目化", "rules/COORDINATION.md"),
    # commander 不得直开 dev（2026-10-04）
    ("不直开 dev", "agents/commander.md"),
    ("不得直开 dev", "rules/SUBAGENT.md"),
    # dev 可再开 dev（2026-10-04）
    ("dev 是可以开 dev 子代理的", "agents/dev.md"),
    # 线 pm 能否开 dev 未规定（2026-10-04）——"未规定"本身也要留痕，免得被会话自行推定
    ("用户口径未规定", "rules/SUBAGENT.md"),
    # 子代理：复用优先／禁空转／线间不设串行闸（2026-10-02~04）
    ("复用", "rules/SUBAGENT.md"),
    ("空转必报", "rules/SUBAGENT.md"),
    ("线间不设串行闸", "rules/SUBAGENT.md"),
    # 取号即锁 ⇒ 本体系的机制形态：编号一律由协调器原子发放
    ("编号当场原子发放", "rules/COORDINATION.md"),
    ("禁手工自选号", "rules/COORDINATION.md"),
    # 越界须立项
    ("界内", "rules/COORDINATION.md"),
    # pm 撰写标准（2026-10-01）
    ("标准撰写", "agents/pm.md"),
)

#: 用户口径里**本体系刻意不管**的那些（工程落点或工程自己的文档面）。
#: 写在这里是"明确的不管"，不是"忘了"——加进来要写理由。
NOT_OURS: tuple[tuple[str, str], ...] = (
    ("取号簿文件落在 `todo/`", "工程落点：本体系的机制是协调器原子发号，簿记形态归工程绑定"),
    ("todo/ 归档与线板格式", "工程自己的追溯体系（MAWS 只渲染视图，不规定其文件组织）"),
    ("对外连接必须正确关闭（2026-10-03）", "代码正确性纪律：归工程自己的工程文档与门禁，本体系不复述"),
    ("测试按域归档与 tests/README 追溯表", "工程测试面口径：见工程绑定 §测试"),
)


@pytest.mark.parametrize(("token", "path"), CARRIED)
def test_user_ruling_is_still_in_the_system(token: str, path: str) -> None:
    """用户定过的口径，必须在它该在的那份文档里找得到。"""
    target = MAWS / path
    assert target.is_file(), f"落点不存在：{path}"
    text = target.read_text(encoding="utf-8")
    assert token in text, (
        f"用户口径丢了：`{token}` 不在 {path} 里。\n"
        f"要么补回文档（那就是丢了），要么把它挪进本文件 NOT_OURS 并写明理由。"
    )


def test_not_ours_entries_carry_a_reason() -> None:
    """"本体系不管"必须带理由——**不许用沉默表达不管**。"""
    for token, reason in NOT_OURS:
        assert token and reason, "NOT_OURS 每一项都要 token 与理由"
        assert len(reason) >= 8, f"理由太短，等于没说：{token}"
