"""DBA／OPS 侧工具的判据测试——**安全方向必须可证**。

这三件工具都带"不许动错东西"的承诺，而这些承诺**只能靠判据测**：

- `scratch_gc`：默认 dry-run；保护名单／模板池永不回收；活着的属主、有连接的库一律跳过。
- `probe`：连续失败达阈才算卡死；**代码里没有任何重启路径**。
- `preflight`：任一 BLOCK 即拒绝开窗；**它自己不删任何东西**。
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

COORD = Path(__file__).resolve().parents[1]
TOOLS = COORD / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load("scratch_gc")
probe = _load("probe")
preflight = _load("preflight")

Fact = gc.Fact


# ---------------------------------------------------------------------------
# scratch_gc：判据
# ---------------------------------------------------------------------------


def _judge(facts, **kw):
    base = {"prefixes": ("bg_",), "include_nopid": False, "ttl_min": 60}
    base.update(kw)
    return {v.name: v for v in gc.judge(facts, **base)}


def test_dry_run_is_the_default() -> None:
    """**默认不动手**是一条安全承诺——所以它必须可测（参数默认值）。"""
    args = gc.build_parser().parse_args([])
    assert args.apply is False
    assert args.include_nopid is False


def test_non_namespace_never_touched() -> None:
    """非 `bg_` 族一律不动——铁律（用**不在豁免名单**里的业务库验证，才测得到这一条）。"""
    v = _judge([Fact("shop_prod", 9999, 0, False, None)])["shop_prod"]
    assert v.action == "skip"
    assert "命名空间" in v.reason


def test_assets_are_exempt_even_when_orphaned() -> None:
    """保护名单与**模板池**永不回收——命名空间放宽后，这是唯一挡住误删的闸。

    实测：真跑 dry-run 时 `bg_tmpl_0036` 正因这条被跳过；少了它，模板池资产会被吃掉。
    """
    facts = [
        Fact("broker_gateway_dev", 9999, 0, False, 999999),
        Fact("bg_tmpl_0036", 9999, 0, False, 999999),
    ]
    got = _judge(facts)
    assert got["broker_gateway_dev"].action == "skip"
    assert got["bg_tmpl_0036"].action == "skip"
    assert all("豁免" in v.reason for v in got.values())


def test_dead_owner_pid_is_an_orphan() -> None:
    facts = [Fact("bg_demo_abc12345_999999_1a2b3c4d", 5, 0, False, 999999)]
    v = _judge(facts, alive=lambda pid: False)["bg_demo_abc12345_999999_1a2b3c4d"]
    assert v.action == "reap"
    assert "已死" in v.reason


def test_live_owner_pid_is_skipped() -> None:
    """**失败方向只「少删」**：属主还活着就不动，哪怕它看起来很老。"""
    facts = [Fact("bg_demo_abc12345_12345_1a2b3c4d", 9999, 0, False, 12345)]
    v = _judge(facts, alive=lambda pid: True)["bg_demo_abc12345_12345_1a2b3c4d"]
    assert v.action == "skip"
    assert "存活" in v.reason


def test_active_connection_blocks_reaping() -> None:
    """判定与动手之间别人可能刚连上——有连接就不动（实测真跑时正是这样跳过一个库）。"""
    v = _judge([Fact("bg_demo_abc12345_999999_1a2b3c4d", 9999, 1, False, 999999)],
               alive=lambda pid: False)["bg_demo_abc12345_999999_1a2b3c4d"]
    assert v.action == "skip"
    assert "活动连接" in v.reason


def test_metadata_lock_blocks_reaping() -> None:
    v = _judge([Fact("bg_demo_abc12345_999999_1a2b3c4d", 9999, 0, True, 999999)],
               alive=lambda pid: False)["bg_demo_abc12345_999999_1a2b3c4d"]
    assert v.action == "skip"
    assert "元数据锁" in v.reason


def test_nopid_family_needs_explicit_optin() -> None:
    """无 pid 族**不得按前缀无条件删**——没有活性判据，就必须显式要。"""
    name = "bg_offchain_snapshot"
    assert _judge([Fact(name, 9999, 0, False, None)])[name].action == "skip"
    got = _judge([Fact(name, 9999, 0, False, None)], include_nopid=True)[name]
    assert got.action == "reap"
    assert "TTL" in got.reason


def test_nopid_unknown_age_is_skipped() -> None:
    """年龄未知（空库／无 create_time）⇒ 不认领——**不知道多老就别动**。"""
    got = _judge([Fact("bg_offchain_snapshot", None, 0, False, None)], include_nopid=True)
    assert got["bg_offchain_snapshot"].action == "skip"
    assert "年龄未知" in got["bg_offchain_snapshot"].reason


def test_nopid_young_is_skipped() -> None:
    got = _judge([Fact("bg_offchain_snapshot", 30, 0, False, None)], include_nopid=True)
    assert got["bg_offchain_snapshot"].action == "skip"


def test_owner_pid_parsed_from_name_tail() -> None:
    assert gc.parse_owner_pid("bg_da_contractfn_9f3a1b2c_4242_deadbeef") == 4242
    assert gc.parse_owner_pid("bg_offchain_snapshot") is None
    # 只有**尾部**的 `_<pid>_<rand8>` 才算——中间出现不算
    assert gc.parse_owner_pid("bg_12345678_x_00000000_extra") is None


def test_gc_has_no_unconditional_drop() -> None:
    """DROP 只能出现在 `recheck_and_drop` 里（二次判定之后）——别处不许有。"""
    src = (TOOLS / "scratch_gc.py").read_text(encoding="utf-8")
    drop_owner = src[src.index("def recheck_and_drop") : src.index("def build_parser")]
    assert "DROP DATABASE" in drop_owner
    assert src.count("DROP DATABASE") == 1, "DROP 只准有一处（二次判定之内）"


# ---------------------------------------------------------------------------
# probe：只告警不重启
# ---------------------------------------------------------------------------


def _code_only(path: Path) -> str:
    """去掉文档串与注释，只留**代码**——判"有没有某条路"要看代码，不是看文档。"""
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno
        ):
            doc_lines.update(range(node.lineno, node.end_lineno + 1))
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines or line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_probe_has_no_restart_path() -> None:
    """**没有任何重启路径**：没有 subprocess、没有 systemctl、没有 os.kill。

    "探活不得重启"是红线；靠自觉守不住，所以判据是**代码里根本没有那条路**。
    """
    code = _code_only(TOOLS / "probe.py")
    for forbidden in ("subprocess", "systemctl", "os.kill", "Popen", "kill("):
        assert forbidden not in code, f"探针**代码**里出现了 {forbidden}——红线破了"


def _server() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_alive_then_dead_after_threshold(tmp_path: Path) -> None:
    srv, port = _server()
    url = f"http://127.0.0.1:{port}/"
    rc = probe.main(["--url", url, "--state-dir", str(tmp_path), "--fail-threshold", "2"])
    assert rc == 0, "服务活着必须退出 0"
    srv.shutdown()

    dead = f"http://127.0.0.1:{_free_port()}/"
    assert probe.main(["--url", dead, "--state-dir", str(tmp_path), "--fail-threshold", "2",
                       "--timeout", "1"]) == 0, "第一次失败未达阈值 ⇒ 仍 0"
    assert probe.main(["--url", dead, "--state-dir", str(tmp_path), "--fail-threshold", "2",
                       "--timeout", "1"]) == 1, "达阈值 ⇒ 判卡死"


def test_probe_state_file_records_failures(tmp_path: Path) -> None:
    dead = f"http://127.0.0.1:{_free_port()}/"
    probe.main(["--url", dead, "--state-dir", str(tmp_path), "--timeout", "1"])
    state = probe.state_path(dead, tmp_path)
    assert json.loads(state.read_text(encoding="utf-8"))["failures"] == 1


# ---------------------------------------------------------------------------
# preflight：判据前移
# ---------------------------------------------------------------------------


def test_preflight_never_deletes_anything() -> None:
    """预检**只判**：它连 `--apply` 都不该传给清库工具。"""
    code = _code_only(TOOLS / "preflight.py")
    assert '"--apply"' not in code, "预检把 --apply 传下去了——它只判，不动手"


def test_preflight_host_leg_blocks_on_low_memory() -> None:
    leg = preflight.leg_host(load_ratio=100.0, min_free_mb=10**9)
    assert not leg.ok
    assert "可用内存" in leg.detail


def test_preflight_reports_blocks() -> None:
    rep = preflight.Report(legs=[preflight.Leg("a", True), preflight.Leg("b", False, "坏了")])
    assert [x.name for x in rep.blocked] == ["b"]


def test_preflight_runs_and_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """真跑一遍：退出码 ∈ {0,1}，JSON 五条腿齐全。"""
    rc = preflight.main(
        [
            "--target", str(COORD.parent.parent / "futures-broker-gateway"),
            "--root", str(tmp_path / "coord"),
            "--load-ratio", "1000",
            "--min-free-mb", "0",
            "--json",
        ]
    )
    assert rc in (0, 1)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {leg["name"] for leg in payload["legs"]} == {
        "协调器自检", "迁移图", "触库可用", "scratch 残留", "宿主余量"
    }
    assert payload["ok"] is (not payload["blocked"])


def test_ops_dba_tools_run_as_plain_python3() -> None:
    """自持：三件都要能以脚本方式直跑（不靠 PYTHONPATH、不靠 venv）。"""
    for name in ("scratch_gc.py", "probe.py", "preflight.py"):
        proc = subprocess.run(
            [sys.executable, str(TOOLS / name), "--help"], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, f"{name} --help 失败：{proc.stderr[:200]}"
