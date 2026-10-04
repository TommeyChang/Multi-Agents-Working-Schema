"""迁移链判据——**系统模块**：静态读图的机械事实，不连库、不执行迁移。

## 为什么在系统侧而不是工具侧

同一条判据要在**两个收口点**（提交 `commit_gate.py`／合并 `merge.py`）＋ 那个单命令闸
（`tools/migration_gate.py`）用。判据只写一处，否则各点上的口径会各自漂移——
而迁移恰恰是最不能漂的东西：链位一旦分叉，两个迁移件都"合法"却谁也升不到头。
（第三个收口点"放号"只管**号**，不读图，见 `engine.SERIAL_FAMILIES`。）

## 判据

| # | 判据 | 为什么 |
|---|---|---|
| 1 | `revision` **唯一** | 撞号之后 downgrade／定位全是掷骰子 |
| 2 | 父节点**指向的件存在** | 悬空父节点＝链断了 |
| 3 | **恰有一个 head** | 两个 head ⇒ 谁也不知道该升到哪个 |
| 4 | 从 head **全可达** | 孤儿件＝永远不会被执行（静默失效） |
| 5 | 新增件**取号单调**且父节点 == 基线 head | 号是资源；插队与"重新分叉"都是事故 |
| 6 | **已落库件不得就地改写／删除** | 改的是"已执行过的历史"，改不动已建的表 ⇒ 合入即漂移，目标口径永不生效 |

## [6] 的三条口径

**"已落库"的判据 = 路径在基线树里**（`migrations.base`，本工程 `origin/main`）。
落地过才冻结；本分支未合入的新件不受 [6] 约束——那是 [5] 的地盘（合入前返工合法）。

**判"是不是本分支改的"，比的是状态，不是提交区间**：

| 对照 | 结论 |
|---|---|
| 工作区 == 基线 | 一致（含"并入 main 带进来的"） |
| 分叉点 == 基线 | 基线自分叉起没动过它 ⇒ **差异是本分支的** ⇒ BLOCK |
| 工作区 == 分叉点（基线自己动过） | **落后**，不是改写 ⇒ 不拦（少误杀） |
| 三份各不相同 | 两边都动过 ⇒ BLOCK |
| 基线有、分叉点没有 | 基线新增之后被本分支改了 ⇒ BLOCK |
| 基线有、工作区没有、分叉点有 | **删除／改名历史件** ⇒ BLOCK |
| 基线有、工作区没有、分叉点没有 | 本分支还没合到那一步 ⇒ 不拦（落后） |

工程侧的区间判据（比 `HEAD~1..HEAD`）看不见"本分支早先提交里的改写"——本闸比**状态**，
故早先提交的改写同样报出来。分叉点不可判（无共同祖先／浅克隆）⇒ **只提示不拦**
（判不了归属就不许拿它当罪名）。

**允许的例外**：剥掉 docstring 后 **AST 等价**（注释／docstring／排版级，无语义改动）
⇒ 放行并留痕。注释根本不在 AST 里，故"逐项前后相等"这件事由闸自己算出来，
不需要作者自证；AST 解析不了（语法坏了）⇒ 证不出等价 ⇒ BLOCK。

**它不管的事**：不跑迁移、不改文件、不判降级保真——那些是执行面与工程口径。
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_REVISION = re.compile(r"^revision(?:\s*:\s*[^=\n]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_DOWN_REVISION = re.compile(r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(.+)$", re.M)
_QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")
_NUMERIC = re.compile(r"^(\d+)")


@dataclass
class Migration:
    path: str
    revision: str
    downs: tuple[str, ...]
    #: 归一化字节指纹（CRLF／末尾换行不算改动，其余逐字节算）
    digest: str = ""
    #: 剥掉 docstring 后的 AST 指纹——注释不在 AST 里，故注释级更正它不变；"" = 解析不了
    code: str = ""

    @property
    def number(self) -> int | None:
        m = _NUMERIC.match(self.revision)
        return int(m.group(1)) if m else None


def normalize(text: str) -> str:
    """归一：`CRLF`／`CR` → `LF` 并去掉末尾空行。

    只吸掉**检出差异**（行尾风格、文件末尾那个换行），不掩盖任何内容改动——
    否则"就地改了一行 DDL"会被行尾噪声淹掉。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def digest_of(text: str) -> str:
    """归一化后的内容指纹（人可核：`git show <ref>:<path>` 与工作区逐字比）。"""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:12]


def code_digest(text: str) -> str:
    """剥掉 docstring 后的 AST 指纹——**例外判据**：等价即"无语义改动"。

    注释不参与 AST，故注释级更正天然不变；docstring 是 AST 节点，故逐层摘掉。
    解析不了（语法坏了、不是 Python）⇒ 空串：**证不出等价就不许放行**（fail closed）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ""
    for node in list(ast.walk(tree)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            head = body[0] if body else None
            if (
                isinstance(head, ast.Expr)
                and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return hashlib.sha1(ast.dump(tree).encode("utf-8")).hexdigest()[:12]


def parse_downs(text: str) -> tuple[str, ...]:
    """取 `down_revision` 的值：单值或元组，`None` 即无父。"""
    m = _DOWN_REVISION.search(text)
    if not m:
        return ()
    rhs = m.group(1).strip()
    if rhs[:1] in "([":
        return tuple(_QUOTED.findall(rhs))
    q = _QUOTED.search(rhs)
    return (q.group(1),) if q else ()


def parse_text(text: str, rel: str) -> Migration | None:
    """从**文本**解析一个迁移件——不 import（那是执行任意代码）。"""
    rev = _REVISION.search(text)
    if not rev:
        return None
    return Migration(
        path=rel,
        revision=rev.group(1),
        downs=parse_downs(text),
        digest=digest_of(text),
        code=code_digest(text),
    )


def parse_file(path: Path, rel: str) -> Migration | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return parse_text(text, rel)


def load_migrations(target: Path, versions_dir: str) -> list[Migration]:
    root = target / versions_dir
    if not root.is_dir():
        return []
    out: list[Migration] = []
    for f in sorted(root.rglob("*.py")):
        if f.name.startswith("__"):
            continue
        m = parse_file(f, str(f.relative_to(target)))
        if m:
            out.append(m)
    return out


def load_from_git(target: Path, base: str, versions_dir: str) -> tuple[list[Migration], str]:
    """基线里的迁移图——`git show <base>:<path>` 逐个取，**不切工作区**。"""
    ls = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base, versions_dir],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if ls.returncode != 0:
        return [], f"基线不可用：{base}"
    out: list[Migration] = []
    for rel in [ln.strip() for ln in ls.stdout.splitlines() if ln.strip().endswith(".py")]:
        show = subprocess.run(
            ["git", "show", f"{base}:{rel}"], cwd=target, capture_output=True, text=True, check=False
        )
        if show.returncode != 0:
            continue
        m = parse_text(show.stdout, rel)
        if m:
            out.append(m)
    return out, ""


def heads_of(rows: list[Migration]) -> list[str]:
    parents = {d for r in rows for d in r.downs}
    return sorted(r.revision for r in rows if r.revision not in parents)


@dataclass(frozen=True)
class Landed:
    """[6] 的对照系：**已落库面**（基线树）＋ **分叉点面**（merge-base 树）。

    - `base`：基线树里 迁移件路径 → blob sha（"已落库"＝路径在基线树里）；
    - `fork`：分叉点树里的同一映射；`None` = 分叉点不可判（无共同祖先／浅克隆）
      ⇒ [6] 只提示不拦——**判不了归属就不许拿它当罪名**；
    - `fork_digest`：只装"基线自分叉点之后动过"的那些件在**分叉点**上的内容指纹
      （常态为空集），用来把「落后于基线」与「本分支改写」分开。
    """

    base: dict[str, str]
    fork: dict[str, str] | None
    fork_digest: dict[str, str] = field(default_factory=dict)


def _ls_tree(target: Path, ref: str, versions_dir: str) -> dict[str, str] | None:
    """`ref` 树里迁移目录的 路径 → blob sha。取不到（ref 不在／不是仓）⇒ `None`。"""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", ref, "--", versions_dir],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if path.endswith(".py") and len(parts) >= 3:
            out[path] = parts[2]
    return out


def load_landed(target: Path, base: str, ref: str, versions_dir: str) -> Landed | None:
    """读 [6] 的对照系：两条 `git ls-tree`（基线／分叉点）＋ 一次 `merge-base`。

    内容只在**基线自分叉点动过**的件上取（常态 0 次 `git show`）——那正是"落后"与
    "改写"需要分开的少数情形。基线树读不到 ⇒ `None`（调用方据此说清"判据未生效"）。
    """
    base_shas = _ls_tree(target, base, versions_dir)
    if base_shas is None:
        return None
    mb = subprocess.run(
        ["git", "merge-base", base, ref], cwd=target, capture_output=True, text=True, check=False
    )
    fork_ref = mb.stdout.strip()
    if mb.returncode != 0 or not fork_ref:
        return Landed(base=base_shas, fork=None)
    fork_shas = _ls_tree(target, fork_ref, versions_dir)
    if fork_shas is None:
        return Landed(base=base_shas, fork=None)
    fork_digest: dict[str, str] = {}
    moved = sorted(p for p, sha in fork_shas.items() if p in base_shas and base_shas[p] != sha)
    for path in moved:
        show = subprocess.run(
            ["git", "show", f"{fork_ref}:{path}"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode == 0:
            fork_digest[path] = digest_of(show.stdout)
    return Landed(base=base_shas, fork=fork_shas, fork_digest=fork_digest)


#: 撞上 [6] 之后的正当路径——不让读者猜该找谁
_REMEDY = (
    "DDL 变更一律**另开下一 revision 补偿式承载**"
    "（另立条目：dba 评审 ＋ ops 窗口）；未落库的新件不受此限"
)


def landed_findings(
    rows: list[Migration], base_rows: list[Migration], landed: Landed | None
) -> list[tuple[str, str]]:
    """[6] 的判据本体（纯函数）：返回 `[(级别, 说明)]`，级别 ∈ `block`／`note`。

    判据表见模块 docstring。**注释／排版级更正**在这里被判为例外（`note`），
    分叉点不可判也在这里降级成 `note`——两者都不许变成拦。
    """
    if landed is None or not base_rows:
        return []
    disk = {r.path: r for r in rows}
    out: list[tuple[str, str]] = []
    undecidable: list[str] = []
    for b in sorted(base_rows, key=lambda r: r.path):
        d = disk.get(b.path)
        if d is None:
            if landed.fork is not None and b.path in landed.fork:
                out.append(
                    (
                        "block",
                        f"[6] 已落库迁移件在工作区里没了：{b.path}——**删除或改名历史件**"
                        "（链上别人的父节点会悬空，已落库的版本无从降级）；"
                        f"恢复该件（`git checkout <基线> -- {b.path}`）",
                    )
                )
            continue
        if d.digest == b.digest:
            continue
        if d.code and b.code and d.code == b.code:
            out.append(
                (
                    "note",
                    f"[6] 注释／docstring 级更正（剥 docstring 后 AST 等价，无语义改动）"
                    f"——允许的例外：{b.path}",
                )
            )
            continue
        if landed.fork is None:
            undecidable.append(b.path)
            continue
        base_sha = landed.base.get(b.path)
        fork_sha = landed.fork.get(b.path)
        if b.path not in landed.fork:
            why = "基线里有、分叉点还没有 ⇒ 基线新增之后被本分支改了"
        elif base_sha == fork_sha:
            why = "基线自分叉点起没动过它 ⇒ 差异是本分支的"
        elif d.digest == landed.fork_digest.get(b.path):
            continue  # 落后：本分支手里还是分叉点那份，是基线自己动了它
        else:
            why = "分叉点、基线、工作区三份各不相同"
        out.append(("block", f"[6] 已落库迁移件被就地改写：{b.path}（{why}）——{_REMEDY}"))
    if undecidable:
        out.append(
            (
                "note",
                f"[6] 分叉点不可判（与基线无共同祖先／浅克隆）⇒ **只提示不拦**："
                f"{undecidable} 与已落库内容不同，但「本分支改的」还是「基线自己动的」判不出来；"
                "「没了的件」同判不了（分不开「落后」与「删了」）",
            )
        )
    return out


def judge(
    rows: list[Migration],
    base_rows: list[Migration],
    versions_dir: str = "",
    *,
    landed: Landed | None = None,
) -> list[str]:
    """返回 BLOCK 清单（空 = 全过）。**纯函数**：三个收口点共用同一条判据。

    `landed`（`load_landed` 的产物）给了才判 [6]；不给＝没有已落库面可比（无基线）。
    """
    blocks: list[str] = []
    if not rows:
        where = versions_dir or "迁移目录"
        return [f"{where} 里没有可解析的迁移件"]

    seen: dict[str, str] = {}
    for r in rows:
        if r.revision in seen:
            blocks.append(f"[1] revision 撞号 {r.revision}：{seen[r.revision]} 与 {r.path}")
        else:
            seen[r.revision] = r.path
    known = set(seen)
    for r in rows:
        for d in r.downs:
            if d not in known:
                blocks.append(f"[2] {r.path} 的 down_revision={d} 在盘上不存在（悬空父节点）")

    heads = heads_of(rows)
    if len(heads) > 1:
        blocks.append(f"[3] 有 {len(heads)} 个 head：{heads}——**链位分叉**，升级目标不唯一")
    elif not heads:
        blocks.append("[3] 没有 head——图里有环或全部互为父节点")

    if len(heads) == 1:
        by_rev = {r.revision: r for r in rows}
        stack, reachable = [heads[0]], set()
        while stack:
            cur = stack.pop()
            if cur in reachable or cur not in by_rev:
                continue
            reachable.add(cur)
            stack.extend(by_rev[cur].downs)
        orphans = sorted(known - reachable)
        if orphans:
            blocks.append(f"[4] 从 head 不可达（永远不会被执行）：{orphans}")

    if base_rows:
        base_nums = [r.number for r in base_rows if r.number is not None]
        base_max = max(base_nums) if base_nums else 0
        base_head = heads_of(base_rows)
        base_revs = {r.revision for r in base_rows}
        for r in rows:
            if r.revision in base_revs:
                continue  # 基线里已有的件不判取号（它们是 [6] 的地盘：只许原样不动）
            if r.number is None:
                blocks.append(f"[5] 新增件 {r.path} 的 revision 不以数字开头：{r.revision}")
                continue
            if r.number <= base_max:
                blocks.append(f"[5] 新增件 {r.path} 取号 {r.revision} 不大于基线最大号 {base_max}——插队")
            if len(base_head) == 1 and r.downs != (base_head[0],):
                blocks.append(
                    f"[5] 新增件 {r.path} 的父节点 {list(r.downs)} ≠ 基线 head {base_head[0]}"
                    "——**重新分叉**（别人先落地了，你要把父节点接到新 head 上）"
                )

    # [6] 已落库件不得就地改写／删除——比的是**状态**（已落库面 vs 当前工作区／分支树），
    # 故"本分支早先提交里的改写"也照报；判据本体在 `landed_findings`（同一处）。
    blocks.extend(msg for level, msg in landed_findings(rows, base_rows, landed) if level == "block")
    return blocks


def landed_notes(
    rows: list[Migration], base_rows: list[Migration], landed: Landed | None
) -> list[str]:
    """[6] 的**非拦**产出：允许的例外（注释级更正）与"判不出来"的提示。

    单独一个面，是因为拦的语义只有一种（`judge` 非空即拦），而注记不该拦人。
    """
    return [msg for level, msg in landed_findings(rows, base_rows, landed) if level == "note"]
