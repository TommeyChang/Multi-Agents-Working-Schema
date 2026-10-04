"""「自称与事实」闸的判据测试——**现状 0 命中，所以判别力必须自证**。

主仓里此刻没有一条"自称并发 + 依赖真锁 + 跑在 SQLite"的用例（`for_update` 在测试面 0 命中），
也就是说：**这条闸现在抓不到任何真阳性**。那它凭什么算闸？靠两件事：

1. **判别力自证**（合成负例必红）——本文件的主要部分；
2. **误报为 0 的实证**（对真仓跑一遍必须全过）——假红比没闸更坏。

判据本身在 `tools/claims.py`；这里既测判据，也测外壳。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

COORD = Path(__file__).resolve().parents[1]
TOOLS = COORD / "tools"

_SPEC = importlib.util.spec_from_file_location("claims", TOOLS / "claims.py")
assert _SPEC and _SPEC.loader
claims = importlib.util.module_from_spec(_SPEC)
sys.modules["claims"] = claims
_SPEC.loader.exec_module(claims)


def _repo(tmp_path: Path, body: str, *, name: str = "test_demo.py") -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / name).write_text(body, encoding="utf-8")
    return repo


def _scan(repo: Path, *, marker: str = "db", fixtures=("mysql_scratch", "db_session")):
    path = next((repo / "tests").rglob("test_*.py"))
    return claims.scan_file(
        path, str(path.relative_to(repo)), marker=marker, db_fixtures=fixtures
    )


# ---------------------------------------------------------------------------
# 判别力自证：负例必红
# ---------------------------------------------------------------------------


def test_lock_claim_on_sqlite_is_blocked(tmp_path: Path) -> None:
    """**合成负例必红**：自称并发、依赖 `FOR UPDATE`、却跑在非真库载体上。"""
    repo = _repo(
        tmp_path,
        '''"""并发占座。"""


def test_two_threads_race_for_same_row(session) -> None:
    """并发争用：两个线程抢同一行，靠 FOR UPDATE 串行化。"""
    row = session.query(User).with_for_update().first()
    assert row is not None
''',
    )
    got = _scan(repo)
    assert len(got) == 1
    assert got[0].test == "test_two_threads_race_for_same_row"
    assert "FOR UPDATE" in got[0].reason


def test_lock_claim_with_db_marker_passes(tmp_path: Path) -> None:
    """标了真库 ⇒ 放行（正例锚点）。"""
    repo = _repo(
        tmp_path,
        '''import pytest


@pytest.mark.db
def test_two_threads_race_for_same_row(session) -> None:
    """并发争用：FOR UPDATE 串行化。"""
    session.query(User).with_for_update().first()
''',
    )
    assert _scan(repo) == []


def test_lock_claim_with_db_fixture_passes(tmp_path: Path) -> None:
    """用了真库夹具 ⇒ 放行——**采集钩子会给它自动打标**，文件里没写 marker 不算证据。"""
    repo = _repo(
        tmp_path,
        '''def test_two_threads_race_for_same_row(mysql_scratch: str) -> None:
    """并发争用：FOR UPDATE 串行化。"""
    url = mysql_scratch
    assert "for_update" in "with_for_update"
''',
    )
    assert _scan(repo) == []


def test_module_level_pytestmark_counts(tmp_path: Path) -> None:
    """文件级 `pytestmark = pytest.mark.db` 也算真库载体。"""
    repo = _repo(
        tmp_path,
        '''import pytest

pytestmark = pytest.mark.db


def test_race_for_row(session) -> None:
    """并发 race，靠 FOR UPDATE。"""
    session.query(User).with_for_update().first()
''',
    )
    assert _scan(repo) == []


# ---------------------------------------------------------------------------
# 误报防线：判据自己的坑（都真踩过）
# ---------------------------------------------------------------------------


def test_block_words_are_not_concurrency_claims(tmp_path: Path) -> None:
    """**`blocks`／`blocked` 里的 `lock` 不算并发词**。

    第一版用子串匹配，实测把 `test_guard_is_what_blocks_future`、
    `test_deny_states_block_delivery` 这类**误报**了 5 条。假红比没闸更坏。
    """
    repo = _repo(
        tmp_path,
        '''def test_deny_states_blocks_delivery(session) -> None:
    """拒绝态阻断投递；FOR UPDATE 只是取数手段。"""
    session.query(Row).with_for_update().first()
''',
    )
    assert _scan(repo) == [], "block/blocks 不该被当成并发自称"


def test_non_lock_db_test_is_not_flagged(tmp_path: Path) -> None:
    """自称并发但**不依赖真锁**（如线程池）⇒ 不管：SQLite 与 MySQL 行为一致。"""
    repo = _repo(
        tmp_path,
        '''def test_concurrent_submits_are_ordered(session) -> None:
    """并发提交的有序性（纯内存队列，与载体无关）。"""
    assert 1 == 1
''',
    )
    assert _scan(repo) == []


def test_sqlite_ignores_for_update_signal_is_documented() -> None:
    """判据的**理由**要在源码里写清（"为什么这条存在"）——不然下一个人会把它删掉。"""
    src = (TOOLS / "claims.py").read_text(encoding="utf-8")
    assert "忽略 FOR UPDATE" in src or "忽略 `FOR UPDATE`" in src


# ---------------------------------------------------------------------------
# 外壳：真仓上跑一遍（误报为 0 的实证）
# ---------------------------------------------------------------------------


def test_real_repo_has_no_false_positive(capsys: pytest.CaptureFixture[str]) -> None:
    """对真目标仓跑：**不许误报**（现状该规则被遵守 ⇒ 0 命中）。

    这条不是"闸没抓到东西所以没用"，而是**误报防线**：判据一误报，
    整条闸就会被绕过——比没有闸更坏。
    """
    target = COORD.parent.parent / "futures-broker-gateway"
    if not (target / "tests").is_dir():
        pytest.skip("目标仓不在本机")
    rc = claims.main(["--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["test_files"] > 100, "测试面不该为空（防空报告假绿）"
    assert rc == 0, f"真仓出现误报：{payload['blocked']}"


def test_missing_target_tests_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = claims.main(["--target", str(tmp_path / "nope")])
    assert rc == 2
    assert "tests/" in capsys.readouterr().err
