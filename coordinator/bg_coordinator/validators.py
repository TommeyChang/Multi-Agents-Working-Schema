"""校验规则——方案 v2 §3.3／§4.9.2 的可判定函数。

**铁律**：协调器只判「形式有效」，绝不判「内容正确」。
这里的每条规则都必须能用字段机械判定；判不了的一律不退化为启发式。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .errors import Code, Rejection
from .models import AcceptanceType, Actor, Evidence, Role, Task, TaskState

# ---------------------------------------------------------------------------
# 路径 / 白名单
# ---------------------------------------------------------------------------

#: 永不回收的生产库名（回收器复用此名单，单源）。
PROTECTED_DB_NAMES: frozenset[str] = frozenset({"broker_gateway", "broker_gateway_dev"})


# ---------------------------------------------------------------------------
# 文档同步（**不强制修订，但一旦修订必须同批**）
# ---------------------------------------------------------------------------

#: 文档面——这些路径是"口径与工程的成文表达"。
DOC_SURFACE: tuple[str, ...] = ("docs/", "README.md")


def is_doc_path(path: str) -> bool:
    p = _norm(path)
    return any(p == d.rstrip("/") or p.startswith(d) for d in DOC_SURFACE)


def changed_docs(changed: list[str]) -> list[str]:
    """**本批实际改动的文档**。

    只记事实，不记计划——把「白名单里列了 docs/」当成「已同步」
    会让异常在真正需要它的时候静默消失。
    """
    seen: list[str] = []
    for f in changed:
        if is_doc_path(f) and f not in seen:
            seen.append(f)
    return seen


def touches_design_surface(changed: list[str]) -> list[str]:
    """实际改动是否触碰**设计面**——契约／口径类文件。

    判据刻意保守：只在**命名形态**上可机械判定时才报，避免启发式误报。
    """
    markers = ("ports.py", "contract.py", "protocol.py", "spec.py")
    return [
        f
        for f in changed
        if any(_norm(f).endswith(m) or m in _norm(f).split("/")[-1] for m in markers)
    ]


def _norm(path: str) -> str:
    return path.strip().strip("/")


def _matches_any(path: str, patterns: list[str]) -> bool:
    """白名单匹配——支持 `**` 与 `*`，**并支持目录前缀**。

    **`*` 不跨目录分隔符**：`data_access/*.py` 不匹配 `data_access/kline/follow.py`。
    这条语义很重要——白名单是并发闸，宽松匹配会静默放行越界改动。

    注意这里是**协调器内部**的匹配，不再受 markdown 里"`/**` 外禁 `*`"那条
    加固纪律约束（那条纪律存在的原因是文本表格会被写坏——方案 v2 §1.1）。
    """
    p = _norm(path)
    for raw in patterns:
        pat = _norm(raw.rstrip("/"))
        if not pat:
            continue
        if p == pat or p.startswith(pat + "/"):
            return True
        if pat.endswith("/**") and p.startswith(pat[:-3] + "/"):
            return True
        # 仅在模式不含路径分隔符时才允许纯 glob 通配（保持 `*` 不跨目录）
        if "/" in pat and fnmatch.fnmatch(p, pat) and not _glob_crosses_sep(p, pat):
            return True
        if "/" not in pat and fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat):
            return True
    return False


def _glob_crosses_sep(path: str, pattern: str) -> bool:
    """判断一次成功的 fnmatch 是否靠 `*` 跨越了目录分隔符。"""
    if "*" not in pattern:
        return False
    prefix = pattern.split("*", 1)[0]
    suffix = pattern.rsplit("*", 1)[-1]
    middle = path[len(prefix) : len(path) - len(suffix) if suffix else None]
    return "/" in middle


def whitelist_covers(changed_files: list[str], whitelist: list[str]) -> tuple[bool, list[str]]:
    """实际改动是否落在白名单内。返回 (是否全部覆盖, 越界文件列表)。"""
    out = [f for f in changed_files if not _matches_any(f, whitelist)]
    return (not out, out)


def whitelist_overlap(a: list[str], b: list[str]) -> bool:
    """两份白名单是否可能写到同一个路径。

    这是「白名单互不重叠 ⇒ 可并行扇出」的机械判据（方案 v2 §3.3）。
    做的是**保守近似**：只要两边存在一方能匹配到另一方字面量的情形即视为冲突。
    """
    if any(_matches_any(x, b) for x in a):
        return True
    return any(_matches_any(y, a) for y in b)


# ---------------------------------------------------------------------------
# 依赖图
# ---------------------------------------------------------------------------

def detect_dep_cycle(tasks: dict[str, Task], start: str, new_deps: list[str]) -> list[str] | None:
    """检测加入 `new_deps` 后是否成环。返回环路径或 None。

    环检测**在 register／define 时做**，不拖到 freeze——否则一个环会潜伏到冻结才被发现
    （方案 v2 §8.1 T04 修正）。
    """
    adj: dict[str, list[str]] = {tid: list(t.deps) for tid, t in tasks.items()}
    adj.setdefault(start, [])
    adj[start] = list(new_deps)

    path: list[str] = []
    seen: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in path:
            return path[path.index(node) :] + [node]
        if node in seen:
            return None
        seen.add(node)
        path.append(node)
        for nxt in adj.get(node, []):
            got = dfs(nxt)
            if got:
                return got
        path.pop()
        return None

    return dfs(start)


def deps_all_terminal(tasks: dict[str, Task], deps: list[str]) -> list[str]:
    """返回**尚未终态**的前置 id 列表（空 ⇒ 全部就绪）。"""
    return [d for d in deps if d in tasks and tasks[d].status != TaskState.ACCEPTED]


def deps_missing(tasks: dict[str, Task], deps: list[str]) -> list[str]:
    return [d for d in deps if d not in tasks]


# ---------------------------------------------------------------------------
# 八条不变量（方案 v2 §3.3／§4.9.2 的实现）
# ---------------------------------------------------------------------------

def rule_1_unique_id(tasks: dict[str, Task], task_id: str) -> Rejection | None:
    """① 号唯一——发号原子化的结果，撞号在结构上不可能。"""
    if task_id in tasks:
        return Rejection(
            Code.E_DUP_ID,
            f"{task_id} 已存在",
            id=task_id,
            hint="取号一律经协调器，禁手工自选号",
        )
    return None


def rule_2_ownership(task: Task, actor: Actor, verb: str) -> Rejection | None:
    """② 一条目一认领人（互斥锁）＋ 认领即冻结。

    **顺序**：先判动词层面的越权，再判终态冻结。否则"无权角色对终态条目动手"
    会报 `E_FROZEN` 而非 `E_FORBIDDEN_WRITE`。
    """
    if verb in {"claim-analyze", "claim-dev"} and task.owner:
        return Rejection(
            Code.E_OWNED,
            f"{task.id} 已被 {task.owner} 认领",
            id=task.id,
            owner=task.owner,
            hint="发现已认领 ⇒ 不重复处理",
        )
    if verb in {"define", "start", "deliver", "verify", "reverify"} and task.owner != actor.name:
        return Rejection(
            Code.E_NOT_OWNER,
            f"{task.id} 的认领人是 {task.owner or '（空）'}，不是 {actor.name}",
            id=task.id,
            owner=actor.name,
        )
    if task.status == TaskState.ACCEPTED and verb != "override":
        return Rejection(
            Code.E_FROZEN,
            f"{task.id} 已终态，范围与内容冻结",
            id=task.id,
            owner=task.owner,
            hint="交付即冻结；新缺口请开新条目（纪律 9）",
        )
    return None


def rule_3_four_elements(task: Task) -> Rejection | None:
    """③ `define` 四要素齐——白名单／不动清单／前置／验收。**缺项即拒、状态不变。**"""
    missing: list[str] = []
    if not task.whitelist:
        missing.append("修改范围白名单")
    if task.frozen is None:
        missing.append("不动清单")  # 空列表合法（明确声明"无"），None 不合法
    if not task.acceptance:
        missing.append("验收标准")
    # 「前置」允许为空列表——无前置是显式声明，不算缺项。
    if missing:
        return Rejection(
            Code.E_INCOMPLETE,
            f"{task.id} 条目化缺项：{'、'.join(missing)}",
            id=task.id,
            owner=task.definer or task.owner,
            hint="无白名单不成条目",
        )
    return None


def rule_4_expect_ver(task: Task, expect_ver: int | None) -> Rejection | None:
    """④ 乐观并发——治「陈旧视图」（当前 82 条重试提交的根因）。"""
    if expect_ver is None:
        return None
    if task.ver != expect_ver:
        return Rejection(
            Code.E_CONFLICT,
            f"{task.id} 已被他人改动（期望 ver={expect_ver}，实际 ver={task.ver}）",
            id=task.id,
            owner=task.owner,
            hint="重新读取当前快照后再试",
        )
    return None


def rule_5_dep_ready(tasks: dict[str, Task], task: Task, verb: str) -> Rejection | None:
    """⑤ 前置须全终态（`claim-dev` 时）+ 依赖不得成环。"""
    if verb != "claim-dev":
        return None
    missing = deps_missing(tasks, task.deps)
    if missing:
        return Rejection(
            Code.E_NO_DEP,
            f"{task.id} 的前置不存在：{missing}",
            id=task.id,
            owner=task.owner,
        )
    pending = deps_all_terminal(tasks, task.deps)
    if pending:
        return Rejection(
            Code.E_NOT_READY,
            f"{task.id} 前置未终态：{pending}",
            id=task.id,
            owner=task.owner,
        )
    return None


def rule_6_whitelist_exclusive(
    tasks: dict[str, Task], task: Task, exclude_self: bool = True
) -> tuple[Rejection | None, list[str]]:
    """⑥ 白名单与在办条目互不重叠——**从纪律变成可判定函数**（方案 v2 §3.3）。

    返回 (拒绝或 None, 冲突条目 id 列表)。
    """
    conflicts: list[str] = []
    for other in tasks.values():
        if exclude_self and other.id == task.id:
            continue
        if not other.in_flight():
            continue
        if whitelist_overlap(task.whitelist, other.whitelist):
            conflicts.append(other.id)
    if conflicts:
        return (
            Rejection(
                Code.E_WL_CONFLICT,
                f"{task.id} 白名单与在办条目冲突：{conflicts}",
                id=task.id,
                owner=task.owner,
                hint="按文件白名单互不重叠扇出；冲突即排队，不硬派",
            ),
            conflicts,
        )
    return (None, [])


def rule_7_mode(tasks: dict[str, Task], task: Task) -> Rejection | None:
    """⑦ `claim-dev` 须有优先级——PO 的产出必须有承载（方案 v2 §9 待决项 7）。"""
    if task.priority is None:
        return Rejection(
            Code.E_NO_PRIORITY,
            f"{task.id} 未定优先级",
            id=task.id,
            hint="优先级由 PO 裁定（P0/P1/P2）后方可派工",
        )
    return None


def rule_8_evidence_form(task: Task, evidence: Evidence | None = None) -> Rejection | None:
    """⑧ 证据形式有效——**只判形式，不判内容**（方案 v2 §4.9.6）。

    能判：路径存在且可读、门禁退出码为 0、verifier 独立复跑过。
    **判不了**：这个测试是否真的证明了这个功能。
    """
    ev = evidence or task.latest_evidence
    if ev is None:
        return Rejection(
            Code.E_NO_EVIDENCE,
            f"{task.id} 无交付证据",
            id=task.id,
            owner=task.owner,
        )
    if ev.gate_exit != 0:
        return Rejection(
            Code.E_GATE_NONZERO,
            f"{task.id} 门禁退出码 {ev.gate_exit} ≠ 0",
            id=task.id,
            owner=task.owner,
            hint="禁把红说成既有红；须给同口径对照实证",
        )
    if not ev.evidence_path:
        return Rejection(
            Code.E_NO_EVIDENCE,
            f"{task.id} 未落盘原始输出路径",
            id=task.id,
            owner=task.owner,
            hint="回报须含：命令 ＋ 退出码 ＋ 摘要数字 ＋ 落盘路径",
        )
    return None


def rule_8b_evidence_readable(evidence_path: str) -> Rejection | None:
    """证据落盘路径**存在且可读**——这一条会真的去碰文件系统。

    这是"证据可核"的机械部分；**它不是"内容正确"的证明**。
    """
    if not evidence_path:
        return Rejection(Code.E_NO_EVIDENCE, "证据路径为空")
    p = Path(evidence_path)
    if not p.exists():
        return Rejection(
            Code.E_NO_EVIDENCE,
            f"证据路径不存在：{evidence_path}",
            owner="",
            hint="门禁原始输出必须落盘供 parent 随查",
        )
    if not p.is_file():
        return Rejection(Code.E_NO_EVIDENCE, f"证据路径不是文件：{evidence_path}")
    try:
        with p.open("rb") as fh:
            fh.read(1)
    except OSError as exc:
        return Rejection(Code.E_NO_EVIDENCE, f"证据路径不可读：{evidence_path}（{exc}）")
    return None


def validate_acceptance(task: Task) -> Rejection | None:
    """验收：逐条按结构化断言判定**形式**（实质判定归 PM）。"""
    if not task.acceptance:
        return Rejection(Code.E_UNMET, f"{task.id} 无验收标准", id=task.id, owner=task.acceptor)
    unmet: list[str] = []
    for item in task.acceptance:
        if item.type == AcceptanceType.NEGATIVE and not item.desc:
            unmet.append("负例缺描述")
        if item.type == AcceptanceType.EVIDENCE and item.path:
            p = Path(item.path)
            if not p.exists():
                unmet.append(f"证据缺失：{item.path}")
        if item.type == AcceptanceType.TEST and not item.cmd:
            unmet.append("验收测试缺命令")
    if unmet:
        return Rejection(
            Code.E_UNMET,
            f"{task.id} 验收项形式不满足：{'；'.join(unmet)}",
            id=task.id,
            owner=task.acceptor,
        )
    return None


def allowed_writer(role: Role) -> bool:
    """⑥ 号纪律改写后的判据：**子代理只能经协调器写**，协调器是唯一写者。

    这个函数表达的是"协调器接受哪些角色作为写入发起方"——不是文件权限。
    """
    return role in set(Role)
