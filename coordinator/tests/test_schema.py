"""MAWS Schema 测试——**对账即测试**。

Schema 的价值全在对账：它治的是一类具体事故——**文档写了角色／动词，代码却没实现**。
纯文档体系里这类漂移无法发现，因为它没有可对账的另一半。
"""

from __future__ import annotations

from pathlib import Path

from bg_coordinator.models import CATEGORY_OWNER, Role
from bg_coordinator.schema import (
    FAMILIES,
    ROLES,
    SCHEMA_VERSION,
    all_verbs,
    reconcile,
    render_schema,
    state_verbs,
    verb_roles,
)
from bg_coordinator.statemachine import TRANSITIONS


def test_schema_is_consistent_with_implementation() -> None:
    """**核心断言**：Schema 说的，实现里必须真有。"""
    rep = reconcile()
    assert rep.consistent, "对账不一致：\n" + "\n".join(str(d) for d in rep.drifts)
    assert rep.version == SCHEMA_VERSION


def test_state_verbs_come_from_transition_table() -> None:
    """状态动词**从转换表派生**，不另立一份——否则两份必然漂移。"""
    assert state_verbs() == tuple(sorted(TRANSITIONS))
    for verb in state_verbs():
        assert verb in TRANSITIONS


def test_no_role_is_declared_twice() -> None:
    keys = [r.key for r in ROLES]
    assert len(keys) == len(set(keys))


def test_schema_covers_every_runtime_role() -> None:
    """运行时能用的角色，Schema 必须都登记——否则会出现"合法但无定义"的角色。"""
    schema_keys = {r.key for r in ROLES}
    runtime_keys = {r.value for r in Role}
    assert runtime_keys <= schema_keys, f"未登记：{runtime_keys - schema_keys}"


def test_only_declared_roles_dispatch_and_dev_recursion_is_the_only_exception() -> None:
    """派单权**只给显式声明者**；子代理默认不得自开，**唯一例外是 dev 再开 dev**。

    口径来源（2026-10-04 用户定）：
    - 「dev 是可以开 dev 子代理的」⇒ dev 有派单权（用于本条目内分工／扇出，按额度计数）；
    - 「你不允许直开 dev」⇒ commander／parent 不得直开 dev ⇒ commander 没有派单权；
    - 「线 pm 能否开 dev：用户口径未规定」⇒ pm 保持无派单权（**不自行推定**）。

    这条以前写的是"子代理都不能开子代理"（结构禁令）；用户后来给的额度／复用／空转
    三处控制取代了那道禁令，判据随之改口径——但**默认仍然是不许**，例外只有一处。
    """
    by_key = {r.key: r for r in ROLES}
    for r in ROLES:
        if r.form == "subagent":
            allowed = {"dev"} if r.key == "dev" else set()
            assert set(r.dispatch) <= allowed, f"{r.key} 是子代理却声明了派单权：{r.dispatch}"
        else:
            assert r.form == "independent"
    assert "dev" not in by_key["commander"].dispatch, "commander 不得直开 dev（10-04 用户定）"
    assert "dev" in by_key["dev"].dispatch, "dev 可再开 dev（10-04 用户定）"


def test_every_dispatch_target_is_a_registered_role() -> None:
    keys = {r.key for r in ROLES}
    for r in ROLES:
        for target in r.dispatch:
            assert target in keys, f"{r.key} 派向未登记的角色 {target}"


def test_every_role_has_exactly_one_owner() -> None:
    keys = {r.key for r in ROLES}
    for r in ROLES:
        assert r.reports_to == "user" or r.reports_to in keys, r.reports_to


def test_categories_are_all_owned() -> None:
    keys = {r.key for r in ROLES}
    assert set(CATEGORY_OWNER) == {"technical", "design", "cross_line"}
    assert all(owner in keys for owner in CATEGORY_OWNER.values())


def test_tl_can_only_decide_technical() -> None:
    """**TL 只裁技术问题**——设计与跨线一律上 PO。"""
    assert [c for c, o in CATEGORY_OWNER.items() if o == "tech-lead"] == ["technical"]


def test_every_verb_group_is_non_empty() -> None:
    groups = all_verbs()
    assert set(groups) == {"state", "resource", "merge", "read"}
    for name, verbs in groups.items():
        assert verbs, f"{name} 组为空"


def test_verb_role_lookup_matches_transition_table() -> None:
    """动词的角色判定**与运行时同源**——不另存一份。"""
    for verb in state_verbs():
        expected = tuple(sorted(r.value for r in TRANSITIONS[verb].roles))
        assert verb_roles(verb) == expected


def test_families_cover_the_three_id_kinds() -> None:
    """三类号都要在 Schema 里——**号是资源**，资源清单不能漏。"""
    serves = {f.serves for f in FAMILIES}
    assert "需求" in serves
    assert "开发任务" in serves
    assert "数据库迁移件" in serves

    alembic = next(f for f in FAMILIES if f.family == "alembic")
    assert alembic.two_phase is True, "迁移号是两阶段：先占号、后落物"
    assert all(f.issued_by == "coordinator" for f in FAMILIES), "号只由协调器发"


def test_reconcile_detects_subagent_dispatch_drift() -> None:
    """**对账要真的有齿**——构造一处漂移，它必须报出来。

    没有这条，`reconcile()` 可能永远返回"一致"，测试就成了摆设。
    """
    from bg_coordinator import schema as mod

    original = mod.ROLES
    try:
        broken = original + (
            mod.RoleSpec(
                key="broken", form="subagent", reports_to="po",
                dispatch=("dev",), brief="子代理却派单", workface=(),
            ),
        )
        mod.ROLES = broken
        rep = mod.reconcile()
        assert not rep.consistent
        assert any("subagent-dispatch" in str(d) for d in rep.drifts)
    finally:
        mod.ROLES = original


def test_render_schema_is_readable() -> None:
    text = render_schema()
    assert "MAWS" in text
    for r in ROLES:
        assert r.key in text
    assert "对账" in text


# ---------------------------------------------------------------------------
# 文档头部 ↔ Schema：**逐字段对账**
# ---------------------------------------------------------------------------


def test_role_docs_are_consistent_with_schema() -> None:
    """每份角色文档的头部必须与 Schema **逐字段一致**。

    只查"文件在不在"不够——文档写"归 PO"、Schema 写"归用户"，
    这种不一致人工审不出来。有了可解析头部，它就是机械判定。
    """
    rep = reconcile()
    drift = [d for d in rep.drifts if d.kind.startswith("role-doc")]
    assert not drift, "角色文档与 Schema 不一致：\n" + "\n".join(str(d) for d in drift)


def test_every_role_doc_has_a_parseable_header() -> None:
    from bg_coordinator.schema import maws_root, parse_doc_header

    root = maws_root() / "agents"
    for spec in ROLES:
        text = (root / f"{spec.key}.md").read_text(encoding="utf-8")
        header = parse_doc_header(text)
        assert header is not None, f"agents/{spec.key}.md 缺可解析头部"
        assert header.role == spec.key


def test_doc_header_is_generated_from_schema() -> None:
    """头部由 Schema 派生——**不手写**，否则又是两份真相。"""
    from bg_coordinator.schema import parse_doc_header, render_doc_header

    for spec in ROLES:
        line = render_doc_header(spec)
        parsed = parse_doc_header(line + "\n")
        assert parsed is not None
        assert parsed.role == spec.key
        assert parsed.form == spec.form
        assert parsed.reports_to == spec.reports_to
        assert parsed.dispatch == spec.dispatch


def test_dash_placeholder_means_no_dispatch() -> None:
    """`dispatch=-` 是"无派单权"——必须解析回空元组，不能变成 `('-',)`。"""
    from bg_coordinator.schema import parse_doc_header

    header = parse_doc_header("> maws: role=x form=subagent reports-to=po dispatch=-\n")
    assert header is not None
    assert header.dispatch == ()


def test_malformed_header_is_reported_not_ignored() -> None:
    """头部残缺 ⇒ 报出来，不静默跳过（静默跳过等于没查）。"""
    from bg_coordinator.schema import parse_doc_header

    assert parse_doc_header("> maws: role=x\n") is None  # 缺 form / reports-to
    assert parse_doc_header("# 无头部\n") is None


def test_field_drift_is_detected() -> None:
    """**对账有齿**：文档字段被改错，必须报出漂移。"""

    from bg_coordinator import schema as mod

    doc = mod.maws_root() / "agents" / "po.md"
    original = doc.read_text(encoding="utf-8")
    try:
        doc.write_text(original.replace("reports-to=user", "reports-to=dba"), encoding="utf-8")
        rep = mod.reconcile()
        assert not rep.consistent
        assert any("reports-to" in str(d) for d in rep.drifts), rep.drifts
    finally:
        doc.write_text(original, encoding="utf-8")
    assert mod.reconcile().consistent

# ---------------------------------------------------------------------------
# 规范面：**一处权威 + 各角色文件指向它**
# ---------------------------------------------------------------------------


def test_reply_style_is_single_sourced_and_referenced() -> None:
    """回复风格**存在于且只存在于一处**，每个角色文件都指向它。

    这条是补一次真实事故的：用户 2026-10-03 定的「回复风格」七条，
    在把体系从散稿收成 `AGENT.md` + `rules/` + `agents/` 时**整段掉了**——
    角色文件里一个字都没有，而没人会发现，因为**丢的不是代码，是散文**。

    所以钉两条：
    ① 权威在 `AGENT.md`（且七条都在——少一条就是又被截了）；
    ② 每个 `agents/*.md` 都指得到它（角色手册不能对"怎么说话"保持沉默）。
    """
    maws = Path(__file__).resolve().parents[2]
    agent = (maws / "AGENT.md").read_text(encoding="utf-8")
    assert "## 九、回复风格" in agent, "AGENT.md 里的「回复风格」节不见了"
    # 2026-10-04 用户把它改成**硬指标**（六条）——护栏跟着钉新版的数字与条目
    for line in ("≤10 行、每行 ≤120 字、单轮 ≤1500 字", "只答被问的", "一条一事", "禁嵌套"):
        assert line in agent, f"回复风格被截断：缺「{line}」"

    missing = [
        p.name
        for p in sorted((maws / "agents").glob("*.md"))
        if "回复风格" not in p.read_text(encoding="utf-8")
        or "AGENT.md" not in p.read_text(encoding="utf-8")
    ]
    assert not missing, f"这些角色文件没有指向回复风格：{missing}"
