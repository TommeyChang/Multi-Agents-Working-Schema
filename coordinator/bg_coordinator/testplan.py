"""测试分级——**哪一级在哪个动作上必须齐**（判据单源，工具只做外壳）。

用户口径（2026-10-05）：开发过程中的测试要**分多级**，而且**不能全量跑**。

四级（与工程已有标记对齐，不新造名词）：

| 级 | 内容 | 何时必然跑 |
|---|---|---|
| `static` | ruff ＋ 复杂度 ratchet ＋ 迁移闸 ＋ 界内闸 | 每次改动、提交前 |
| `affected` | 只跑改动映射到的域（`-m "not db"` 快跑通道） | 交付前 |
| `domain` | 所涉域整域（含真库档），真库档不并发 | 合并前 |
| `full` | 全量（＋真机档择窗） | 仅发布，或命中公共面 |

**判据的形状**是「改动文件 → 域」：单域 ⇒ `affected`；跨域 ⇒ `domain`；
命中公共面 ⇒ `full`。**映射不明 ⇒ 不作罪名**（返回 `None`），但必须记缺口：
本体系的口径是"判不了归属就不许拦"，而"算不出该跑哪级"是要人补映射的信号。

数据来自工程绑定 §四·五（`domains`／`public`／`budget` 三类必填）——
**体系管义务，工程管事实**：域与目录的对应、哪些是公共面、每级实测多久，都是工程事实。
"""

from __future__ import annotations

from pathlib import Path

#: 从宽到严（顺序即严格程度，`meets()` 靠它比）
TIERS: tuple[str, ...] = ("static", "affected", "domain", "full")
RANK: dict[str, int] = {t: i for i, t in enumerate(TIERS)}


def normalize(files: list[str]) -> list[str]:
    """统一成仓内相对路径（去 `./`、去前导 `/`）。"""
    return [f.strip().lstrip("/").removeprefix("./") for f in files if f and f.strip()]


def _domain_of(path: str, domains: dict[str, list[str]]) -> str | None:
    """按**最长前缀**归属；都不匹配 ⇒ None（不明）。"""
    best: tuple[int, str] | None = None
    for name, prefixes in domains.items():
        for pre in prefixes:
            p = pre.strip().lstrip("/").removeprefix("./")
            if not p:
                continue
            if (path == p or path.startswith(p.rstrip("/") + "/")) and (
                best is None or len(p) > best[0]
            ):
                best = (len(p), name)
    return best[1] if best else None


def required_scope(
    changed_files: list[str],
    domains: dict[str, list[str]],
    public: tuple[str, ...] = (),
) -> tuple[str | None, list[str]]:
    """算出这条改动**至少要跑到哪一级**；返回（级, 理由）。

    `None` = 判不了（没改动、没映射、或映射不明）——**判不了就不当罪名**，
    但理由里必须写清缺什么，好让人去补绑定。
    """
    files = normalize(changed_files)
    if not files:
        return None, ["没有改动文件可判（映射算不出来）"]
    if not domains:
        return None, ["绑定缺「域 → 路径」映射（`testplan.domains`），无法判该跑哪级"]

    hits: dict[str, list[str]] = {}
    unknown: list[str] = []
    for f in files:
        d = _domain_of(f, domains)
        if d is None:
            unknown.append(f)
        else:
            hits.setdefault(d, []).append(f)
    # **公共面优先**：命中即全量，不看域映射（公共面改动本就最该全量，
    # 不该被"域映射没填"挡住——那正好会把最该跑全量的改动放行成 affected）。
    pub = [f for f in files if any(
        f == p.strip().lstrip("/") or f.startswith(p.strip().lstrip("/").rstrip("/") + "/")
        for p in public if p.strip()
    )]
    if pub:
        return "full", [f"命中公共面：{'、'.join(pub[:3])}（公共面改动必须全量）"]
    if unknown:
        return None, [f"映射不明：{'、'.join(unknown[:3])}"
                      + ("…" if len(unknown) > 3 else "") + "——先补绑定，别默认跳过"]
    if len(hits) > 1:
        return "domain", [f"跨 {len(hits)} 个域：{'、'.join(sorted(hits))}"]
    d = next(iter(hits))
    return "affected", [f"只在域 `{d}` 内：{'、'.join(hits[d][:3])}"]


def meets(required: str | None, actual: str) -> bool:
    """实测/申报的级别是否够。`required=None`（判不了）**一律算够**——
    这是"失败方向"的选择：判不了不放行是假红，比没闸更坏。"""
    if required is None:
        return True
    return RANK.get(actual, -1) >= RANK.get(required, 99)


def load(block: dict) -> tuple[dict[str, list[str]], tuple[str, ...], dict[str, int]]:
    """从绑定 §四·五 的机读块取（`domains`／`public`／`budget`）。"""
    domains = {
        str(k): [str(x) for x in v] for k, v in dict(block.get("domains") or {}).items()
    }
    public = tuple(str(x) for x in (block.get("public") or []))
    budget = {str(k): int(v) for k, v in dict(block.get("budget") or {}).items()}
    return domains, public, budget


def changed_in_repo(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    """`git diff --name-only base head`——外壳用，判据不依赖它。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return normalize(out.stdout.splitlines())
