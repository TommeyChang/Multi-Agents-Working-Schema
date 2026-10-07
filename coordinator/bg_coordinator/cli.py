"""协调器 CLI——**唯一写入口**。

所有写动作都经此进协调器；子代理**不再直接碰任务数据文件**（纪律 ⑥ 改写后的形态）。

用法示例：
    coord init
    coord register --id T-D-236 --line D --title "…" --role po:po
    coord claim-analyze --id T-D-236 --role pm:pm-D:D
    coord define --id T-D-236 --role pm:pm-D:D --whitelist "data_access/**" --acceptance "..."
    coord claim-dev --id T-D-236 --role tl:TL-D:D
    coord deliver --id T-D-236 --role tl:TL-D:D --commit abc --gate-cmd "pytest -q" \\
                  --gate-exit 0 --evidence scratch.log
    coord verify  --id T-D-236 --role tl:TL-D:D
    coord accept  --id T-D-236 --role pm:pm-D:D
    coord trace T-D-236
    coord why data_access/kline/follow.py
    coord audit
    coord report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .audit import audit, audit_summary, trace, why
from .engine import (
    Params,
    acquire_lease,
    format_id,
    mark_reclaimed,
    now_iso,
    release_lease,
)
from .errors import Rejection
from .merge import (
    complete_merge,
    dispatch_next,
    queue_status,
    reject_conflict,
    request_merge,
)
from .models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    Origin,
    Priority,
    Role,
    TaskState,
    canonical_role,
)
from .render import human_summary, render_report, render_todo_ready
from .storage import Store, StoreError


def _default_root() -> Path:
    """默认状态根：**显式环境变量 → 与包同级的 .state 目录**，不猜别处。

    这里曾经硬编码体系改名前的旧路径（`.../coordinator`，早已不存在），
    于是不带 `--root` 跑一条命令就会在旧位置**新起一个空状态根**——
    两次调用看到两个真相，正是本体系要治的病。
    """
    env = os.environ.get("BG_COORDINATOR_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / ".state"


DEFAULT_ROOT = _default_root()


def _default_repo() -> Path | None:
    """发布视图的目标仓库——**只认显式声明，绝不内置真实主检出**。

    这里曾经硬编码主检出路径，于是「只覆盖了 `--root`、忘了 `--repo`」的一次
    `publish` 会把 `todo/` **13 个文件、2306 行**换成一份空投影
    （实测事故：一个 1 条目的临时状态覆盖了真实看板，工作树面目全非）。
    **默认值指向别人的工作区，就是一颗静默改主工作区的雷**——所以现在：

    - 没有 `--repo` 也没有 `$BG_COORDINATOR_REPO` ⇒ 返回 `None`；
    - `None` 时 `publish` 直接报错、事件快照不落盘，**什么都不写**。
    """
    env = os.environ.get("BG_COORDINATOR_REPO")
    if not env:
        return None
    path = Path(env)
    return path if path.is_dir() else None


def _store(args: argparse.Namespace) -> Store:
    root = Path(args.root) if getattr(args, "root", None) else DEFAULT_ROOT
    given = getattr(args, "repo", None)
    repo = Path(given) if given else _default_repo()
    return Store(root=root, repo=repo)


def _actor(args: argparse.Namespace) -> Actor:
    return Actor.from_str(args.role)


def _emit(result: Any, as_json: bool) -> int:
    """统一输出：**通过即静默（只给一行）**，失败即一条带责任人的异常。"""
    if hasattr(result, "ok"):
        if result.ok:
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "seq": result.state.seq,
                            "detail": result.detail,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                extra = result.detail or {}
                msg = f"OK seq={result.state.seq}"
                if extra:
                    msg += " " + " ".join(f"{k}={v}" for k, v in extra.items())
                print(msg)
            return 0
        rej: Rejection = result.rejection
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "code": str(rej.code), "message": rej.message, "hint": rej.hint},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"REJECTED {rej}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    st = _store(args)
    st.init()
    print(f"已初始化协调器状态根：{st.root}")
    print(f"  events: {st.events_path}")
    print(f"  state : {st.state_path}")
    print(f"  repo  : {st.repo or '（未配置——发布视图将不可用）'}")
    print("\n**权威是 events.jsonl**；state.json 是可重算缓存（崩溃后可重建）。")
    print("**events 只准追加，任何就地编辑都是事故。**")
    return 0


def cmd_verify_state(args: argparse.Namespace) -> int:
    """校验缓存与事件日志一致；`--rebuild` 时重建并落盘。

    这是「权威是日志、缓存只是派生」这条设计的**可执行判据**。
    """
    st = _store(args)
    if not st.events_path.exists():
        print("协调器未初始化", file=sys.stderr)
        return 1
    state = st.load_state(strict=False)
    diffs = st.verify_cache(state)
    if args.rebuild:
        st.rebuild_and_save()
        print("已从事件日志重建状态缓存")
        return 0
    if not diffs:
        print(f"一致：缓存与事件日志吻合（seq={state.seq}）")
        return 0
    print("不一致：", file=sys.stderr)
    for d in diffs:
        print(f"  - {d}", file=sys.stderr)
    print("\n处置：coord verify-state --rebuild（以事件日志为准重建）", file=sys.stderr)
    return 1


def cmd_health(args: argparse.Namespace) -> int:
    st = _store(args)
    if not st.events_path.exists():
        print("协调器未初始化（先跑 coord init）", file=sys.stderr)
        return 1
    h = st.health()
    print(json.dumps(h, ensure_ascii=False, indent=2))
    return 0 if h["usable"] else 1


def _write_verb(args: argparse.Namespace, verb: str, params: Params) -> int:
    st = _store(args)
    st.init()
    result = st.transact(
        verb,
        args.id,
        _actor(args),
        params=params,
        expect_ver=getattr(args, "expect_ver", None),
        request_id=getattr(args, "request_id", "") or "",
    )
    return _emit(result, args.json)


def _flat(raw: list | None) -> list:
    """展平 `action="append"` 产生的嵌套列表。"""
    if not raw:
        return []
    out: list = []
    for item in raw:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return out


def _acceptance_items(args: argparse.Namespace) -> list[AcceptanceItem] | None:
    raw = _flat(getattr(args, "acceptance", None))
    if not raw:
        return None
    items: list[AcceptanceItem] = []
    for spec in raw:
        typ, _, rest = spec.partition(":")
        if typ == "test":
            items.append(AcceptanceItem(type=AcceptanceType.TEST, cmd=rest, desc=rest))
        elif typ == "negative":
            items.append(AcceptanceItem(type=AcceptanceType.NEGATIVE, desc=rest))
        elif typ == "evidence":
            items.append(AcceptanceItem(type=AcceptanceType.EVIDENCE, path=rest, desc=rest))
        elif typ == "closure":
            # **闭环路径**：功能条目必须有至少一条（rule_closure_path）
            items.append(AcceptanceItem(type=AcceptanceType.CLOSURE, desc=rest))
        else:
            items.append(AcceptanceItem(type=AcceptanceType.MANUAL, desc=spec))
    return items


def cmd_register(args: argparse.Namespace) -> int:
    """登记需求。

    **编号由协调器分配，不由调用方指定**——这是硬要求：
    代理不选号，只声明「哪条线、什么类型」，编号在**同一次持锁事务内**发放。
    """
    if not args.line:
        print("register 必须指定 --line（编号按线分配）", file=sys.stderr)
        return 2
    st = _store(args)
    st.init()
    params = Params(
        title=args.title,
        src=args.src,
        line=args.line.strip().upper(),
        kind=Kind(args.kind) if args.kind else None,
        deps=_flat(args.deps),
        priority=Priority(args.priority) if args.priority else None,
        origin=Origin(args.origin) if args.origin else None,
        source_ref=args.source_ref,
    )
    return _emit(
        st.transact_register(
            _actor(args),
            params=params,
            request_id=getattr(args, "request_id", "") or "",
        ),
        args.json,
    )


def cmd_claim_analyze(args: argparse.Namespace) -> int:
    return _write_verb(args, "claim-analyze", Params())


def _needs_migration_req(repo: Path | None, whitelist: list[str]) -> bool:
    """白名单是否触及绑定 §迁移 的 `dir`（信号只负责"要不要查"）。"""
    if not whitelist:
        return False
    try:
        from .binding import load as _load_binding
        from .testplan import locate_testplan

        path = locate_testplan(repo)
        data, _err = _load_binding(path) if path else ({}, "")
        mig_dir = str((data or {}).get("migrations", {}).get("dir") or "").strip("/")
    except Exception:  # noqa: BLE001 —— 取不到绑定 ⇒ 不查（判不了就不当罪名）
        return False
    if not mig_dir:
        return False
    return any(
        w.lstrip("./") == mig_dir or w.lstrip("./").startswith(mig_dir + "/")
        or w.lstrip("./").rstrip("*").rstrip("/") == mig_dir
        for w in whitelist
    )


def cmd_define(args: argparse.Namespace) -> int:
    frozen = _flat(args.frozen)
    whitelist = _flat(args.whitelist)
    p = Params(
        title=args.title or "",
        whitelist=whitelist,
        frozen=frozen,
        acceptance=_acceptance_items(args),
        constraints=_flat(getattr(args, "constraint", None)) or None,
        deps=_flat(args.deps) or None,
        migration_req=getattr(args, "migration_req", "") or "",
        needs_migration_req=_needs_migration_req(
            Path(getattr(args, "repo", "") or "") if getattr(args, "repo", "") else None,
            whitelist,
        ),
    )
    return _write_verb(args, "define", p)


def cmd_claim_dev(args: argparse.Namespace) -> int:
    return _write_verb(args, "claim-dev", Params())


def cmd_freeze(args: argparse.Namespace) -> int:
    """固化范围与验收快照（DEFINED→DEFINED 的落痕步）。"""
    return _write_verb(args, "freeze", Params())


def cmd_start(args: argparse.Namespace) -> int:
    return _write_verb(args, "start", Params())


def cmd_deliver(args: argparse.Namespace) -> int:
    p = Params(
        commit=args.commit,
        gate_cmd=args.gate_cmd,
        gate_exit=args.gate_exit,
        evidence_path=args.evidence,
        verifier=args.verifier or "",
        verifier_exit=args.verifier_exit,
        changed_files=_flat(args.changed),
        test_scope=getattr(args, "test_scope", "") or "",
    )
    return _write_verb(args, "deliver", p)


def cmd_verify(args: argparse.Namespace) -> int:
    return _write_verb(args, "verify", Params())


def cmd_accept(args: argparse.Namespace) -> int:
    return _write_verb(args, "accept", Params(repo_path=getattr(args, "repo", "") or ""))


def cmd_reverify(args: argparse.Namespace) -> int:
    """返工。**必须给原因**——空串不算给了。"""
    if not (args.reason or "").strip():
        print("reverify 必须给 --reason（缺什么、错在哪、按什么判据）", file=sys.stderr)
        return 2
    return _write_verb(args, "reverify", Params(reason=args.reason))


def cmd_block(args: argparse.Namespace) -> int:
    """阻断。**必须给原因**——空串不算给了。"""
    if not args.reason.strip():
        print("block 必须给 --reason（写清等什么、被谁卡、何时可解）", file=sys.stderr)
        return 2
    return _write_verb(args, "block", Params(reason=args.reason))


def cmd_unblock(args: argparse.Namespace) -> int:
    return _write_verb(args, "unblock", Params())


def cmd_complete(args: argparse.Namespace) -> int:
    return _write_verb(args, "complete", Params(reason=args.note or ""))


def cmd_review(args: argparse.Namespace) -> int:
    return _write_verb(args, "review", Params(reason=args.note or ""))


def cmd_priority(args: argparse.Namespace) -> int:
    return _write_verb(
        args, "set-priority", Params(priority=Priority(args.priority), reason=args.reason or "")
    )


def cmd_override(args: argparse.Namespace) -> int:
    target = TaskState(args.state) if args.state else None
    return _write_verb(
        args, "override", Params(reason=args.reason, target_state=target)
    )


# --- 资源 ---


def cmd_acquire(args: argparse.Namespace) -> int:
    import time

    st = _store(args)
    st.init()
    with st.lock():
        state = st.load_state()
        result = acquire_lease(
            state,
            klass=args.klass,
            holder=args.holder,
            db_name=args.db_name,
            pid=args.pid,
            ttl=args.ttl,
            clock=time.time(),
        )
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_release(args: argparse.Namespace) -> int:
    st = _store(args)
    with st.lock():
        state = st.load_state()
        result = release_lease(state, args.lease_id, args.holder)
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_reclaim(args: argparse.Namespace) -> int:
    st = _store(args)
    with st.lock():
        state = st.load_state()
        result = mark_reclaimed(state, args.lease_id, mdl_failed=args.mdl)
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_raise(args: argparse.Namespace) -> int:
    """**TL 评估后的登记**：影响面决定去向。

    - `intra_line`：本线自决——四要素齐即直达可开发，**不经 PO**；
    - `cross_line`：越出本线 ⇒ 落需求态，交 PO 归一与排序。
    """
    st = _store(args)
    st.init()
    params = Params(
        title=args.title,
        line=args.line or None,
        kind=Kind.R,
        origin=Origin.LINE,
        source_ref=args.source_ref,
        scope=args.scope,
        category=args.category,
        whitelist=_flat(args.whitelist),
        frozen=_flat(args.frozen),
        acceptance=_acceptance_items(args),
        priority=Priority(args.priority) if args.priority else None,
    )
    return _emit(
        st.transact("raise", "", _actor(args), params=params, request_id=args.request_id or ""),
        args.json,
    )


def cmd_bind(args: argparse.Namespace) -> int:
    """工程绑定状态——**当前工程的落点声明齐不齐**。

    读，不写：绑定是工程的声明，协调器只解析与校验。
    缺项**报出来**（退出码 1），不替工程猜默认值——猜出来的默认值就是静默失真。
    """
    from .binding import describe
    from .schema import BINDING_FIELDS

    repo = _store(args).repo
    info = describe(repo, BINDING_FIELDS)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"工程绑定｜{info['project'] or '（未指定目标仓）'}｜{info['source']}")
        if info["path"]:
            print(f"  文件    {info['path']}")
        print(f"  状态    {info['status']}")
        for gap in info["gaps"]:
            print(f"  GAP     {gap}")
        for code, workface in sorted(info["lines"].items()):
            print(f"  线 {code:<5} {'、'.join(workface or []) or '—'}")
        for shadow in info.get("shadowed", []):
            tag = "与已采用的**不一致**" if shadow["diverged"] else "与已采用的一致（冗余）"
            print(f"  另有    {shadow['source']}副本：{shadow['path']}——{tag}")
        if info["status"] != "完整":
            print()
            print("  补齐后体系侧一个字都不用动——**落点归工程**。")
    return 0 if info["status"] == "完整" else 1


def cmd_dispatch(args: argparse.Namespace) -> int:
    """**派单授权**：开子代理前先领凭证（按条目一张）。

    以前"能开几个"只写在 `rules/SUBAGENT.md` 的散文里（"累计 ≤10"），
    指望每个会话自己数——**散文拦不住任何东西**。现在它是协调器里的一条租约：
    可数、有界、会过期、能对账；在办条目没有在手授权，审计会报出来。
    """
    import time

    from .engine import grant_dispatch
    from .models import canonical_role
    from .schema import ROLES

    role_key = canonical_role(args.role.split(":")[0])
    spec = next((r for r in ROLES if r.key == role_key), None)
    if spec is None:
        print(f"[E_UNAUTHORIZED_DISPATCH] 未登记的角色：{args.role}", file=sys.stderr)
        return 1
    if not spec.can_dispatch:
        print(
            f"[E_UNAUTHORIZED_DISPATCH] {role_key} 没有派单权——子代理不能开子代理",
            file=sys.stderr,
        )
        return 1

    st = _store(args)
    st.init()
    with st.lock():
        state = st.load_state()
        task = state.tasks.get(args.task)
        if task is None:
            print(f"[E_UNKNOWN_ID] 条目不存在：{args.task}", file=sys.stderr)
            return 1
        line = str(task.line)
        result = grant_dispatch(
            state, holder=args.role, task_id=args.task, line=line,
            clock=time.time(), ttl=args.ttl,
        )
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_confirm(args: argparse.Namespace) -> int:
    """**记下用户的一次显式确认**（按需求）——commander 发需求的前置。

    这条动词存在的唯一理由：`commander` 既是**用户接口**又是**需求的形式化者**，
    没有独立留痕时，"用户要的"与"它认为用户要的"在状态里长得一模一样。
    确认必须**单独一次动作**、带**用户原话**、绑定**内容指纹**，且发号即消费。

    什么时候该跑它：**用户明确说了要什么之后**（原话进 `--said`）。
    它不产生需求、不占号——只把"用户说过"这件事留成可核的痕迹。
    """
    import time

    from .engine import grant_confirm

    st = _store(args)
    st.init()
    actor = _actor(args)
    with st.lock():
        state = st.load_state()
        result = grant_confirm(
            state,
            line=args.line,
            title=args.title,
            by=f"{actor.role}:{actor.name}",
            said=args.said,
            clock=time.time(),
            ttl=args.ttl_hours * 3600.0,
            request_id=getattr(args, "request_id", "") or "",
        )
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_confirms(args: argparse.Namespace) -> int:
    """在手用户确认一览（读）——**"这条需求到底有没有用户点头"的唯一答案**。"""
    import time

    from .engine import live_confirmations

    st = _store(args)
    state = st.load_state()
    now = time.time()
    rows = live_confirmations(state, line=args.line or "", now=now)
    if args.json:
        print(
            json.dumps(
                {
                    "count": len(rows),
                    "confirmations": [
                        {
                            "id": c.id,
                            "line": c.line,
                            "title": c.title,
                            "digest": c.digest,
                            "by": c.by,
                            "said": c.said,
                            "expires_in_s": int(c.expires_at - now),
                        }
                        for c in rows
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not rows:
        print("在手用户确认：无（commander 要发需求，先请用户确认）")
        return 0
    for c in rows:
        print(f"{c.id}  [{c.line}] {c.title}｜记录人 {c.by}｜{int(c.expires_at - now)}s 后过期")
        print(f"    用户原话：{c.said}")
    return 0


def cmd_grants(args: argparse.Namespace) -> int:
    """在手授权一览——**"现在开了几个子代理"这个问题的唯一答案**。"""
    import time

    from .engine import active_grants

    st = _store(args)
    state = st.load_state()
    now = time.time()
    rows = active_grants(state)
    if args.json:
        print(
            json.dumps(
                {
                    "count": len(rows),
                    "limit": state.quota.subagent_max_per_dispatcher,
                    "tally": dict(state.dispatch_tally),
                    "grants": [
                        {
                            "lease_id": ls.lease_id,
                            "holder": ls.holder,
                            "task": ls.task,
                            "age_s": int(now - ls.created_at),
                            "ttl_s": int(ls.ttl),
                        }
                        for ls in rows
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    limit = state.quota.subagent_max_per_dispatcher
    # **两个数都要给**：在手是当下占着几个，累计是这个派单方一共开了几个
    # ——闸看的是**累计**（用户口径：一个对话累计 ≤10），在手只作观察。
    tally_line = "、".join(
        f"{k} 累计 {v}/{limit}" for k, v in sorted(state.dispatch_tally.items())
    ) or "尚无派单记录"
    print(f"在手子代理授权 {len(rows)} ／ 累计台账：{tally_line}")
    for ls in rows:
        held = int(now - ls.created_at)
        print(f"  {ls.lease_id}  {ls.holder:<24} {ls.task:<10} 已持 {held}s / TTL {int(ls.ttl)}s")
    if not rows:
        print("  （无。要开子代理先领凭证，见 coord dispatch --help）")
    return 0


def cmd_maws(args: argparse.Namespace) -> int:
    """打印 MAWS —— Multi-Agents Working Schema，**并与实现对账**。

    对账治的是一类具体事故：文档写了角色/动词，代码却没实现（或反之）。
    纯文档体系里这类漂移无法发现。
    """
    from .schema import reconcile, render_schema

    rep = reconcile()
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_schema())
    return 0 if rep.consistent else 1


def cmd_line(args: argparse.Namespace) -> int:
    """管理开发线——**线的数量与划分由任务决定**，新线开在协调器里，不开在代码里。"""
    from .engine import add_dev_line

    st = _store(args)
    st.init()
    if args.add:
        with st.lock():
            state = st.load_state()
            ok, msg = add_dev_line(state, args.add, note=args.note or "")
            if ok:
                st.save_state(state)
        print(msg, file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1
    state = st.load_state()
    if args.json:
        print(json.dumps(state.dev_lines, ensure_ascii=False))
    else:
        print("开发线：" + "、".join(state.dev_lines))
    return 0


def cmd_number(args: argparse.Namespace) -> int:
    """查看编号这笔资源的账：库存（已落物／待建／已让号／无账之号）＋ 待建清单。

    号是资源。这里给出的正是它的库存与损耗。
    """
    from .engine import number_gaps, number_inventory, pending_allocations

    st = _store(args)
    state = st.load_state()
    inv = number_inventory(state)
    gaps = number_gaps(state)
    pending = pending_allocations(state)

    if args.json:
        print(
            json.dumps(
                {
                    "inventory": inv,
                    "gaps": gaps,
                    "pending": pending,
                    "ledger": state.allocations[-30:],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not inv:
        print("（簿记为空）")
        return 0

    print(f"{'族':<10}{'水位':>6}{'已落物':>8}{'待建':>6}{'已让号':>8}{'无账':>6}")
    for family, row in inv.items():
        print(
            f"{family:<10}{row['high']:>6}{row['materialized']:>8}"
            f"{row['pending']:>6}{row['released']:>8}{row['unaccounted']:>6}"
        )

    if pending:
        print("\n待建对象（号已占、物未落）：")
        for e in pending:
            who = e.get("holder") or "（无持有人）"
            task = e.get("task") or "（未绑定条目）"
            print(f"  - {format_id(str(e['family']), int(e['number']))}  持有 {who}  来源 {task}")
        print("  处置：对象落成后 materialize；不再需要则 release 并给理由。")

    if gaps:
        print("\n无账之号（号出去了，账上没有）：")
        for g in gaps:
            print(f"  - {g}")
        print("\n处置：**无账之号是资源流失**——须补记来源，或明确让号。")
    return 0


def cmd_reserve(args: argparse.Namespace) -> int:
    """占号：号发出去，**对象待建**。

    用于"先取号、后建件"的场景（如迁移件：先占 revision 号，再写迁移脚本）。
    占号即推进水位，**别的线看到的是占号之后的号**——不会各自都选同一个。
    """
    from .engine import reserve_number
    from .errors import Code, InflightError

    st = _store(args)
    st.init()
    with st.lock():
        state = st.load_state()
        try:
            n, entry = reserve_number(
                state,
                family=args.family,
                holder=args.holder,
                ts=now_iso(),
                task=args.task or "",
                note=args.note or "",
            )
        except InflightError as exc:
            # 串行族的放号闸：**结构化拒绝**，不写盘——账上什么都没发生
            rej = Rejection(
                Code.E_NUMBER_INFLIGHT,
                str(exc),
                owner=args.holder,
                hint="等它 materialize／让号之后再取；该族的号顺序就是链位顺序",
            )
            print(str(rej), file=sys.stderr)
            return 1
        st.save_state(state)
    print(format_id(args.family, n))
    if not args.json:
        print(f"（已占号并记账：持有 {args.holder}，来源 {args.task or '未绑定'}）", file=sys.stderr)
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    """落物：占的号终于有了对象。"""
    from .engine import materialize_number

    st = _store(args)
    with st.lock():
        state = st.load_state()
        ok, msg = materialize_number(state, args.family, args.number, args.id)
        if ok:
            st.save_state(state)
    print(msg, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


def cmd_release_number(args: argparse.Namespace) -> int:
    """让号：明确弃用，**须给理由**——否则与静默丢弃无异。

    与「释放租约」（`release`）是两件事：那是还资源，这是弃号。
    """
    from .engine import release_number

    st = _store(args)
    with st.lock():
        state = st.load_state()
        ok, msg = release_number(state, args.family, args.number, args.reason)
        if ok:
            st.save_state(state)
    print(msg, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


def cmd_quota(args: argparse.Namespace) -> int:
    """查看或设置配额策略。**配额是策略，不是状态**——它落在 `quota.json`。"""
    st = _store(args)
    st.init()
    changes: dict[str, object] = {}
    if args.enabled is not None:
        changes["enabled"] = args.enabled
    if args.bg_db_max is not None:
        changes["bg_db_max"] = args.bg_db_max
    if args.test_slot is not None:
        changes["test_slot"] = args.test_slot
    if args.total is not None:
        changes["total_in_flight"] = args.total
    if args.subagent_max is not None:
        changes["subagent_max_per_dispatcher"] = args.subagent_max
    if changes:
        q = st.set_quota(**changes)
        print(f"已更新配额：{json.dumps(q.to_dict(), ensure_ascii=False)}")
        return 0

    # **按线批预算**：线是工作面，工作量天然不均，所以逐线给，不给统一默认值
    if args.line:
        q = st.load_state().quota
        q.set_line(args.line, args.budget)
        st.save_quota(q)
        if args.budget is None:
            print(f"已收回 {args.line.upper()} 线的预算（该线视为未批）")
        else:
            print(f"已给 {args.line.upper()} 线批预算：同时在办 ≤ {args.budget}")
        return 0

    state = st.load_state()
    if args.json:
        print(json.dumps(state.quota.to_dict(), ensure_ascii=False, indent=2))
        return 0
    q = state.quota
    if not q.enabled:
        print("配额未启用——并行度不受协调器约束")
    print(f"总量上限：{q.total_in_flight if q.total_in_flight is not None else '未设'}")
    if not q.per_line:
        print("逐线预算：**一条未批**——线是工作面，工作量不均，请逐线给")
        print("  怎么定数：python3 tools/calibrate.py（量机器拐点 + 按各线占比给建议）")
    else:
        print("逐线预算（同时在办上限）：")
        for code in state.dev_lines:
            b = q.per_line.get(code)
            used = sum(1 for t in state.tasks.values() if str(t.line) == code and t.in_flight())
            mark = "未批" if b is None else f"≤ {b}（在办 {used}）"
            print(f"  {code:<6} {mark}")
    return 0


def cmd_leases(args: argparse.Namespace) -> int:
    import time

    st = _store(args)
    state = st.load_state()
    from .engine import reclaimable_leases

    rows = []
    for ls in state.leases.values():
        rows.append(
            {
                "lease_id": ls.lease_id,
                "class": ls.klass,
                "holder": ls.holder,
                "db_name": ls.db_name,
                "pid": ls.pid,
                "state": str(ls.state),
            }
        )
    recl = [ls.lease_id for ls in reclaimable_leases(state, time.time())]
    from .engine import queue_positions

    print(
        json.dumps(
            {
                "leases": rows,
                "reclaimable": recl,
                "quota_enabled": state.quota.enabled,
                "quota": state.quota.to_dict(),
                "waiters": queue_positions(state),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# --- 合并 ---


def _migration_checker(repo, commit: str):
    """装配**链位判据**：读分支提交与 main 的迁移图，判有没有分叉／插队／就地改写。

    两个图都**从 git 读**（`git show <ref>:<路径>`）——合并时主检出停在 main，
    分支的迁移件不在工作区里，只有 git 对象面里才有。
    判据本身在 `bg_coordinator/migrations.py`（与提交闸共用，**一处口径**）。

    绑定没有声明迁移目录 ⇒ 本工程没有"迁移件"这类改动 ⇒ 不适用（返回空串）。

    **注记不在这里出**：本判据的返回值语义是"非空即拦"，而注释级例外与"判不出归属"
    都不该拦人——它们由 `commit_gate` 的 WARN 面与 `migration_gate.py` 的提示面承载。
    """
    from . import migrations as mig
    from .binding import load as load_binding
    from .binding import locate as locate_binding

    if repo is None:
        # 无目标仓 ⇒ 无图可判（且没有仓可读）。**不返回 None**：None 的语义是
        # "调用方忘了注入"（fail closed），两者必须分清，否则无仓配置时全部合并被误拦。
        return lambda changed_files, base: ""
    _src, path = locate_binding(repo)
    data: dict = {}
    if path is not None:
        loaded, _err = load_binding(path)
        data = loaded or {}
    mconf = data.get("migrations") if isinstance(data.get("migrations"), dict) else {}
    vdir = str(mconf.get("dir") or "").strip()
    if not vdir:
        return lambda changed_files, base: ""  # 未声明迁移目录 ⇒ 本工程不适用

    def check(changed_files: list[str], base: str) -> str:
        if not any(f.startswith(vdir.rstrip("/") + "/") for f in changed_files):
            return ""  # 本批没有迁移件
        if not (commit and base):
            return "含迁移件，但拿不到分支提交或 main head——链位无从确认"
        rows, err1 = mig.load_from_git(repo, commit, vdir)
        base_rows, err2 = mig.load_from_git(repo, base, vdir)
        if err1 or err2:
            return f"迁移图不可读（{err1 or err2}）——链位无从确认"
        # [6] 的对照系：已落库面（main 树）＋分叉点面（本分支与 main 的 merge-base）
        landed = mig.load_landed(repo, base, commit, vdir) if base_rows else None
        if base_rows and landed is None:
            return "[6] 基线树读不到（`git ls-tree` 失败）——已落库件是否被改写判不出来"
        return "；".join(mig.judge(rows, base_rows, vdir, landed=landed))

    return check


def cmd_merge_request(args: argparse.Namespace) -> int:
    import time

    st = _store(args)
    st.init()
    with st.lock():
        state = st.load_state()
        result = request_merge(
            state,
            task_id=args.id,
            branch=args.branch,
            commit=args.commit,
            requester=args.role,
            changed_files=_flat(args.changed),
            base_commit=args.base or "",
            main_head=args.main_head or "",
            migration_check=_migration_checker(st.repo, args.commit),
            clock=time.time(),
        )
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_merge_next(args: argparse.Namespace) -> int:
    st = _store(args)
    with st.lock():
        state = st.load_state()
        result = dispatch_next(state)
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_merge_ok(args: argparse.Namespace) -> int:
    st = _store(args)
    with st.lock():
        state = st.load_state()
        result = complete_merge(state, args.merge_id, args.result_commit)
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_merge_conflict(args: argparse.Namespace) -> int:
    st = _store(args)
    with st.lock():
        state = st.load_state()
        result = reject_conflict(state, args.merge_id, args.detail or "")
        if result.event:
            st.append_event(result.event)
        st.save_state(result.state)
    return _emit(result, args.json)


def cmd_merge_status(args: argparse.Namespace) -> int:
    st = _store(args)
    print(json.dumps(queue_status(st.load_state()), ensure_ascii=False, indent=2))
    return 0


# --- 查询 / 审核 / 报告 ---


def cmd_show(args: argparse.Namespace) -> int:
    """取出一条目的**当前字段**——「取出」最基本的形态。

    与 `trace` 的区别：`trace` 给**事件链**（发生过什么），
    `show` 给**当前快照**（现在是什么）。两者互补，不重复。
    """
    st = _store(args)
    state = st.load_state()
    t = state.tasks.get(args.id)
    if t is None:
        print(f"未找到条目 {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
        return 0

    def _fmt(items: list[str]) -> str:
        return "、".join(items) if items else "（空）"

    print(f"{t.id}  [{t.kind} · {t.line} 线 · {t.status}]  ver {t.ver}  轮 {t.round}")
    print(f"  标题    {t.title or '（空）'}")
    print(f"  来源    {t.origin or '（未记）'}" + (f"  ← {t.source_ref}" if t.source_ref else ""))
    if t.confirmed_by or t.user_said:
        # **用户确认留痕**：需求类条目"到底有没有用户点头"要一眼看得见
        print(f"  用户确认 {t.confirmed_by or '（无记录）'}")
        if t.user_said:
            print(f"    用户原话：{t.user_said}")
    print(f"  优先级  {t.priority or '（未定）'}")
    print(f"  认领人  {t.owner or '（空——可认领）'}")
    print(f"  定稿人  {t.definer or '（空）'}    验收人  {t.acceptor or '（空）'}")
    print(f"  依赖    {_fmt(t.deps)}")
    print(f"  白名单  {_fmt(t.whitelist)}")
    print(f"  不动    {_fmt(t.frozen)}")
    if t.acceptance:
        print(f"  验收    {len(t.acceptance)} 条")
        for i, a in enumerate(t.acceptance, 1):
            detail = a.cmd or a.desc or a.path
            print(f"          {i}. [{a.type}] {detail}")
    else:
        print("  验收    （空）")
    if t.evidence:
        print(f"  证据    {len(t.evidence)} 轮")
        ev = t.latest_evidence
        if ev is not None:
            print(f"          最新：commit {ev.commit} · 门禁 exit={ev.gate_exit}")
            print(f"          落盘：{ev.evidence_path}")
    else:
        print("  证据    （无）")
    if t.blocked_from is not None:
        print(f"  阻塞    来自 {t.blocked_from}：{t.block_reason}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """按条件批量取出——**有界**，默认最多 20 条。"""
    st = _store(args)
    state = st.load_state()
    rows = [t for t in state.tasks.values() if (not args.line or t.line == args.line.upper())]
    if args.status:
        rows = [t for t in rows if t.status.value == args.status]
    if args.unclaimed:
        rows = [t for t in rows if not t.owner]
    rows.sort(key=lambda t: (t.priority or "P9", t.id))
    total = len(rows)
    shown = rows[: args.limit]

    if args.json:
        print(json.dumps([t.to_dict() for t in shown], ensure_ascii=False, indent=2))
        return 0
    if not shown:
        print("（无匹配条目）")
        return 0
    print(f"{'条目':<12}{'线':<6}{'状态':<13}{'优先级':<8}{'来源':<11}{'认领人':<10}标题")
    for t in shown:
        print(
            f"{t.id:<12}{t.line:<6}{t.status.value:<13}"
            f"{(t.priority or '—'):<8}{(t.origin or '—'):<11}"
            f"{(t.owner or '—'):<10}{t.title[:36]}"
        )
    if total > args.limit:
        print(f"…（共 {total} 条，已显示 {args.limit} 条；用 --limit 调整）")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    st = _store(args)
    state = st.load_state()
    task = state.tasks.get(args.id)
    if task is None:
        print(f"未找到条目 {args.id}", file=sys.stderr)
        return 1
    print(trace(st.read_events(), task).render())
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    st = _store(args)
    state = st.load_state()
    print(why(state, args.path, st.read_events()).render())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    import time

    st = _store(args)
    state = st.load_state()
    if args.summary:
        s = audit_summary(state, clock=time.time())
        print(
            f"正常 {s['ok_count']} 条 / 异常 {s['anomaly_count']} 条（共 {s['total']} 条）"
        )
        return 0
    anomalies = audit(state, clock=time.time())
    if not anomalies:
        print("（无异常）")
        return 0
    print("| 条目 | 异常 | 责任人 | 建议动作 |")
    print("|---|---|---|---|")
    for a in anomalies:
        print(a.render_row())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import time

    st = _store(args)
    state = st.load_state()
    text = render_report(state, clock=time.time())
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """**调度器**：每个角色下一步该做什么（取代"读视图自己对账"）。

    只算不动；它给的每个动作都带放行判据。视图留作证据，不再是找活的入口。
    """
    import time as _time

    from .models import Role as _Role
    from .schedule import plan as _plan
    from .schedule import plan_role as _plan_role

    st = _store(args)
    state = st.load_state()
    line = args.line or "D"
    now = _time.time()
    if args.role_name:
        role = _Role(canonical_role(args.role_name))
        actions = _plan_role(state, role, line, now)
        if args.json:
            print(json.dumps({"role": role.value, "line": line,
                              "actions": [a.__dict__ for a in actions]}, ensure_ascii=False))
        else:
            print(f"{role.value}@{line} 下一步 {len(actions)} 项：")
            for a in actions:
                print(f"  {a.verb:<14} {a.task_id:<14} {a.reason}")
            if not actions:
                print("  （无——当前没有轮到你的事）")
        return 0
    plans = _plan(state, line, now)
    if args.json:
        print(json.dumps({r: [a.__dict__ for a in acts] for r, acts in plans.items()},
                         ensure_ascii=False))
    else:
        for r, acts in plans.items():
            print(f"{r}：{len(acts)} 项")
            for a in acts:
                print(f"  {a.verb:<14} {a.task_id:<14} {a.reason}")
    return 0


def cmd_ready(args: argparse.Namespace) -> int:
    """可认领清单；`--fanout` 给出**可同时开工的批次**。

    "开发用子代理提高并行度"需要一个机械前提：**同批内白名单两两不重叠**。
    否则两个子代理会改同一片文件。`--fanout` 就是把这件事算出来。
    """
    from .readiness import plan_fanout

    st = _store(args)
    state = st.load_state()
    role = Role(canonical_role(args.role_name))

    if not args.fanout:
        print(render_todo_ready(state, role=role))
        return 0

    lines = [args.line] if args.line else list(state.dev_lines)
    any_ready = False
    for code in lines:
        plan = plan_fanout(state.tasks, code, role, state.quota)
        if not plan.total and not plan.deferred:
            continue
        any_ready = True
        cap = f"，线内预算 {plan.budget}" if plan.budget is not None else "（预算未启用）"
        print(f"{code} 线：可认领 {plan.total} 条，最宽可同时开 {plan.width} 个{cap}")
        for i, batch in enumerate(plan.batches, 1):
            ids = "、".join(f"{t.id}({t.priority or '—'})" for t in batch)
            print(f"  第 {i} 批：{ids}")
        if plan.deferred:
            ids = "、".join(t.id for t in plan.deferred)
            print(f"  超预算暂缓：{ids}——先开满本批，腾出额度再派")
        print("  同批白名单两两不重叠 ⇒ 可分派给不同子代理并行开工")
    if not any_ready:
        print("（无可认领条目）")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    from .render import render_all

    st = _store(args)
    st.init()
    written = st.publish(render_all, commit=not args.no_commit)
    for p in written:
        print(f"  写出 {p}")
    print(f"共 {len(written)} 个视图文件")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = _store(args)
    print(human_summary(st.load_state()))
    return 0


# ---------------------------------------------------------------------------
# 参数装配
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="coord",
        description="任务协调器——协议层与存储层（唯一写入口）",
    )
    ap.add_argument("--root", help="协调器状态根（默认 $BG_COORDINATOR_ROOT）")
    ap.add_argument(
        "--repo",
        help="仓库路径（发布视图／落事件快照用；**不给就不写**，不猜主检出）",
    )
    ap.add_argument("--json", action="store_true", help="机读输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_write(name: str, fn: Any, *, need_id: bool = True, **extra: Any) -> argparse.ArgumentParser:
        s = sub.add_parser(name)
        if need_id:
            s.add_argument("--id", required=True)
        s.add_argument("--role", required=True, help="role:name[:line]，如 tl:TL-D:D")
        s.add_argument("--expect-ver", type=int, default=None, help="乐观并发：期望版本号")
        s.add_argument("--request-id", default="", help="幂等键")
        for flag, opts in extra.items():
            opts = dict(opts)
            # 列表型选项（`nargs="*"`）改造成「可重复 ＋ 每次可给多个」并**累加**。
            # 两个坑都要避开：
            #   ① 纯 `nargs="*"` ⇒ 最后一次**覆盖**前面几次，**静默丢约束**；
            #   ② `append` ＋ `nargs="*"` ⇒ 每次只吃一个值，第 2 个会被当成多余参数报错。
            # 故取 `append` ＋ `nargs="+"`。
            if opts.get("nargs") == "*":
                opts["action"] = "append"
                opts["nargs"] = "+"
            s.add_argument(f"--{flag.replace('_', '-')}", **opts)
        s.set_defaults(func=fn)
        return s

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("health").set_defaults(func=cmd_health)
    s_verify = sub.add_parser("verify-state", help="校验状态缓存与事件日志是否一致")
    s_verify.add_argument("--rebuild", action="store_true", help="以事件日志为准重建缓存")
    s_verify.set_defaults(func=cmd_verify_state)
    sub.add_parser("status").set_defaults(func=cmd_status)

    s_reg = sub.add_parser("register", help="登记（编号由协调器分配）")
    s_reg.add_argument("--role", required=True)
    s_reg.add_argument("--title", required=True)
    s_reg.add_argument("--line", required=True, help="线码：A/B/C/D/E/ACL/OPS")
    s_reg.add_argument("--kind", default=None, help="R（需求，默认）或 T（任务）")
    s_reg.add_argument("--src", default="")
    s_reg.add_argument(
        "--origin",
        default=None,
        help="来源：user / commander / line / dba / ops / pm",
    )
    s_reg.add_argument("--source-ref", default="", help="来源引用（条目号／问题号／原话摘要）")
    s_reg.add_argument("--deps", nargs="*", default=[])
    s_reg.add_argument("--priority", default=None)
    s_reg.add_argument("--request-id", default="")
    s_reg.set_defaults(func=cmd_register)
    add_write("claim-analyze", cmd_claim_analyze)
    add_write(
        "define",
        cmd_define,
        title={"default": ""},
        whitelist={"nargs": "*", "default": []},
        frozen={"nargs": "*", "default": []},
        acceptance={"nargs": "*", "default": []},
        constraint={"nargs": "*", "default": None},
        deps={"nargs": "*", "default": None},
        migration_req={"default": ""},
    )
    add_write("claim-dev", cmd_claim_dev)
    add_write("freeze", cmd_freeze)
    add_write("start", cmd_start)
    add_write(
        "deliver",
        cmd_deliver,
        commit={"required": True},
        gate_cmd={"required": True},
        gate_exit={"type": int, "required": True},
        evidence={"required": True},
        verifier={"default": ""},
        verifier_exit={"type": int, "default": None},
        changed={"nargs": "*", "default": []},
        test_scope={"default": "", "help": "申报的测试级别：static|affected|domain|full"},
    )
    add_write("verify", cmd_verify)
    add_write("reverify", cmd_reverify, reason={"required": True})
    add_write("accept", cmd_accept)
    add_write("block", cmd_block, reason={"required": True})
    add_write("unblock", cmd_unblock)
    add_write("complete", cmd_complete, note={"default": ""})
    add_write("review", cmd_review, note={"default": ""})
    add_write("priority", cmd_priority, priority={"required": True}, reason={"default": ""})
    add_write("override", cmd_override, reason={"required": True}, state={"default": None})

    s = sub.add_parser("acquire")
    s.add_argument("--class", dest="klass", default="bg_db")
    s.add_argument("--holder", required=True)
    s.add_argument("--db-name", required=True)
    s.add_argument("--pid", type=int, required=True)
    s.add_argument("--ttl", type=float, default=1800.0)
    s.set_defaults(func=cmd_acquire)

    s = sub.add_parser("release")
    s.add_argument("--lease-id", required=True)
    s.add_argument("--holder", required=True)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("reclaim")
    s.add_argument("--lease-id", required=True)
    s.add_argument("--mdl", action="store_true", help="因 MDL 占用而失败 ⇒ 进 reclaim_tracking")
    s.set_defaults(func=cmd_reclaim)

    sub.add_parser("leases").set_defaults(func=cmd_leases)

    s_raise = sub.add_parser("raise", help="TL 评估后登记（本线自决可不过 PO）")
    s_raise.add_argument("--role", required=True, help="role:name:LINE（须声明自己所属的线）")
    s_raise.add_argument("--title", required=True)
    s_raise.add_argument(
        "--category",
        required=True,
        choices=["technical", "design", "cross_line"],
        help="决定类别：technical（TL 可自决）/ design（归 PM，经 PO）/ cross_line（归 PO）",
    )
    s_raise.add_argument(
        "--scope",
        default=None,
        choices=["intra_line", "cross_line"],
        help="影响面；省略时由 --category 推导",
    )
    s_raise.add_argument("--line", default=None, help="落在哪条线；本线自决须等于自己所属的线")
    s_raise.add_argument("--source-ref", default="", help="来源引用（条目号／问题号）")
    s_raise.add_argument("--priority", default=None)
    s_raise.add_argument("--whitelist", nargs="*", default=[])
    s_raise.add_argument("--frozen", nargs="*", default=[])
    s_raise.add_argument("--acceptance", nargs="*", default=[])
    s_raise.add_argument("--request-id", default="")
    s_raise.set_defaults(func=cmd_raise)

    s_bind = sub.add_parser("bind", help="工程绑定状态（读：来源／缺口／各线工作面）")
    s_bind.set_defaults(func=cmd_bind)

    s_disp = sub.add_parser("dispatch", help="派单授权（开子代理前领凭证，按条目一张）")
    s_disp.add_argument("--role", required=True, help="派单方 role:name[:line]，如 tech-lead:TL-D:D")
    s_disp.add_argument("--task", required=True, help="被派的条目号")
    s_disp.add_argument("--ttl", type=float, default=3600.0, help="授权有效期秒（到期自动回收）")
    s_disp.add_argument("--json", action="store_true")
    s_disp.set_defaults(func=cmd_dispatch)

    s_conf = sub.add_parser("confirm", help="记录用户对某条需求的显式确认（commander 发需求的前置）")
    s_conf.add_argument("--role", required=True, help="记录方 role:name[:line]（自报，无鉴权，留痕可审）")
    s_conf.add_argument("--title", required=True, help="需求标题——必须与 register 的**逐字一致**")
    s_conf.add_argument("--line", required=True, help="线码：A/B/C/D/E/ACL/OPS")
    s_conf.add_argument("--said", required=True, help="**用户原话**（这条记录唯一的证据面）")
    s_conf.add_argument(
        "--ttl-hours", dest="ttl_hours", type=float, default=12.0, help="有效期小时（默认 12）"
    )
    s_conf.add_argument("--request-id", default="")
    s_conf.add_argument("--json", action="store_true")
    s_conf.set_defaults(func=cmd_confirm)

    s_confs = sub.add_parser("confirms", help="在手用户确认一览（读）")
    s_confs.add_argument("--line", default="")
    s_confs.add_argument("--json", action="store_true")
    s_confs.set_defaults(func=cmd_confirms)

    s_grants = sub.add_parser("grants", help="在手子代理授权一览（读）")
    s_grants.add_argument("--json", action="store_true")
    s_grants.set_defaults(func=cmd_grants)

    sub.add_parser("maws", help="打印 MAWS 并对账（Schema ↔ 实现）").set_defaults(func=cmd_maws)

    s_line = sub.add_parser("line", help="管理开发线（查看 / 新建）")
    s_line.add_argument("--add", default=None, help="登记一条新开发线（线码）")
    s_line.add_argument("--note", default="")
    s_line.set_defaults(func=cmd_line)

    s_num = sub.add_parser("number", help="查看编号这笔账（库存 / 待建 / 无账之号）")
    s_num.set_defaults(func=cmd_number)

    s_rsv = sub.add_parser("reserve", help="占号（对象待建）")
    s_rsv.add_argument("--family", required=True, help="如 alembic / T-D / R-ACL")
    s_rsv.add_argument("--holder", required=True)
    s_rsv.add_argument("--task", default="", help="绑定来源条目号")
    s_rsv.add_argument("--note", default="")
    s_rsv.set_defaults(func=cmd_reserve)

    s_mat = sub.add_parser("materialize", help="落物（占的号有了对象）")
    s_mat.add_argument("--family", required=True)
    s_mat.add_argument("--number", type=int, required=True)
    s_mat.add_argument("--id", required=True, help="对象标识（文件名 / 条目号）")
    s_mat.set_defaults(func=cmd_materialize)

    s_rel = sub.add_parser("release-number", help="让号（须给理由）")
    s_rel.add_argument("--family", required=True)
    s_rel.add_argument("--number", type=int, required=True)
    s_rel.add_argument("--reason", required=True)
    s_rel.set_defaults(func=cmd_release_number)

    s_quota = sub.add_parser("quota", help="查看或设置配额策略")
    s_quota.add_argument("--enabled", type=lambda v: v.lower() in {"1", "true", "yes"}, default=None)
    s_quota.add_argument("--bg-db-max", type=int, default=None)
    s_quota.add_argument("--test-slot", type=int, default=None)
    s_quota.add_argument(
        "--subagent-max", type=int, default=None,
        help="同一派单方同时在手的**子代理授权**上限（默认 10）",
    )
    s_quota.add_argument("--line", default=None, help="给某条线批预算")
    s_quota.add_argument(
        "--budget", type=int, default=None,
        help="该线的在办上限；省略＝收回预算（视为未批）",
    )
    s_quota.add_argument("--total", type=int, default=None, help="全部线合计在办上限")
    s_quota.set_defaults(func=cmd_quota)

    s = sub.add_parser("merge-request")
    s.add_argument("--id", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--role", required=True)
    s.add_argument("--changed", action="append", default=[])
    s.add_argument("--base", default="")
    s.add_argument("--main-head", default="")
    s.set_defaults(func=cmd_merge_request)

    sub.add_parser("merge-next").set_defaults(func=cmd_merge_next)

    s = sub.add_parser("merge-ok")
    s.add_argument("--merge-id", required=True)
    s.add_argument("--result-commit", required=True)
    s.set_defaults(func=cmd_merge_ok)

    s = sub.add_parser("merge-conflict")
    s.add_argument("--merge-id", required=True)
    s.add_argument("--detail", default="")
    s.set_defaults(func=cmd_merge_conflict)

    sub.add_parser("merge-status").set_defaults(func=cmd_merge_status)

    s = sub.add_parser("show", help="取一条目的当前字段")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("list", help="按条件批量取出（有界）")
    s.add_argument("--line", default=None)
    s.add_argument("--status", default=None)
    s.add_argument("--unclaimed", action="store_true", help="只看无人认领的")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("trace")
    s.add_argument("id")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("why")
    s.add_argument("path")
    s.set_defaults(func=cmd_why)

    s = sub.add_parser("audit")
    s.add_argument("--summary", action="store_true")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("report")
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_report)

    s_sched = sub.add_parser("schedule", help="调度器：每个角色下一步该做什么（只算不动）")
    s_sched.add_argument("--role", dest="role_name", default="", help="只看某角色")
    s_sched.add_argument("--line", default="")
    s_sched.set_defaults(func=cmd_schedule)

    s = sub.add_parser("ready", help="可认领清单；--fanout 给可并行批次")
    s.add_argument("--role-name", default="tech-lead")
    s.add_argument("--fanout", action="store_true", help="按互不重叠白名单分批")
    s.add_argument("--line", default=None, help="只算某条线")
    s.set_defaults(func=cmd_ready)

    s = sub.add_parser("publish")
    s.add_argument("--no-commit", action="store_true")
    s.set_defaults(func=cmd_publish)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except StoreError as exc:
        print(f"STORE ERROR: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"BAD ARG: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
