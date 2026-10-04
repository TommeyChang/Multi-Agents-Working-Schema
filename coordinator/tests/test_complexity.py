"""复杂度闸（ratchet）——**判据要有齿，且不许误杀**。

判据本体在 `tools/complexity.py`（radon 只出数，红绿由我们的判据说）。
测试用**注入的 radon 桩**（两个环境变量给两棵树的数据）：判据是纯逻辑，
不该因为"这台机器装没装 radon"而测不了；真 radon 另有一条 skip 型用例兜底。

控制例成对出现：**该红的必红、不该红的必不红**——这条闸的假红形态（把
"落后于基线"算成"本分支变差"）正是主仓在迁移面打过的两轮补丁，这里逐条钉住。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

COORD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COORD))

_SPEC = importlib.util.spec_from_file_location("complexity", COORD / "tools" / "complexity.py")
assert _SPEC and _SPEC.loader
cx = importlib.util.module_from_spec(_SPEC)
sys.modules["complexity"] = cx
_SPEC.loader.exec_module(cx)

#: 桩：按 cwd 判断这次是"工作区"还是"分叉点树"，各读各的环境变量。
#: radon 的 JSON 形状照抄真件（只取我们判据要用的键）。
_STUB = '''\
import json, os, sys
args = sys.argv[1:]              # ["cc", "-j", *paths]
key = "FAKE_RADON_BASELINE" if "maws-cx-" in os.getcwd() else "FAKE_RADON_CURRENT"
data = json.loads(os.environ.get(key, "{}"))
paths = args[2:]
out = {
    p: v
    for p, v in data.items()
    if any(p == q or p.startswith(q.rstrip("/") + "/") for q in paths)
}
print(json.dumps(out))
'''


def _item(name: str, rank: str, complexity: int, *, type_: str = "function", cls: str = "") -> dict:
    item = {"type": type_, "rank": rank, "complexity": complexity, "name": name, "lineno": 1}
    if cls:
        item["classname"] = cls
    return item


@pytest.fixture
def stub(tmp_path: Path) -> str:
    """radon 桩的命令行（`--radon` 的值）。"""
    path = tmp_path / "fake_radon.py"
    path.write_text(_STUB, encoding="utf-8")
    return f"{sys.executable} {path}"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """真 git 仓 + 一条基线：分叉点对照必须有真树可导。"""
    r = tmp_path / "repo"
    (r / "app").mkdir(parents=True)
    (r / "app" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    return r


def _run(
    repo: Path,
    stub: str,
    current: dict,
    baseline: dict,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
) -> int:
    monkeypatch.setenv("FAKE_RADON_CURRENT", json.dumps(current))
    monkeypatch.setenv("FAKE_RADON_BASELINE", json.dumps(baseline))
    return cx.main(
        [
            "--target",
            str(repo),
            "--base",
            "base",
            "--paths",
            "app",
            "--radon",
            stub,
            *extra,
        ]
    )


# ---------------------------------------------------------------------------
# 该红的：新增与变差
# ---------------------------------------------------------------------------


def test_new_block_at_floor_is_blocked(repo, stub, monkeypatch, capsys) -> None:
    """本分支**新增**一个达到档位的块 ⇒ BLOCK（ratchet 治的就是"新欠的债"）。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "A", 2), _item("big", "C", 12)]},
        {"app/core.py": [_item("f", "A", 2)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "新增" in out and "big" in out


def test_new_block_below_floor_only_warns(repo, stub, monkeypatch, capsys) -> None:
    """档位之下（B）只告警，不拦——否则闸会因为"稍微复杂一点"就拦住所有正常改动。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "A", 2), _item("mid", "B", 7)]},
        {"app/core.py": [_item("f", "A", 2)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WARN" in out and "mid" in out


def test_worsened_rank_at_floor_is_blocked(repo, stub, monkeypatch, capsys) -> None:
    """已有块**等级变差到档位** ⇒ BLOCK。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "C", 11)]},
        {"app/core.py": [_item("f", "A", 4)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "变差" in out and "A" in out and "C" in out


def test_worsened_rank_below_floor_only_warns(repo, stub, monkeypatch, capsys) -> None:
    """等级变差但仍在档位下（A→B）⇒ WARN，不拦。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "B", 8)]},
        {"app/core.py": [_item("f", "A", 4)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WARN" in out and "变差" in out


def test_complexity_growth_same_rank_warns(repo, stub, monkeypatch, capsys) -> None:
    """等级没变但复杂度涨了（且已在告警档之上）⇒ WARN：留痕，不拦。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "D", 26)]},
        {"app/core.py": [_item("f", "D", 25)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "上升" in out


# ---------------------------------------------------------------------------
# 不该红的：变简单、落后、分叉点不可判
# ---------------------------------------------------------------------------


def test_simplified_or_removed_is_not_blocked(repo, stub, monkeypatch, capsys) -> None:
    """代码变简单／整块消失 ⇒ 不判（ratchet 只管变差，不管变好）。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "A", 2)]},
        {"app/core.py": [_item("f", "A", 9), _item("gone", "F", 42)]},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "结论 放行" in out and "\n  BLOCK" not in out


def test_behind_branch_is_not_blocked(repo, stub, monkeypatch, capsys) -> None:
    """**落后不是变差**：基线那边新增的复杂块，本分支手里没有 ⇒ 不许算到它头上。

    这是本条闸最容易误杀的形态；对照"分叉点"而不是"基线树"就是为了堵它。
    """
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "A", 2)]},  # 本分支：还是分叉点那份
        {"app/core.py": [_item("f", "C", 15)]},  # 基线：别人把它改复杂了
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, f"{out}\n把基线侧的改动算成本分支的变差 = 假红"


def test_unknown_fork_only_notes(tmp_path, stub, monkeypatch, capsys) -> None:
    """分叉点不可判（无共同祖先）⇒ **只提示不拦**：判不了归属就不许拿它当罪名。"""
    r = tmp_path / "orphan"
    (r / "app").mkdir(parents=True)
    (r / "app" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    _git(r, "checkout", "-q", "--orphan", "other")
    _git(r, "rm", "-rq", "--cached", ".")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "orphan")

    rc = _run(
        r,
        stub,
        {"app/core.py": [_item("f", "F", 61)]},
        {},
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "分叉点不可判" in out


# ---------------------------------------------------------------------------
# 环境与落点：缺什么就红，不猜
# ---------------------------------------------------------------------------


def test_missing_radon_exits_2(repo, monkeypatch, capsys) -> None:
    """**radon 不在 ⇒ 退出 2**（fail closed）——"必须使用"就落在这一条上。"""
    monkeypatch.setenv("FAKE_RADON_CURRENT", "{}")
    rc = cx.main(
        ["--target", str(repo), "--base", "base", "--paths", "app", "--radon", "no-such-radon-cmd"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "radon 不可用" in err and "缺它即红" in err


def test_missing_paths_exits_2(repo, capsys) -> None:
    """没给路径、绑定也没声明 ⇒ **拒绝猜**（落点不明不许静默全量）。"""
    rc = cx.main(["--target", str(repo), "--base", "base"])
    assert rc == 2
    assert "路径" in capsys.readouterr().err


def test_missing_base_exits_2(repo, capsys) -> None:
    """没有对照系就判不出"新欠的债" ⇒ 退出 2。"""
    rc = cx.main(["--target", str(repo), "--paths", "app"])
    assert rc == 2
    assert "基线" in capsys.readouterr().err


def test_bad_rank_exits_2(repo, capsys) -> None:
    rc = cx.main(["--target", str(repo), "--base", "base", "--paths", "app", "--floor", "Z"])
    assert rc == 2


def test_json_carries_distribution(repo, stub, monkeypatch, capsys) -> None:
    """**绿地跑也要留分布证据**：没有它，阈值就是拍的。"""
    rc = _run(
        repo,
        stub,
        {"app/core.py": [_item("f", "A", 2), _item("g", "C", 12), _item("h", "C", 14)]},
        {"app/core.py": [_item("f", "A", 2), _item("g", "C", 12), _item("h", "C", 14)]},
        monkeypatch,
        "--json",
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert payload["distribution"]["A"] == 1
    assert payload["distribution"]["C"] == 2
    assert payload["blocks_total"] == 3


# ---------------------------------------------------------------------------
# 真 radon：端到端一条（没装就跳过）
# ---------------------------------------------------------------------------


def _real_radon_python() -> str | None:
    """找一个装了 radon 的解释器（产品仓 venv 优先）——测试不因此变脆。"""
    for cand in (
        COORD.parent.parent / "futures-broker-gateway" / ".venv" / "bin" / "python",
        Path(sys.executable),
    ):
        if not Path(cand).exists():
            continue
        proc = subprocess.run(
            [str(cand), "-c", "import radon"], capture_output=True, text=True, check=False
        )
        if proc.returncode == 0:
            return str(cand)
    return None


def test_real_radon_end_to_end(tmp_path: Path) -> None:
    """真 radon 跑一遍：新增一个 C 级函数 ⇒ BLOCK；原样 ⇒ 放行。"""
    python = _real_radon_python()
    if python is None:
        pytest.skip("本机没有装了 radon 的解释器")

    repo = tmp_path / "real"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "base")

    args = [
        "--target",
        str(repo),
        "--base",
        "base",
        "--paths",
        "app",
        "--radon",
        f"{python} -m radon",
    ]
    assert cx.main(args) == 0, "原样不动 ⇒ 必须放行（否则是假红）"

    body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(12))
    (repo / "app" / "core.py").write_text(
        f"def f():\n    return 1\n\n\ndef big(x):\n{body}\n    return -1\n", encoding="utf-8"
    )
    assert cx.main(args) == 1, "新增 C 级函数没拦住"


def test_declared_gate_is_actually_installed() -> None:
    """产品仓的 dev 依赖里声明了 radon，且 venv 里真的装了。

    这条**不 skip**（除非产品仓不在本机）：绑定声明了复杂度闸却不装 ⇒ 门禁必红，
    那正是要暴露的事，而不是让测试悄悄跳过。
    """
    target = COORD.parent.parent / "futures-broker-gateway"
    venv = target / ".venv" / "bin" / "python"
    if not (target / "pyproject.toml").is_file() or not venv.exists():
        pytest.skip("产品仓（或其 venv）不在本机")
    text = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert "radon" in text, "产品仓 dev 依赖里没有 radon —— 绑定声明了却没人装"
    proc = subprocess.run([str(venv), "-c", "import radon"], capture_output=True, check=False)
    assert proc.returncode == 0, "声明了 radon 但 venv 里没有 ⇒ 门禁会红（先 uv sync）"


def _product_list_constants(path: Path, names: tuple[str, ...]) -> dict[str, list[str]] | None:
    """从产品仓源码里**用 ast 读常量**（不 import：那是执行别人的代码）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in names
        ):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
        out[node.target.id] = [str(v) for v in value]
    return out or None


def _face_diff(declared: set[str], product: set[str]) -> str:
    """两处扫描面的差异（空串 = 一致）——本体抽出来，好让负例也能打它。"""
    if declared == product:
        return ""
    return (
        "绑定 §门禁 complexity.paths 与产品仓 C20 的扫描面不一致："
        f"只在绑定里 {sorted(declared - product)}；只在 C20 里 {sorted(product - declared)}"
    )


def test_scan_face_has_a_single_source(tmp_path: Path) -> None:
    """**扫描面只许有一处真相**：绑定声明的 `paths` ≡ 产品仓 C20 的常量。

    本体系自己的第一定律：两处并存必然漂移。这里真的并存了两处——
    工程侧 C20（存量台账）与本闸（新增闸）要判**同一个面**，否则同一个函数
    一边黄一边绿。这条闸把"同一件事写了两遍"变成一次可核的比对：对不上就红。
    """
    target = COORD.parent.parent / "futures-broker-gateway"
    src = target / "scripts" / "tools" / "process_audit" / "_checks_quality.py"
    if not src.is_file():
        pytest.skip("产品仓（或它的 C20）不在本机")
    consts = _product_list_constants(src, ("RADON_SCAN_DIRS", "RADON_SCAN_FILES"))
    if not consts:
        pytest.skip("C20 的扫描面不是字面量常量（形状变了：请人工核）")

    product = set(consts.get("RADON_SCAN_DIRS", [])) | set(consts.get("RADON_SCAN_FILES", []))
    declared = set(cx.binding_complexity(target).get("paths") or [])
    assert _face_diff(declared, product) == "", _face_diff(declared, product)

    # **负例（防空绿）**：比对本身必须能逮住漂移；解析器也必须真读到了东西。
    assert _face_diff(declared, product | {"phantom"}) != ""
    probe = tmp_path / "probe.py"
    probe.write_text(
        'RADON_SCAN_DIRS: tuple[str, ...] = ("only_here",)\nRADON_SCAN_FILES: tuple[str, ...] = ()\n',
        encoding="utf-8",
    )
    parsed = _product_list_constants(probe, ("RADON_SCAN_DIRS", "RADON_SCAN_FILES"))
    assert parsed == {"RADON_SCAN_DIRS": ["only_here"], "RADON_SCAN_FILES": []}, parsed
