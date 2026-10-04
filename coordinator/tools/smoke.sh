#!/usr/bin/env bash
# 冒烟：把协调器的阶段一/二能力在**隔离状态根**里跑一遍（绝不碰真实状态）。
#
# 用法：tools/smoke.sh
# 退出码：0 = 全绿；非 0 = 有断言失败。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(cd "$HERE/.." && pwd)"
# 解释器自持：协调器只用标准库 → `python3` 即可（见 watch.sh 同一理由）
PY="${BG_COORDINATOR_PYTHON:-}"
if [ -z "$PY" ]; then
  command -v python3 >/dev/null 2>&1 || { echo "找不到 python3（可用 BG_COORDINATOR_PYTHON 覆盖）" >&2; exit 2; }
  PY=python3
fi

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

ROOT="$SANDBOX/coordinator"
REPO="$SANDBOX/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email smoke@local
git -C "$REPO" config user.name smoke

coord() { "$PY" -m bg_coordinator.cli --root "$ROOT" --repo "$REPO" "$@"; }

pass=0
fail=0
check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  [PASS] $desc"; pass=$((pass + 1))
  else
    echo "  [FAIL] $desc"; fail=$((fail + 1))
  fi
}
check_fail() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  [FAIL] $desc（应被拒却通过了）"; fail=$((fail + 1))
  else
    echo "  [PASS] $desc"; pass=$((pass + 1))
  fi
}

echo "[smoke] 状态根=$ROOT 仓库=$REPO"
cd "$PKG_ROOT" || exit 2

echo "① 初始化与自检"
check "init" coord init
check "health" coord health

echo "② 端到端七步"
EV="$SANDBOX/gate.log"; echo "2182 passed" > "$EV"
# **编号由协调器分配**——脚本只声明线别与类型，然后从返回值里取号
coord init >/dev/null 2>&1
TID=$(coord --json register --role po:po --title 冒烟 --line D --priority P1 --kind T \
      | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["detail"]["id"])')
if [ -n "$TID" ]; then
  echo "  [PASS] register（协调器分配编号：$TID）"; pass=$((pass + 1))
else
  echo "  [FAIL] register 未返回编号"; fail=$((fail + 1))
fi
check "claim-analyze"  coord claim-analyze --id "$TID" --role pm:pm-D:D
check "define"         coord define --id "$TID" --role pm:pm-D:D \
                         --whitelist 'data_access/**' --frozen main.py \
                         --acceptance 'test:uv run pytest -q' \
                         --acceptance 'negative:未注入仍 200 ⇒ 必红'
check "ready"          coord ready --role-name tech-lead
# **默认即闸**：没批预算 ⇒ 派不了活（这条把文档那句变成可执行判据）
check_fail "未批预算 ⇒ 派不了活" coord claim-dev --id "$TID" --role tech-lead:TL-D:D
check "批预算（D 线 ≤2）" coord quota --line D --budget 2
check "claim-dev"      coord claim-dev --id "$TID" --role tech-lead:TL-D:D
# **子代理授权**：开子代理前先领凭证（按条目一张，可数、有界、会过期）
check "dispatch 领授权"   coord dispatch --role tech-lead:TL-D:D --task "$TID"
check "grants 可查"       bash -c "'$PY' -m bg_coordinator.cli --root '$ROOT' --json grants | grep -q '$TID'"
check_fail "同条目再领 ⇒ 拒" coord dispatch --role tech-lead:TL-D:D --task "$TID"
check_fail "dev 越权领 ⇒ 拒" coord dispatch --role dev:d1:D --task "$TID"
check "start"          coord start --id "$TID" --role tech-lead:TL-D:D
check "deliver"        coord deliver --id "$TID" --role tech-lead:TL-D:D \
                         --commit abc1234 --gate-cmd 'uv run pytest -q' --gate-exit 0 \
                         --evidence "$EV" --changed data_access/kline/follow.py
check "verify"         coord verify --id "$TID" --role tech-lead:TL-D:D
check "accept"         coord accept --id "$TID" --role pm:pm-D:D

echo "③ 拒绝路径（每条都必须被拒）"
check "登记发号不重号"    coord --json register --role po:po --title x --line D --priority P1
check "编号簿记独立"      coord number
# 白名单是 `define` 的要素，不是 `register` 的——登记阶段允许只有标题。
TID9=$(coord --json register --role po:po --title x --line D --priority P1 --kind T \
       | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["detail"]["id"])')
check "第二条独立编号"    test "$TID9" != "$TID"
check "claim-analyze"     coord claim-analyze --id "$TID9" --role pm:pm-D:D
check_fail "define 缺白名单 ⇒ E_INCOMPLETE" \
                          coord define --id "$TID9" --role pm:pm-D:D --acceptance 'test:pytest'
check_fail "define 缺验收 ⇒ E_INCOMPLETE" \
                          coord define --id "$TID9" --role pm:pm-D:D --whitelist 'a/**'
check_fail "越权：dev 冒充 TL"          coord claim-dev --id "$TID9" --role dev:d1:D
check_fail "终态不可再改 ⇒ E_FROZEN"    coord deliver --id "$TID" --role tech-lead:TL-D:D \
                                          --commit x --gate-cmd y --gate-exit 0 --evidence "$EV"
check_fail "陈旧 expect-ver ⇒ E_CONFLICT" coord override --id "$TID" --role commander:c \
                                          --reason t --expect-ver 1 --state defined

echo "④ 溯源 / 审核 / 报告"
check "trace"  coord trace "$TID"
check "why"    coord why data_access/kline/follow.py
check "audit"  coord audit --summary
check "report" coord report

echo "⑤ 资源租约"
check "acquire"      coord acquire --holder w1 --db-name bg_w1 --pid $$ --ttl 60
check "leases"       coord leases
check_fail "死租约回收（未被拒即错）" coord reclaim --lease-id L-nonexistent

echo "⑥ 视图发布（兼容视图 ＋ 报告 ＋ events 快照）"
check "publish" coord publish
check "线板已生成"      test -f "$REPO/todo/lines/infra.md"
check "报告已生成"      test -f "$REPO/reports/调度报告.md"
check "events 已快照"   bash -c "ls '$REPO'/todo/.coordinator-snapshot/events-*.jsonl >/dev/null 2>&1"
check "表头逐字兼容"    grep -q '^| 条目号 | 内容 | 修改范围白名单 | 不动清单 | 前置 | 验收标准 | 状态/认领人 |$' "$REPO/todo/lines/infra.md"
check "状态格首字符是 token" grep -qE "^\\| ${TID} \\|.*\\| ✅ TL-D \\|$" "$REPO/todo/lines/infra.md"

echo "⑦ 改造收进来的三件工具（界内闸／触库单元／合并封装）"
# 界内闸：读协调器里的白名单与不动清单（条目 $TID 的白名单是 data_access/**、不动清单是 main.py）
mkdir -p "$REPO/data_access"; echo x > "$REPO/data_access/inside.py"
git -C "$REPO" add data_access/inside.py
check "commit_gate 白名单内放行"       "$PY" "$PKG_ROOT/tools/commit_gate.py" --root "$ROOT" --repo "$REPO" --task "$TID"
echo x > "$REPO/outside.py"; git -C "$REPO" add outside.py
check_fail "commit_gate 白名单外 ⇒ BLOCK" "$PY" "$PKG_ROOT/tools/commit_gate.py" --root "$ROOT" --repo "$REPO" --task "$TID"
git -C "$REPO" reset -q
echo x > "$REPO/main.py"; git -C "$REPO" add main.py
check_fail "commit_gate 命中不动清单 ⇒ BLOCK" "$PY" "$PKG_ROOT/tools/commit_gate.py" --root "$ROOT" --repo "$REPO" --task "$TID"
git -C "$REPO" reset -q; rm -f "$REPO/main.py" "$REPO/outside.py"

# 触库单元：--check 返回 0（可用）或 2（不可用）皆合法——没装 MySQL 的机器不该因此判红
check "db_ladder --check 可跑（0/2 皆合法）" bash -c \
  "\"$PY\" \"$PKG_ROOT/tools/db_ladder.py\" --check >/dev/null 2>&1; rc=\$?; [ \$rc -eq 0 ] || [ \$rc -eq 2 ]"
check_fail "db_ladder 不给动作 ⇒ 用法错" "$PY" "$PKG_ROOT/tools/db_ladder.py"

# 合并封装：在沙箱仓里真组树 ＋ 真快进（绝不碰真实仓）
git -C "$REPO" add -A >/dev/null 2>&1; git -C "$REPO" commit -qm smoke-base >/dev/null 2>&1
git -C "$REPO" worktree add -q "$SANDBOX/wt" -b smoke-merge
echo merged > "$SANDBOX/wt/merged.txt"
git -C "$SANDBOX/wt" add merged.txt; git -C "$SANDBOX/wt" commit -qm smoke-feature
check "merge_ff 组树＋快进"         "$PKG_ROOT/tools/merge_ff.sh" --worktree "$SANDBOX/wt" --branch smoke-merge --main "$REPO"
check "合并结果已在 main"           test -f "$REPO/merged.txt"
echo dirty >> "$SANDBOX/wt/merged.txt"
check_fail "merge_ff 脏 worktree ⇒ 拒" "$PKG_ROOT/tools/merge_ff.sh" --worktree "$SANDBOX/wt" --branch smoke-merge --main "$REPO"

echo "⑧ 门禁入口 / 迁移闸 / 监视器（本体系自持）"
# 在沙箱仓里造一个最小可跑面：ruff 配置 ＋ 一个域下的测试文件
mkdir -p "$REPO/tests/auth"
printf '[tool.ruff]\nline-length = 100\n' > "$REPO/pyproject.toml"
printf 'def test_ok():\n    assert 1 + 1 == 2\n' > "$REPO/tests/auth/test_demo.py"
# 口径：冒烟只断言**门禁能出裁决并落证据**（0/1 皆合法）。
# 为什么不断言"必绿"：沙箱仓的解释器是本体系的 `python3`，它未必装了 ruff／pytest——
# 那种情况下 gate **本来就该报红**（fail closed，不许静默变绿）。
# **绿／红两条路径**由 `tests/test_gate.py` 用带依赖的解释器覆盖。
check "gate 出裁决（受影响面）" bash -c \
  "'$PY' '$PKG_ROOT/tools/gate.py' --target '$REPO' --python '$PY' --changed auth/x.py \
     --log-dir '$SANDBOX/ev' --json > '$SANDBOX/gate.json' 2>/dev/null; rc=\$?; [ \$rc -eq 0 ] || [ \$rc -eq 1 ]"
check "gate 证据已落盘"       bash -c "ls '$SANDBOX'/ev/gate-affected-*.log >/dev/null 2>&1"
check "gate 裁决形状正确"     bash -c "grep -q '\"scope\": \"affected\"' '$SANDBOX/gate.json' && grep -q '\"legs\"' '$SANDBOX/gate.json'"
check "gate 出裁决（全量口径）" bash -c \
  "'$PY' '$PKG_ROOT/tools/gate.py' --target '$REPO' --python '$PY' --scope full \
     --log-dir '$SANDBOX/ev' >/dev/null 2>&1; rc=\$?; [ \$rc -eq 0 ] || [ \$rc -eq 1 ]"
check_fail "gate 范围不明 ⇒ 用法错（不静默全量）" "$PY" "$PKG_ROOT/tools/gate.py" \
                              --target "$REPO" --python "$PY" --scope affected

# 迁移闸：好图全过 / 撞号必拦
mkdir -p "$REPO/alembic/versions"
printf 'revision = "0001"\ndown_revision = None\n' > "$REPO/alembic/versions/0001_a.py"
check "migration_gate 好图全过"  "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO"
printf 'revision = "0001"\ndown_revision = None\n' > "$REPO/alembic/versions/0002_dup.py"
check_fail "migration_gate 撞号 ⇒ BLOCK" "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO"
rm -f "$REPO/alembic/versions/0002_dup.py"
printf 'revision = "0002"\ndown_revision = "0001"\n' > "$REPO/alembic/versions/0002_b.py"
printf 'revision = "0003"\ndown_revision = "0001"\n' > "$REPO/alembic/versions/0003_c.py"
check_fail "migration_gate **分叉** ⇒ BLOCK（两个 head）" "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO"
rm -f "$REPO/alembic/versions/0003_c.py"

# [6] 已落库件保真（git 对象事实）：注释级更正放行 / 真改动必拦 / 落后分支不许误报
printf '"""docstring 原文。"""\nrevision = "0001"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n' \
  > "$REPO/alembic/versions/0001_a.py"
git -C "$REPO" add -A >/dev/null 2>&1
git -C "$REPO" commit -qm smoke-mig-base >/dev/null 2>&1
git -C "$REPO" tag smoke-mig-base
printf '"""docstring 更正。"""\n# 新增注释（无语义改动）\nrevision = "0001"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n' \
  > "$REPO/alembic/versions/0001_a.py"
check "migration_gate **注释级更正** ⇒ 放行（允许的例外）" \
  "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO" --base smoke-mig-base
printf '"""docstring 更正。"""\nrevision = "0001"\ndown_revision = None\n\n\ndef upgrade():\n    op.add_column("t", "x")\n' \
  > "$REPO/alembic/versions/0001_a.py"
check_fail "migration_gate **已落库件被就地改写** ⇒ BLOCK" \
  "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO" --base smoke-mig-base
git -C "$REPO" checkout -q -- alembic/versions/0001_a.py
git -C "$REPO" branch -q smoke-behind
printf 'revision = "0003"\ndown_revision = "0002"\n' > "$REPO/alembic/versions/0003_d.py"
git -C "$REPO" add -A >/dev/null 2>&1
git -C "$REPO" commit -qm smoke-mig-ahead >/dev/null 2>&1
SMOKE_MAIN_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
git -C "$REPO" checkout -q smoke-behind
check "migration_gate **落后分支不误报**（落后 ≠ 改写）" \
  "$PY" "$PKG_ROOT/tools/migration_gate.py" --target "$REPO" --base "$SMOKE_MAIN_BRANCH"

# 串行族放号闸：迁移件同时只允许一个在飞占号（号顺序＝链位顺序）
check "reserve alembic 首个" bash -c "'$PY' -m bg_coordinator.cli --root '$ROOT' reserve --family alembic --holder dba-a --task T-D-1 >/dev/null 2>&1"
check_fail "reserve alembic 第二个 ⇒ E_NUMBER_INFLIGHT" "$PY" -m bg_coordinator.cli --root "$ROOT" reserve --family alembic --holder dba-b --task T-D-2

# 监视器：有 delta 唤醒 / 无 delta 静默 / 任意 cwd 可跑
check "watch 有 delta ⇒ 唤醒（0）" env BG_COORDINATOR_ROOT="$ROOT" BG_COORDINATOR_STATE="$SANDBOX/watch" \
                                   "$PKG_ROOT/tools/watch.sh"
check "watch 无 delta ⇒ 静默（10）" bash -c \
  "BG_COORDINATOR_ROOT='$ROOT' BG_COORDINATOR_STATE='$SANDBOX/watch' '$PKG_ROOT/tools/watch.sh' >/dev/null 2>&1; [ \$? -eq 10 ]"
check "watch 任意 cwd 可跑"        bash -c \
  "cd / && BG_COORDINATOR_ROOT='$ROOT' BG_COORDINATOR_STATE='$SANDBOX/watch' '$PKG_ROOT/tools/watch.sh' --force >/dev/null 2>&1"

echo "⑨ DBA／OPS 侧工具（默认不动手／不重启／只判）"
check "scratch_gc 默认 dry-run（0/1/2 皆合法）" bash -c \
  "'$PY' '$PKG_ROOT/tools/scratch_gc.py' --prefix bg_maws --json >/dev/null 2>&1; rc=\$?; [ \$rc -eq 0 ] || [ \$rc -eq 1 ] || [ \$rc -eq 2 ]"
check "scratch_gc 参数默认不是 apply" bash -c \
  "'$PY' -c \"import importlib.util,sys; s=importlib.util.spec_from_file_location('g','$PKG_ROOT/tools/scratch_gc.py'); m=importlib.util.module_from_spec(s); sys.modules['g']=m; s.loader.exec_module(m); a=m.build_parser().parse_args([]); assert a.apply is False and a.include_nopid is False\""
check_fail "scratch_gc 坏参数（--ttl-min 非数）" "$PY" "$PKG_ROOT/tools/scratch_gc.py" --ttl-min abc
check "probe 探活失败未达阈 ⇒ 0" bash -c \
  "'$PY' '$PKG_ROOT/tools/probe.py' --url http://127.0.0.1:1/health --timeout 1 --fail-threshold 3 --state-dir '$SANDBOX/probe' >/dev/null 2>&1"
check_fail "probe 达阈 ⇒ 判卡死(1)" bash -c \
  "'$PY' '$PKG_ROOT/tools/probe.py' --url http://127.0.0.1:1/health --timeout 1 --fail-threshold 1 --state-dir '$SANDBOX/probe' >/dev/null 2>&1"
check "preflight 出裁决（0/1 皆合法）" bash -c \
  "'$PY' '$PKG_ROOT/tools/preflight.py' --target '$REPO' --root '$ROOT' --load-ratio 1000 --min-free-mb 0 \
     --json > '$SANDBOX/preflight.json' 2>/dev/null; rc=\$?; [ \$rc -eq 0 ] || [ \$rc -eq 1 ]"
check "preflight 五条腿齐全" bash -c "grep -q '触库可用' '$SANDBOX/preflight.json' && grep -q '宿主余量' '$SANDBOX/preflight.json'"

echo "⑩ 把条款变成闸：自称与事实／条目级约束"
mkdir -p "$REPO/tests/demo"
printf 'import pytest\n\n\ndef test_race_for_row(session) -> None:\n    """并发争用：FOR UPDATE 串行化。"""\n    session.query(X).with_for_update().first()\n' > "$REPO/tests/demo/test_race.py"
check_fail "自称并发＋依赖真锁却在 SQLite ⇒ BLOCK" "$PY" "$PKG_ROOT/tools/claims.py" --target "$REPO"
printf 'import pytest\n\n\n@pytest.mark.db\ndef test_race_for_row(session) -> None:\n    """并发争用：FOR UPDATE 串行化。"""\n    session.query(X).with_for_update().first()\n' > "$REPO/tests/demo/test_race.py"
check "标了真库 ⇒ 放行" "$PY" "$PKG_ROOT/tools/claims.py" --target "$REPO"
check_fail "未知约束 ⇒ 定稿即拒（没有判据的约束等于没声明）" coord define --id "$TID9" --role pm:pm-D:D \
                           --whitelist 'a/**' --acceptance 'test:pytest' --constraint 别乱改

echo
echo "[smoke] 通过 $pass ／ 失败 $fail"
[ "$fail" -eq 0 ] || exit 1
echo "[smoke] 全绿。"
