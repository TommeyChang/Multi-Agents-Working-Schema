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
#: 冷启动单文件预算（字符）——`agents/<角色>.md`，每个角色一份。
#: 2026-10-04 由 4300 抬到 4700：角色文件里加了「读表」（谁读哪几节）——
#: 它花 ~330 字符，换的是这个角色**少读几千字符**的共享面。这是一次有人负责的抬预算。
ROLE_MAX = 4700
#: 冷启动合计预算（字符）：`AGENT.md` ＋ 任一角色文件（取最坏的那个角色）。
#: 2026-10-04 由 9800 抬到 10400：同一次抬 ROLE_MAX 的理由（角色文件加了读表），
#: 合计面跟着动——**抬两个是对的，只抬一个会让合计悄悄失效**。
COLD_START_MAX = 10300
#: 常翻面总量预算（字符）：`rules/*.md` 合计，**不含 `*-details.md`**。
#:
#: 口径说明（2026-10-05 收紧）：这条管的是"**实践中几乎每轮都要翻**"的量。
#: 分册（`*-details.md`）恰恰是"按需才读"的那一半，计进来会奖励"把东西挪来挪去"——
#: 它们单独可见（`rules_detail_total`），但不占这条预算。
RULES_TOTAL_MAX = 22000
#: **常规轮次实际会读的面**（字符）：`AGENT.md` ＋ 最重角色 ＋ 两份动作面主文件。
#:
#: 为什么单列这一条：按需面里真正每轮都翻的就是这四份——总量上限管不住
#: "每份都刚好贴着上限"的合谋，所以要有一个**跨文件**的天花板。
#: 查阅型细节（`rules/*-details.md`）**不计入**：它们只在需要时读，
#: 计进来反而会奖励"把东西挪来挪去"。
#: 2026-10-04 由 22000 抬到 22500：同一次抬法的第三处（角色文件变重是读表的代价）。
ROUTINE_MAX = 21900
#: 动作面主文件（常规轮次会读的两份 rules）
ROUTINE_RULES = ("rules/COORDINATION.md", "rules/WORKSPACE.md")

#: **每个角色的精确读表**——（规则文件, 章号元组）。章号 `None` = 整份读。
#:
#: 为什么权威在这里而不是各角色文件里：**同一份判据只写一次**。
#: 一个角色一份完整工作流的写法会把状态机／动词表抄七遍，抄出来的第二份必然漂移
#: ——本体系最贵的事故形态。所以拆的是**导航**（谁读哪几节），不是**判据**。
#: 角色文件里那张读表由本表对账（`tests/test_context_budget.py`），两处不许不一致。
#:
#: 依据是**角色面**（ROLES 里的 workface）：dev 不改状态 ⇒ 不读动词表；
#: pm 不取号、不派单 ⇒ 不读编号与子代理面；commander 不定义条目 ⇒ 不读四要素。
ROLE_READS: dict[str, tuple[tuple[str, tuple[str, ...] | None], ...]] = {
    "commander": (
        ("rules/COORDINATION.md", ("二", "三", "四", "六", "九")),
        ("rules/WORKSPACE.md", ("二", "五", "八", "九")),
    ),
    "po": (
        ("rules/COORDINATION.md", ("二", "三", "四", "六", "九")),
        ("rules/SUBAGENT.md", ("一", "二", "六", "七", "九", "十")),
    ),
    "pm": (
        ("rules/COORDINATION.md", ("二", "三", "四", "五", "九")),
        ("rules/WORKSPACE.md", ("四", "八")),
    ),
    "tech-lead": (
        ("rules/COORDINATION.md", ("二", "三", "四", "五", "九")),
        ("rules/WORKSPACE.md", ("二", "三", "四", "五", "八", "九", "十")),
        ("rules/SUBAGENT.md", ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十二")),
    ),
    "dev": (
        ("rules/COORDINATION.md", ("四", "九")),
        ("rules/WORKSPACE.md", ("二", "三", "四", "八", "十")),
        ("rules/SUBAGENT.md", ("二", "五", "七", "九")),
    ),
    "dba": (
        ("rules/COORDINATION.md", ("二", "三", "四", "九")),
        ("rules/WORKSPACE.md", ("五", "八", "九")),
    ),
    "ops": (
        ("rules/COORDINATION.md", ("二", "三", "四", "九")),
        ("rules/WORKSPACE.md", ("二", "四", "八", "九")),
    ),
}

#: 每个角色"要读的面"上限（字符）：`AGENT.md` ＋ 角色文件 ＋ 它读的各节。
#: **按角色**给上限（不是一刀切）：tech-lead 是执行入口，天然要读最多；
#: dev 不改状态 ⇒ 完全不需要动词表与合入面。数字 = 实测 ＋ 约 4% 余量——
#: 超了说明有人往这个角色的面上加了东西，得先删或显式抬。
ROLE_READ_MAX: dict[str, int] = {
    "commander": 16200,
    # 2026-10-05 +400：同上（po 派 pm，派单口径在它面上）
    # 2026-10-06 +100：SUBAGENT §七 加「预热」（开工即启动／接活才领凭证）——po 是派单方，必读
    "po": 15400,
    "pm": 14000,
    "tech-lead": 25300,
    # dev 也要读派单面（它可再开 dev）——SUBAGENT §二 写明累计口径后 +200
    # 2026-10-06 +300：SUBAGENT §二 加「终态条目必须销账」（dev 读 §二）
    "dev": 16500,
    "dba": 15300,
    "ops": 14300,
}

#: 按需面（不进任何角色的必读集，读不读由事决定）：查阅型分册 ＋ 体系自身风险表
ON_DEMAND: tuple[str, ...] = ("rules/RISKS.md",)

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


def _section_sizes(path: Path) -> dict[str, int]:
    """`## N、标题` → 该节字符数（含标题与下级 `###`）。没有 `## ` 的整份算一节。"""
    text = path.read_text(encoding="utf-8")
    idx = [m.start() for m in re.finditer(r"(?m)^## ", text)]
    if not idx:
        return {"": len(text)}
    out: dict[str, int] = {}
    for i, s in enumerate(idx):
        e = idx[i + 1] if i + 1 < len(idx) else len(text)
        head = text[s:].splitlines()[0]
        num = re.match(r"##\s*([^、\s]+)、?", head)
        out[num.group(1) if num else head] = e - s
    return out


def role_reads(maws_root: Path, role: str) -> tuple[dict, list[str]]:
    """某角色的读面：`文件 → 节` 与**锚点错误**清单（章号在该文件里不存在）。"""
    picked: dict = {}
    bad: list[str] = []
    for rel, chapters in ROLE_READS.get(role, ()):
        path = maws_root / rel
        if not path.is_file():
            bad.append(f"{role}: 文件不存在 {rel}")
            continue
        if chapters is None:
            picked[rel] = {"": len(path.read_text(encoding="utf-8"))}
            continue
        sizes = _section_sizes(path)
        head = next((h for h in sizes if h != ""), "")
        for ch in chapters:
            if ch not in sizes:
                bad.append(f"{role}: {rel} 没有 §{ch} 这一节")
                continue
            picked.setdefault(rel, {})[ch] = sizes[ch]
        if head:
            picked[rel]["(前言)"] = len(path.read_text(encoding="utf-8")) - sum(
                v for k, v in sizes.items() if k != head
            )
    return picked, bad


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
    rules = {}
    details: dict[str, int] = {}
    for p in sorted(maws_root.glob(ON_DEMAND_GLOB)):
        rel = str(p.relative_to(maws_root))
        (details if "-details" in p.name else rules)[rel] = len(p.read_text(encoding="utf-8"))
    worst_role = max(roles.items(), key=lambda kv: kv[1]) if roles else ("", 0)
    routine = {**cold, **{k: roles.get(k, 0) for k in [worst_role[0]]},
               **{k: rules.get(k, 0) for k in ROUTINE_RULES}}
    routine.pop("", None)
    per_role: dict = {}
    bad_anchors: list[str] = []
    for role in ROLE_READS:
        picked, bad = role_reads(maws_root, role)
        bad_anchors += bad
        role_file = maws_root / "agents" / f"{role}.md"
        base = sum(cold.values()) + (len(role_file.read_text(encoding="utf-8")) if role_file.is_file() else 0)
        per_role[role] = {
            "files": {k: sum(v.values()) for k, v in picked.items()},
            "chars": base + sum(sum(v.values()) for v in picked.values()),
        }
    unread = [
        str(p.relative_to(maws_root))
        for p in sorted(maws_root.glob("rules/*.md"))
        if "-details" not in p.name
        and str(p.relative_to(maws_root)) not in ON_DEMAND
        and not any(rel == str(p.relative_to(maws_root)) for reads in ROLE_READS.values() for rel, _ in reads)
    ]
    return {
        "cold": cold,
        "per_role": per_role,
        "bad_anchors": bad_anchors,
        "unread_files": unread,
        "roles": roles,
        "rules": rules,
        "cold_start_max": sum(cold.values()) + worst_role[1],
        "cold_start_worst_role": worst_role[0],
        "rules_total": sum(rules.values()),
        "rules_detail_total": sum(details.values()),
        "routine": {k: v for k, v in routine.items() if v},
        "routine_total": sum(routine.values()),
        "budgets": {
            "agent_max": AGENT_MAX,
            "role_max": ROLE_MAX,
            "cold_start_max": COLD_START_MAX,
            "rules_total_max": RULES_TOTAL_MAX,
            "routine_max": ROUTINE_MAX,
            "role_read_max": dict(ROLE_READ_MAX),
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
    for role, over_cap in measured.get("per_role", {}).items():
        cap = ROLE_READ_MAX.get(role)
        if cap and over_cap["chars"] > cap:
            out.append(
                f"{role} 的读面 {over_cap['chars']} 字符 > 预算 {cap}"
                "（AGENT ＋ 本角色文件 ＋ 它读的各节）"
            )
    for bad in measured.get("bad_anchors", []):
        out.append(f"读表锚点失效：{bad}（节号变了或节被搬走 ⇒ 读表要跟着改）")
    for rel in measured.get("unread_files", []):
        out.append(f"{rel} 没有任何角色读它——孤儿文件（要么进读表，要么进 ON_DEMAND）")
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
