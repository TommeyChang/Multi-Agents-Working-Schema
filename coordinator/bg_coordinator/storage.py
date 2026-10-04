"""存储层——锁、原子写、事件追加、状态投影、发布。

**这是唯一碰磁盘的模块。** 引擎保持纯函数，因此状态机与校验规则可以脱离环境测试。

三条硬要求（方案 v2 §3.7／§4.6）：
1. **全程互斥**：`flock` 覆盖 read-modify-write 的**整段**——git 只原子了"写"，
   读与写之间那一段是敞开的，那正是 82 条重试提交的根因；
2. **原子写**：temp → fsync → rename → fsync(dir)，防半截文件；
3. **events 只追加，永不改写**：就地编辑 = 状态机被静默污染且不可重建。
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Params, Result, State, apply
from .errors import Code, Rejection
from .models import Actor, Event
from .readiness import Quota

SCHEMA_VERSION = 1


def _confirm_seq_of(cid: str) -> int:
    """`C-<n>` → n（重放时把确认流水号推回原值，号不许因重建而重复）。"""
    head, _, tail = cid.partition("-")
    return int(tail) if head == "C" and tail.isdigit() else 0


class StoreError(RuntimeError):
    pass


@dataclass
class Store:
    """协调器的持久化门面。

    `root` 是**协调器状态根**（默认工作区根下 `coordinator/`，**在仓库之外**）。
    `repo` 是可选的仓库路径——只有在发布投影时才需要（方案 v2 §4.2）。
    """

    root: Path
    repo: Path | None = None
    lock_timeout: float = 10.0
    batch_window: float = 2.0

    # --- 路径 ---
    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def quota_path(self) -> Path:
        return self.root / "quota.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    @property
    def snapshot_dir(self) -> Path:
        return self.root / "snapshots"

    # --- 初始化 ---
    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            _atomic_write_text(self.events_path, "")
        if not self.state_path.exists():
            _atomic_write_text(self.state_path, json.dumps(State().to_dict(), ensure_ascii=False))
        if not self.quota_path.exists():
            _atomic_write_text(
                self.quota_path,
                json.dumps(State().quota.to_dict(), ensure_ascii=False, indent=2),
            )

    # --- 锁 ---
    @contextmanager
    def lock(self) -> Iterator[None]:
        """`flock` 覆盖整段 read-modify-write。"""
        self.root.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")  # noqa: SIM115 - 句柄生命周期即锁生命周期
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise StoreError(
                            f"获取协调器锁超时（{self.lock_timeout}s）——另一端可能卡住"
                        ) from None
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    # --- 事件 ---
    def read_events(self) -> list[Event]:
        if not self.events_path.exists():
            return []
        out: list[Event] = []
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(Event(**json.loads(line)))
        return out

    def seen_request_ids(self) -> set[str]:
        return {e.request_id for e in self.read_events() if e.request_id}

    def append_event(self, ev: Event) -> None:
        """**只追加**。任何改写已有行的行为都是事故。"""
        line = json.dumps(ev.to_dict(), ensure_ascii=False) + "\n"
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    # --- 状态投影：**缓存只由日志派生** ---
    #
    # 权威是 events.jsonl；state.json 是**派生缓存**。
    # 缓存带 `last_seq`，读时与日志末尾比对；不符即重放。
    # 这条保证「权威」不是一个口号——**缓存永远追不上日志时，就说明它不可信**。

    def _log_tail_seq(self) -> int:
        """日志末尾的事件序号；空日志为 0。"""
        events = self.read_events()
        return max((e.seq for e in events), default=0)

    def _duplicate_seqs(self) -> list[int]:
        """日志里重复出现的序号。

        **重复序号 = 两个写者交错追加**——此时日志不再是单条时间线，
        重放结果取决于顺序，权威性丧失。这种情况必须拒服务，不能猜。
        """
        from collections import Counter

        counts = Counter(e.seq for e in self.read_events())
        return sorted(seq for seq, n in counts.items() if n > 1)

    def load_state(self, strict: bool = True) -> State:
        """读缓存，**校验它没有落后于日志**。

        `strict=False` 时跳过校验（仅重建路径内部使用）。
        """
        if not self.state_path.exists():
            return self.replay()
        try:
            state = State.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise StoreError(f"state.json 损坏：{exc}") from exc

        if strict:
            dupes = self._duplicate_seqs()
            if dupes:
                raise StoreError(
                    f"事件日志出现重复序号 {dupes}——两个写者交错追加，"
                    f"无法确定权威。请人工核查 events.jsonl，勿直接重放。"
                )
            tail = self._log_tail_seq()
            if state.last_seq != tail:
                # 缓存落后或超前 ⇒ 不可信，**以日志为准重建**（这是恢复路径，不是错误）
                state = self.replay()
        self._apply_quota_file(state)
        return state

    def _apply_quota_file(self, state: State) -> None:
        """把磁盘上的配额策略套到状态上。

        **配额是策略，不是状态**——它由人改（`quota.json`），
        若写进状态缓存就会被事件重放冲掉。此前 `quota.json` 建了却没人读，
        于是"配额生效"是假的：实测设成 1 之后第二个申请照样通过。
        """
        if not self.quota_path.exists():
            return
        try:
            payload = json.loads(self.quota_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise StoreError(f"quota.json 损坏：{exc}") from exc
        state.quota = Quota.from_dict(payload)

    def save_quota(self, quota: Quota) -> None:
        """把配额策略落盘。**配额是策略、不是状态**——它不进事件流。"""
        _atomic_write_text(
            self.quota_path, json.dumps(quota.to_dict(), ensure_ascii=False, indent=2)
        )

    def set_quota(self, **changes: object) -> Quota:
        """改配额策略并落盘（唯一入口）。"""
        with self.lock():
            current = Quota()
            if self.quota_path.exists():
                current = Quota.from_dict(
                    json.loads(self.quota_path.read_text(encoding="utf-8"))
                )
            payload = current.to_dict()
            payload.update(changes)
            new = Quota.from_dict(payload)
            _atomic_write_text(
                self.quota_path, json.dumps(new.to_dict(), ensure_ascii=False, indent=2)
            )
            return new

    def save_state(self, state: State) -> None:
        state.last_seq = state.seq  # 与日志末尾对齐的锚
        _atomic_write_text(
            self.state_path, json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        )

    def replay(self) -> State:
        """从 events.jsonl 重放重建状态——**唯一的状态来源**。

        事件里带了**完整条目快照**，所以这里是逐字重建，不是近似恢复。
        """
        from .models import Confirmation, Task

        state = State()
        for ev in self.read_events():
            state.seq = max(state.seq, ev.seq)
            if ev.snapshot:
                state.tasks[ev.id] = Task.from_dict(ev.snapshot)
            # **用户确认也进事件面**：它是"用户要什么"的证据，不能像租约那样
            # "重建时从旧缓存里捞一把"——那样证据会悄悄失真（配额踩过同一个坑）。
            payload = (ev.detail or {}).get("confirmation")
            if ev.verb == "confirm" and ev.result == "ok" and isinstance(payload, dict):
                conf = Confirmation.from_dict(payload)
                state.confirmations[conf.id] = conf
                state.confirm_seq = max(state.confirm_seq, _confirm_seq_of(conf.id))
            used = (ev.detail or {}).get("confirmation_used")
            if isinstance(used, str) and used in state.confirmations:
                state.confirmations[used].used_by = ev.id
        state.last_seq = state.seq
        self._apply_quota_file(state)
        return state

    def verify_cache(self, state: State) -> list[str]:
        """校验缓存与重放结果一致。返回差异清单（空 = 一致）。

        `health` 与恢复演练共用这条判据——**没有校验过的缓存不算权威的投影**。
        """
        rebuilt = self.replay()
        diffs: list[str] = []
        if rebuilt.seq != state.seq:
            diffs.append(f"seq：缓存 {state.seq} ≠ 重放 {rebuilt.seq}")
        a, b = set(state.tasks), set(rebuilt.tasks)
        if a - b:
            diffs.append(f"缓存多出条目：{sorted(a - b)}")
        if b - a:
            diffs.append(f"缓存缺少条目：{sorted(b - a)}")
        for tid in sorted(a & b):
            if state.tasks[tid].to_dict() != rebuilt.tasks[tid].to_dict():
                diffs.append(f"{tid} 字段不一致")
        ca, cb = set(state.confirmations), set(rebuilt.confirmations)
        if ca - cb:
            diffs.append(f"缓存多出用户确认：{sorted(ca - cb)}")
        if cb - ca:
            diffs.append(f"缓存缺少用户确认：{sorted(cb - ca)}")
        for cid in sorted(ca & cb):
            if state.confirmations[cid].to_dict() != rebuilt.confirmations[cid].to_dict():
                diffs.append(f"用户确认 {cid} 字段不一致")
        return diffs

    def rebuild_and_save(self) -> State:
        """显式重建并落盘——恢复演练与 health 自检共用。

        **租约是运行期状态，事件面未覆盖**：重建时保留现存租约，只重建条目。

        **用户确认不在此列**：它进事件面（见 `replay`），重建即还原——
        从旧缓存里捞会把"已消费"悄悄还原成"在手"，那是证据失真。
        """
        state = self.replay()
        try:
            current = self.load_state(strict=False)
            state.leases = current.leases
            state.quota = current.quota
            state.merges = current.merges
        except StoreError:
            pass
        self.save_state(state)
        return state

    # --- 事务：锁内 read-modify-write ---
    def transact(
        self,
        verb: str,
        task_id: str,
        actor: Actor,
        params: Params | None = None,
        expect_ver: int | None = None,
        request_id: str = "",
    ) -> Result:
        """**唯一写入口**。锁 → 读 → 算 → 校验幂等 → 追加事件 → 存状态 → 释放。"""
        with self.lock():
            state = self.load_state()

            # 幂等：同 request_id 已处理 ⇒ 直接返回成功（不产生重复事件）
            if request_id and request_id in self.seen_request_ids():
                evs = [e for e in self.read_events() if e.request_id == request_id]
                return Result(
                    ok=True,
                    state=state,
                    event=evs[-1] if evs else None,
                    detail={"idempotent_replay": True},
                )

            result = apply(
                state,
                verb,
                task_id,
                actor,
                params=params,
                expect_ver=expect_ver,
                request_id=request_id,
            )
            if result.event is not None:
                self.append_event(result.event)
            self.save_state(result.state)
            return result

    def transact_register(
        self, actor: Actor, params: Params, request_id: str = ""
    ) -> Result:
        """登记——**编号由协调器在事务内分配**。

        与 `transact` 的区别只在「号从哪来」：这里不发号给调用方，
        调用方拿到的是协调器分配的编号。
        """
        return self.transact("register", "", actor, params=params, request_id=request_id)

    # --- 发布：把状态渲染进仓库（视图 A ＋ 视图 B）---
    def publish(
        self,
        render: Callable[[State], dict[Path, str]],
        commit: bool = True,
        message: str = "chore(coordinator): publish task views",
    ) -> list[Path]:
        """渲染视图 → 写盘 → **显式路径提交**（禁 `-A`）。

        **核心不变量：只有内容真的变了才写、才提交。**

        这条不是优化，是**正确性要求**——方案里那类"为写状态板而提交"的多余提交
        （实测近 40 条提交 **40/40** 触及 `todo/`；`pm/*` 合并 **19 : 2** 于 `dev/*`）
        正是它们把每次改动都变成一次全仓合并面。

        哪怕渲染里出现一个会漂移的字段（时间戳、日期列），这条闸也能挡住：
        逐字节比对，相同即**整份跳过**，既不写盘也不提交，因此不产生冲突面。
        """
        if self.repo is None:
            raise StoreError("未配置 repo，无法发布视图")
        with self.lock():
            state = self.load_state()
            files = render(state)

        written: list[Path] = []
        rel_paths: list[str] = []
        for path, text in files.items():
            target = self.repo / path if not path.is_absolute() else path
            if target.exists() and target.read_text(encoding="utf-8") == text:
                continue  # ← 内容未变：不写盘、不提交
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, text)
            written.append(target)
            rel_paths.append(str(path))

        if commit and rel_paths:
            self._git_commit(rel_paths, message)
        self._snapshot_events()
        return written

    def _snapshot_events(self) -> Path | None:
        """把 events 快照进仓库一次——**甲案的必要组成，非可选优化**。

        events 在仓库之外 ⇒ 一次磁盘故障就丢了全部任务史（方案 v2 §4.7）。
        """
        if self.repo is None or not self.events_path.exists():
            return None
        state = self.load_state()
        dest = self.repo / "todo" / ".coordinator-snapshot" / f"events-{state.seq}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(dest, self.events_path.read_text(encoding="utf-8"))
        self._prune_snapshots(dest.parent, keep=10)
        return dest

    @staticmethod
    def _prune_snapshots(directory: Path, keep: int) -> None:
        snaps = sorted(directory.glob("events-*.jsonl"), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-keep] if len(snaps) > keep else []:
            old.unlink(missing_ok=True)

    def _git_commit(self, rel_paths: list[str], message: str) -> str:
        """**显式路径**提交——严禁 `git add -A`（公约 2）。"""
        assert self.repo is not None
        subprocess.run(  # noqa: S603 - 固定参数，无 shell
            ["git", "add", "--", *rel_paths],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        done = subprocess.run(  # noqa: S603
            ["git", "commit", "-m", message, "--", *rel_paths],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            # 无改动可提交不算失败
            if "nothing to commit" in (done.stdout + done.stderr):
                return ""
            raise StoreError(f"git commit 失败：{done.stderr.strip()}")
        return done.stdout.strip()

    def health(self) -> dict[str, Any]:
        """自检：事件序号连续、**缓存与重放一致**、无重复序号。

        **自检本身不得抛错**——它存在的意义就是把问题报出来。
        故这里**不做严格校验**，改为把不一致算成差异清单。
        """
        state = self.load_state(strict=False)
        events = self.read_events()
        dupes = self._duplicate_seqs()
        cache_diffs = [] if dupes else self.verify_cache(state)
        seqs = sorted(e.seq for e in events)
        gaps: list[tuple[int, int]] = []
        if seqs and seqs[0] != 1:
            gaps.append((0, seqs[0]))  # 起始就必须是 1，否则前面丢了事件
        gaps.extend(
            (a, b) for a, b in zip(seqs, seqs[1:], strict=False) if b - a != 1
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "tasks": len(state.tasks),
            "quota_enabled": state.quota.enabled,
            "waiters": len(state.waiters),
            "leases": len(state.leases),
            "merges": len(state.merges),
            "events": len(events),
            "seq": state.seq,
            "last_seq": state.last_seq,
            "log_tail_seq": max((e.seq for e in events), default=0),
            "seq_gaps": gaps,
            "seq_duplicates": dupes,
            "cache_diffs": cache_diffs,
            "usable": not gaps and not cache_diffs and not dupes,
        }


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """temp → fsync → rename → fsync(dir)。防半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def rejection_of(result: Result) -> Rejection:
    return result.rejection or Rejection(Code.OK, "ok")
