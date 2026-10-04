"""**固定上下文的预算**——冷启动要读的东西，规模必须可见、可拦。

## 为什么需要它

`AGENT.md` §三 早写明了"必须读 vs 按需读"，也把体积列成表——但**表里的数字是手写的**，
于是它漂了：写的是"AGENT.md 约 1.3k tokens"，实测 ≈2.9k；角色文件声明 0.7~1.3k，
实测 0.8~2.6k。**手写的实测值必然过期**，而按过期数字做预算的会话会误判自己还剩多少余地。

所以这里把两件事拆开：

| 什么 | 在哪 | 性质 |
|---|---|---|
| **预算**（政策：允许多大） | `BUDGETS`（本模块，**一处权威**） | 稳定，值得写进文档 |
| **实测**（事实：现在多大） | `measure()`（每次现算） | 会变，**不进文档** |

**单位取字符数**：体系不引分词器（那是外部依赖），字符数是确定性的；
汉字约 1 token/字，可据此粗估，但判据只认字符。

## 三个面

- **冷启动（必须读）**：`AGENT.md` ＋ `agents/<角色>.md`——每个会话无条件读的那两份；
- **按需面**：`rules/*.md`（查判据时才读）——不在冷启动预算里，但**总量有上限**：
  它虽叫"按需"，实践中几乎每轮都翻，放任增长等于把固定上下文搬到隔壁；
- **工程绑定**：按需，且**只取机读块**（`coord bind` 给的摘要即是有界读法）。

## 超了怎么办

**不是"警告一下"**：超预算即红（`violations()` 非空 ⇒ 工具退出 1、测试失败）。
要加内容就得**先删**，或**显式抬预算**——抬预算要改本模块这一处常量并在提交信息里写明理由，
让"我们决定让冷启动变大"成为一次**有人负责的决定**，而不是悄悄涨上去。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 冷启动单文件预算（字符）——`AGENT.md` 全体必读
AGENT_MAX = 5600
#: 冷启动单文件预算（字符）——`agents/<角色>.md`，每个角色一份
ROLE_MAX = 4300
#: 冷启动合计预算（字符）：`AGENT.md` ＋ 任一角色文件（取最坏的那个角色）
COLD_START_MAX = 9800
#: 按需面总量预算（字符）：`rules/*.md` 合计
RULES_TOTAL_MAX = 30000
#: **常规轮次实际会读的面**（字符）：`AGENT.md` ＋ 最重角色 ＋ 两份动作面主文件。
#:
#: 为什么单列这一条：按需面里真正每轮都翻的就是这四份——总量上限管不住
#: "每份都刚好贴着上限"的合谋，所以要有一个**跨文件**的天花板。
#: 查阅型细节（`rules/*-details.md`）**不计入**：它们只在需要时读，
#: 计进来反而会奖励"把东西挪来挪去"。
ROUTINE_MAX = 22000
#: 动作面主文件（常规轮次会读的两份 rules）
ROUTINE_RULES = ("rules/COORDINATION.md", "rules/WORKSPACE.md")

#: 冷启动必读集合（相对体系根）
COLD_FILES = ("AGENT.md",)
COLD_ROLE_GLOB = "agents/*.md"
#: 按需面（相对体系根）
ON_DEMAND_GLOB = "rules/*.md"

_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def est_tokens(text: str) -> int:
    """粗估 token 数（汉字 ≈1、其余 ≈1/4 字符）——**只用于人看，判据不认它**。"""
    cjk = len(_CJK.findall(text))
    return cjk + (len(text) - cjk) // 4


def measure(maws_root: Path) -> dict:
    """现算固定上下文的规模：逐文件字符数 ＋ 冷启动最坏合计 ＋ 按需面合计。"""
    cold: dict[str, int] = {}
    for name in COLD_FILES:
        path = maws_root / name
        if path.is_file():
            cold[str(path.relative_to(maws_root))] = len(path.read_text(encoding="utf-8"))
    roles: dict[str, int] = {}
    for path in sorted(maws_root.glob(COLD_ROLE_GLOB)):
        roles[str(path.relative_to(maws_root))] = len(path.read_text(encoding="utf-8"))
    rules = {
        str(p.relative_to(maws_root)): len(p.read_text(encoding="utf-8"))
        for p in sorted(maws_root.glob(ON_DEMAND_GLOB))
    }
    worst_role = max(roles.items(), key=lambda kv: kv[1]) if roles else ("", 0)
    routine = {**cold, **{k: roles.get(k, 0) for k in [worst_role[0]]},
               **{k: rules.get(k, 0) for k in ROUTINE_RULES}}
    routine.pop("", None)
    return {
        "cold": cold,
        "roles": roles,
        "rules": rules,
        "cold_start_max": sum(cold.values()) + worst_role[1],
        "cold_start_worst_role": worst_role[0],
        "rules_total": sum(rules.values()),
        "routine": {k: v for k, v in routine.items() if v},
        "routine_total": sum(routine.values()),
        "budgets": {
            "agent_max": AGENT_MAX,
            "role_max": ROLE_MAX,
            "cold_start_max": COLD_START_MAX,
            "rules_total_max": RULES_TOTAL_MAX,
            "routine_max": ROUTINE_MAX,
        },
    }


def violations(measured: dict) -> list[str]:
    """超预算清单（空 = 合规）。**纯函数**——测试拿合成数据就能验它有齿。"""
    out: list[str] = []
    for name, size in measured.get("cold", {}).items():
        if size > AGENT_MAX:
            out.append(f"{name} {size} 字符 > 预算 {AGENT_MAX}（冷启动必读，加内容前先删）")
    for name, size in measured.get("roles", {}).items():
        if size > ROLE_MAX:
            out.append(f"{name} {size} 字符 > 预算 {ROLE_MAX}（该角色冷启动必读）")
    total = measured.get("cold_start_max", 0)
    if total > COLD_START_MAX:
        out.append(
            f"冷启动合计 {total} 字符 > 预算 {COLD_START_MAX}"
            f"（最重角色：{measured.get('cold_start_worst_role', '?')}）"
        )
    routine_total = measured.get("routine_total", 0)
    if routine_total > ROUTINE_MAX:
        out.append(
            f"常规轮次读的面合计 {routine_total} 字符 > 预算 {ROUTINE_MAX}"
            "（AGENT ＋ 最重角色 ＋ 两份动作面主文件）"
        )
    rules_total = measured.get("rules_total", 0)
    if rules_total > RULES_TOTAL_MAX:
        out.append(f"按需面（rules/*.md）合计 {rules_total} 字符 > 预算 {RULES_TOTAL_MAX}")
    return out
