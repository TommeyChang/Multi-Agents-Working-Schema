"""**用户口径台账**——每条口径写在哪、**由谁核**、还是只有文字。

用户口径（2026-10-04）：「规则性的，最好用工具来解决，才能够真正量化，
单纯靠文字，约束力太小了。」

这份台账就是那句话的落地：**每条口径都要能被回答"谁核它"**。
`judge` 只有两种合法值：

- **仓内文件**（某个工具／某个测试）⇒ 有闸，可复跑；
- **`文字`** ⇒ 还没有闸，**欠账**。

欠账本身有**上限**（`TEXT_ONLY_MAX`）：想新加一条纯文字口径，就得先给旧的一条配上闸
——这样"文字条款"的总量只会减，不会悄悄涨。

权威在本模块（`tools/rule_coverage.py` 与 `tests/test_user_rulings.py` 都读它，
**一处声明，两处用**）。
"""

from __future__ import annotations

from pathlib import Path

#: `judge` 的"没有闸"取值——**故意用一个显眼的中文词**，好让它在一屏输出里扎眼。
TEXT = "文字"

#: 允许"只有文字"的口径条数上限（**棘轮**：新加一条纯文字，就先给旧的一条补闸）
TEXT_ONLY_MAX = 6

#: （口径 token, 它该在的落点, 兑现者 = 仓内文件 或 `文字`）
RULINGS: tuple[tuple[str, str, str], ...] = (
    # —— 回复风格（2026-10-03，全角色适用）：丢过一次，故列在第一位
    ("## 九、回复风格", "AGENT.md", "coordinator/tools/style_check.py"),
    ("只答被问的", "AGENT.md", TEXT),  # 用户 2026-10-04 降级：会压掉有用信息，不作硬指标
    ("禁嵌套", "AGENT.md", "coordinator/tests/test_style_check.py"),
    # —— 功能条目验收必须含闭环路径（2026-10-01）
    ("闭环路径", "rules/COORDINATION.md", "coordinator/tests/test_rules_as_gates.py"),
    # —— 调研亦须条目化（2026-10-01）："无条目不做工"由界内闸兜住（改动必须在条目白名单内）
    ("调研亦须条目化", "rules/COORDINATION.md", "coordinator/tools/commit_gate.py"),
    # —— commander 不得直开 dev／dev 可再开 dev（2026-10-04）
    ("不直开 dev", "agents/commander.md", "coordinator/bg_coordinator/schema.py"),
    ("不得直开 dev", "rules/SUBAGENT.md", "coordinator/bg_coordinator/schema.py"),
    # —— 终态条目必须销账（2026-10-06，收窄版：只点终态）——**曾丢过一次**（提交信息说登记了、
    #    文件里却没有；台账查不出自己的行被删），补回
    ("终态条目必须销账", "rules/SUBAGENT.md", "coordinator/bg_coordinator/audit.py"),
    # —— 公共开发树暂不支持（2026-10-06 用户定；理由见 WORKSPACE-details §八·五）
    ("暂不支持", "rules/WORKSPACE-details.md", TEXT),
    # —— 会话锚：开工立 goal、只放指针不放内容（2026-10-06）
    ("goal 只写条目号", ".dsh/skills/subagent-dispatch/SKILL.md", TEXT),
    # —— 可复用子代理预热启动、公共信息预置、续派只给增量（2026-10-06）
    ("开工即启动", "rules/SUBAGENT.md", TEXT),
    # —— 开子代理仅限 PO 与 TL（2026-10-06，仅限 MAWS；**撤销** 10-04 的 dev→dev 例外）
    ("pm 与 dev 无派单权", "rules/SUBAGENT.md", "coordinator/bg_coordinator/schema.py"),
    # —— 线 pm 能否开 dev（2026-10-04 未规定 → **2026-10-06 已裁定：不能**，
    #    开子代理仅限 PO 与 TL）——原"未规定"留痕随裁定失效，撤行以记变迁。
    # —— 子代理三条（2026-10-02~04）
    ("复用", "rules/SUBAGENT.md", "coordinator/bg_coordinator/engine.py"),
    ("空转必报", "rules/SUBAGENT.md", "coordinator/bg_coordinator/audit.py"),
    ("线间不设串行闸", "rules/SUBAGENT.md", TEXT),
    # —— 累计派单上限（2026-10-04）：文字说累计、工具算在办 ⇒ 已改成闸
    ("是累计，不是「在手」", "rules/SUBAGENT.md", "coordinator/tests/test_rules_as_gates.py"),
    # —— 取号即锁 ⇒ 本体系机制：编号一律由协调器原子发放
    ("编号当场原子发放", "rules/COORDINATION.md", "coordinator/bg_coordinator/engine.py"),
    ("禁手工自选号", "rules/COORDINATION.md", "coordinator/bg_coordinator/engine.py"),
    # —— 越界须立项
    ("界内", "rules/COORDINATION.md", "coordinator/tools/commit_gate.py"),
    # —— pm 撰写标准（2026-10-01）
    ("标准撰写", "agents/pm.md", TEXT),
    # —— commander 发需求必须用户确认（2026-10-04）
    ("E_NO_USER_CONFIRM", "rules/COORDINATION.md", "coordinator/tests/test_confirm.py"),
    # —— 固定上下文规模（2026-10-04）
    ("context_budget.py", "AGENT.md", "coordinator/tools/context_budget.py"),
    ("规模有预算、有闸", "rules/COORDINATION.md", "coordinator/bg_coordinator/context.py"),
    # —— 开子代理的交办单元：一次性 vs 可复用（2026-10-05）
    # 可核的部分＝"同一任务同一时刻只发一张凭证"（engine）；
    # "该复用的却新开了"仍未闸（靠查在册 ＋ 回报留痕）——别当已闸
    ("交办单元", "rules/SUBAGENT.md", "coordinator/bg_coordinator/engine.py"),
    # —— 测试分级：哪一级在哪个动作上必须齐（2026-10-05）
    ("分级义务", "rules/SUBAGENT.md", "coordinator/bg_coordinator/testplan.py"),
    # —— 调度器取代文档对账（2026-10-06）
    ("调度器取代文档对账", "rules/COORDINATION.md", "coordinator/bg_coordinator/schedule.py"),
    # —— 按角色拆读表（2026-10-04）
    ("每个角色读哪几节", "AGENT.md", "coordinator/tests/test_context_budget.py"),
)

#: 用户口径里**本体系刻意不管**的那些（工程落点或工程自己的文档面）。
#: 写在这里是"明确的不管"，不是"忘了"——加进来必须写理由（第二条判据会核）。
NOT_OURS: tuple[tuple[str, str], ...] = (
    ("取号簿文件落在 `todo/`", "工程落点：本体系的机制是协调器原子发号，簿记形态归工程绑定"),
    ("todo/ 归档与线板格式", "工程自己的追溯体系（MAWS 只渲染视图，不规定其文件组织）"),
    ("对外连接必须正确关闭（2026-10-03）", "代码正确性纪律：归工程自己的工程文档与门禁，本体系不复述"),
    ("测试按域归档与 tests/README 追溯表", "工程测试面口径：见工程绑定 §测试"),
)


def report(maws_root: Path) -> dict:
    """对账：口径还在不在、兑现者是否存在、欠账几条。**纯读**。"""
    rows: list[dict] = []
    for token, path, judge in RULINGS:
        target = maws_root / path
        gate = maws_root / judge if judge != TEXT else None
        rows.append(
            {
                "token": token,
                "path": path,
                "judge": judge,
                "missing_doc": not (target.is_file() and token in target.read_text(encoding="utf-8")),
                "missing_judge": gate is not None and not gate.is_file(),
            }
        )
    text_only = [r for r in rows if r["judge"] == TEXT]
    return {
        "rows": rows,
        "total": len(rows),
        "text_only": text_only,
        "text_only_max": TEXT_ONLY_MAX,
        "gaps": [r for r in rows if r["missing_doc"] or r["missing_judge"]],
    }
