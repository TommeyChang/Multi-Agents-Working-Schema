#!/usr/bin/env bash
# 监视器——**脚本定时 ＋ LLM 事件驱动**（方案 v2 §7）。
#
# 为什么必须是脚本：① 不占「单对话累计子代理 ≤ 10」额度；
# ② 不违「禁空转」（"等待"发生在脚本里，LLM 只在 delta 非空时被拉起）；
# ③ 无 sleep／轮询落在子代理里。
#
# 用法：
#   tools/watch.sh                # 一次扫描：有 delta 才输出（退出码 0=有事，10=无事）
#   tools/watch.sh --role tl      # 只看某角色相关
#   tools/watch.sh --force        # 无条件输出当前摘要
#
# cron 示例（每 60s）——把 <本体系绝对路径> 换成本体系目录实际所在位置：
#   * * * * * <本体系绝对路径>/coordinator/tools/watch.sh >> /var/log/bg-coord-watch.log 2>&1
#
# **只在退出码为 0（有 delta）时才唤醒会话**——这是它不违禁空转的全部理由。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 必须进包目录才 import 得到 `bg_coordinator`——少了这一句，
# 从任何别的工作目录跑本脚本都会 `No module named bg_coordinator`（监视器整个哑掉）。
cd "$(cd "$HERE/.." && pwd)" || exit 2
# 状态根**只认环境变量**：不设就让 CLI 用它自己的默认——
# 默认值只准有一处定义（`bg_coordinator/cli.py::_default_root`），这里再算一遍就是第二份口径。
ROOT_ARGS=()
[ -n "${BG_COORDINATOR_ROOT:-}" ] && ROOT_ARGS=(--root "$BG_COORDINATOR_ROOT")
# **解释器自持**：协调器只用标准库，`python3` 就够——不借目标仓的 venv。
# 借了就等于本体系依赖别人的环境：那个 venv 一没，监视器与冒烟一起哑。
PY="${BG_COORDINATOR_PYTHON:-}"
if [ -z "$PY" ]; then
  command -v python3 >/dev/null 2>&1 || { echo "找不到 python3（可用 BG_COORDINATOR_PYTHON 覆盖）" >&2; exit 2; }
  PY=python3
fi
STATE="${BG_COORDINATOR_STATE:-$HOME/.cache/bg-coordinator}"

ROLE=""
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# 兜底：受限环境（沙箱／只读 HOME）下退到临时目录——监视器不该因为写不了游标就罢工。
if ! mkdir -p "$STATE" 2>/dev/null; then
  STATE="$(mktemp -d)"
fi
CURSOR_FILE="$STATE/seq.cursor"
LAST_SEQ=$(cat "$CURSOR_FILE" 2>/dev/null || echo 0)

# 当前序号（health 是自检，不写盘）
NOW_SEQ=$("$PY" -m bg_coordinator.cli "${ROOT_ARGS[@]+"${ROOT_ARGS[@]}"}" health 2>/dev/null \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("seq",0))' 2>/dev/null || echo 0)

if [ "$FORCE" -eq 0 ] && [ "$NOW_SEQ" = "$LAST_SEQ" ]; then
  # 无 delta ⇒ 静默退出。**这不是空转，是脚本的正常退出。**
  exit 10
fi

echo "[watch] $(date -Is) seq ${LAST_SEQ} → ${NOW_SEQ}"
"$PY" -m bg_coordinator.cli "${ROOT_ARGS[@]+"${ROOT_ARGS[@]}"}" report
if [ -n "$ROLE" ]; then
  echo "--- 可认领（${ROLE}）---"
  "$PY" -m bg_coordinator.cli "${ROOT_ARGS[@]+"${ROOT_ARGS[@]}"}" ready --role-name "$ROLE" || true
fi

# 只有确认输出了，才推进游标
echo "$NOW_SEQ" > "$CURSOR_FILE"
exit 0
