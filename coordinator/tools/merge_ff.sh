#!/usr/bin/env bash
#
# merge_ff.sh — MAWS 合并封装：私有 worktree 组树 → 主检出快进
#
# 与协调器的分工：`bg_coordinator/merge.py` 只记录**状态**（入队／串行闸／合并结果），永不执行
# git；本脚本只做**git 机制**（组树、快进、竞态与活锁重试）。理由见 merge.py 开篇：串行化能治
# 索引竞态，治不了内容冲突——故 shell 只做机械动作，语义冲突一律中止交回人工。
#
# 用法：
#   merge_ff.sh --worktree <私有 worktree 路径> --branch <分支> \
#               [--main <主 checkout 路径>] [--message <合并消息>] \
#               [--dry-run] [--retries N] [--lock-wait <秒>]
#
#   --worktree   必填。私有 worktree；工作区须干净，脏则拒绝（禁自动 stash）。
#   --branch     必填。被合并分支；不存在即前置失败。
#   --main       选填。主 checkout；默认取 `git worktree list` 首个（主工作树）。
#   --message    选填。合并提交消息；默认 `merge: <branch> → main`。
#   --dry-run    只打印将执行的命令序列，**零写操作**。
#   --retries    快进竞态重试上限（默认 8）；每次重试都从【当前 main】重建组树。
#   --lock-wait  `.git/index.lock` 活锁等待上限（秒，默认 30，可小数）；超限才失败。
#
# 保留（承重件）：私有 worktree 对【当前 main】`--no-ff` 组树 ＋ 主检出只 `--ff-only` 快进；
#   脏 worktree 拒绝；竞态重试且每次**重新组树**；`.git/index.lock` 活锁有界等待（并发 git 不判
#   致命）；`--dry-run` 零写；语义化退出码 ＋ stderr 诊断。
#
# 删除（原件与主仓绑定的部分，及理由）：
#   · 合并消息模板与任务编号措辞：主仓 T-*／R-* 编号体制；MAWS 条目号归协调器，复述只会造出
#     第二份真相源；
#   · WORKTREE.md 条文引用：MAWS 唯一权威是 rules/WORKSPACE.md，脚本只引用不复述；
#   · `tq_*` 等领域件：与合并机制无关的主仓专有物；
#   · 收尾复原入口 HEAD 与退出码 9：复原非合并机制本身（worktree 回收归合入者），精简为不做；
#   · 原件 C15 空合并复核：内容复核是合入者的独立步骤（WORKSPACE.md §五 检查清单第 3 项），
#     不是 shell 机制职责——本脚本不复述其判据；
#   · 假定「主 checkout 不是 worktree／布局固定」的分支：主 checkout 一律由 `git worktree list`
#     定位，脚本不假设任何目录布局。
#
# 退出码：0 成功（含 --dry-run）；2 用法／参数错误；3 前置失败（worktree 脏／分支不存在／主
#   checkout 不存在／非 git 仓）；4 `.git/index.lock` 等待超时；1 其它 git 失败（含冲突中止、
#   竞态重试耗尽）。刻意不用 `set -e`：组树与快进需**读退出码后分流**（冲突／竞态／活锁各走
#   各路），`-e` 会在分流前终止，且 `$(...)`／`((...))` 的失败语义易被误判——改为逐步显式判码。
set -uo pipefail
LC_ALL=C

# 用法即头部注释（第 3 行到 `set` 行）——单源：改头即改 --help，不会有两份措辞。
usage() { sed -n '3,/^set -uo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

log() { printf '[merge_ff] %s\n' "$*" >&2; }
# 语义化退出：参数错 2、前置失败 3、锁超时 4、其它 git 失败 1。
die() { local code="$1"; shift; printf '[merge_ff] 错误：%s\n' "$*" >&2; exit "$code"; }

# 毫秒时钟（`date +%s%N` 前 13 位 = Unix 毫秒）。
_now_ms() { date +%s%N | cut -c1-13; }

# 有界等待仓的 `.git/index.lock` 释放，超时返回 1。**活锁非致命**：并发 git 持锁则等，只有超限
# 才失败。linked worktree 的 index 在 `worktrees/<name>/` 下，故锁路径由 git 自报、不猜布局。
wait_for_index_lock() {
  local repo="$1" wait_s="$2" label="${3:-$1}" lock ms deadline
  ms="$(awk -v s="$wait_s" 'BEGIN{printf "%d", (s < 0 ? 0 : s) * 1000}')"
  lock="$(git -C "$repo" rev-parse --git-path index.lock 2>/dev/null || true)"
  [[ -n "$lock" ]] || lock=".git/index.lock"
  [[ "$lock" == /* ]] || lock="$repo/$lock"
  deadline=$(( $(_now_ms) + ms ))
  while [[ -e "$lock" ]]; do
    if (( $(_now_ms) >= deadline )); then
      printf '[merge_ff] 等 %s 的 .git/index.lock 释放超时（%ss）：%s——确认无并发 git 进程后删锁重试\n' \
        "$label" "$wait_s" "$lock" >&2
      return 1
    fi
    sleep 0.1
  done
}

main() {
  local WT="" BRANCH="" MAIN="" MESSAGE="" DRY_RUN=0 RETRIES=8
  LOCK_WAIT=30
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --worktree)  WT="${2:-}"; shift 2 ;;
      --branch)    BRANCH="${2:-}"; shift 2 ;;
      --main)      MAIN="${2:-}"; shift 2 ;;
      --message)   MESSAGE="${2:-}"; shift 2 ;;
      --dry-run)   DRY_RUN=1; shift ;;
      --retries)   RETRIES="${2:-}"; shift 2 ;;
      --lock-wait) LOCK_WAIT="${2:-}"; shift 2 ;;
      -h|--help)   usage; return 0 ;;
      *) printf '[merge_ff] 未知参数：%s\n' "$1" >&2; usage >&2; return 2 ;;
    esac
  done

  if [[ -z "$WT" || -z "$BRANCH" ]]; then
    printf '[merge_ff] --worktree 与 --branch 为必填\n' >&2
    usage >&2
    return 2
  fi
  [[ "$RETRIES" =~ ^[0-9]+$ ]] && (( RETRIES >= 1 )) || die 2 "--retries 须为正整数：$RETRIES"
  [[ "$LOCK_WAIT" =~ ^[0-9]+([.][0-9]+)?$ ]] || die 2 "--lock-wait 须为非负秒数：$LOCK_WAIT"
  [[ -d "$WT" ]] || die 3 "私有 worktree 不存在：$WT"
  WT="$(cd "$WT" && pwd -P)"
  git -C "$WT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 3 "非 git 工作区：$WT"

  # 主 checkout 一律由 `git worktree list` 首个（主工作树）定位——不假设任何目录布局。
  if [[ -z "$MAIN" ]]; then
    MAIN="$(git -C "$WT" worktree list --porcelain | awk '/^worktree /{print substr($0,10); exit}')"
  fi
  [[ -n "$MAIN" && -d "$MAIN" ]] || die 3 "主 checkout 不存在：$MAIN"
  MAIN="$(cd "$MAIN" && pwd -P)"
  git -C "$MAIN" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 3 "主 checkout 非 git 工作区：$MAIN"

  [[ -n "$MESSAGE" ]] || MESSAGE="merge: $BRANCH → main"
  git -C "$WT" rev-parse --verify --quiet "${BRANCH}^{commit}" >/dev/null \
    || die 3 "被合并分支不存在：$BRANCH"

  local wt_status
  wt_status="$(git -C "$WT" status --porcelain)" || die 1 "读取 worktree 状态失败：$WT"
  if [[ -n "$wt_status" ]]; then
    printf '[merge_ff] 私有 worktree 不干净，拒绝组树（禁自动 stash）：\n%s\n' "$wt_status" >&2
    return 3
  fi

  # dry-run 在任何写操作之前返回：零写是硬承诺，不是「尽量不写」。
  if (( DRY_RUN )); then
    cat <<EOF
[merge_ff --dry-run] 零写操作；将执行如下命令序列（<M> = 组好的合并提交）：
  前置校验：git -C $WT status --porcelain            # 期望空（私有 worktree 干净）
  锁等待：  等 $WT／$MAIN 的 .git/index.lock 释放（轮询 0.1s，上限 ${LOCK_WAIT}s，超限退出 4）
  组树①：   git -C $WT checkout --detach $(git -C "$MAIN" rev-parse HEAD)   # 对齐【当前 main】
  组树②：   git -C $WT merge --no-ff $BRANCH -m "$MESSAGE"
  快进：    git -C $MAIN merge --ff-only <M>         # 非快进即从当前 main 重建组树（≤ $RETRIES 次）
EOF
    return 0
  fi

  local attempt=0 main_sha merge_sha merge_out merge_rc ff_out ff_rc mh
  while (( attempt < RETRIES )); do
    attempt=$(( attempt + 1 ))
    main_sha="$(git -C "$MAIN" rev-parse HEAD)" || die 1 "读取 main HEAD 失败：$MAIN"

    # 每轮都从**当前 main** 重建组树——竞态后复用旧组树等于把陈旧基座合进去。
    wait_for_index_lock "$WT" "$LOCK_WAIT" "私有 worktree" || return 4
    log "组树①：$WT 检出当前 main $main_sha（detached）"
    git -C "$WT" checkout --detach --quiet "$main_sha" || die 1 "checkout --detach 失败：$WT"

    wait_for_index_lock "$WT" "$LOCK_WAIT" "私有 worktree" || return 4
    log "组树②：--no-ff 合并 $BRANCH（第 $attempt 次 / 上限 $RETRIES）"
    merge_out="$(git -C "$WT" merge --no-ff "$BRANCH" -m "$MESSAGE" 2>&1)"
    merge_rc=$?
    [[ -n "$merge_out" ]] && printf '%s\n' "$merge_out"

    if (( merge_rc != 0 )); then
      mh="$(git -C "$WT" rev-parse --absolute-git-dir)/MERGE_HEAD"
      if [[ -f "$mh" ]]; then
        printf '[merge_ff] 冲突即中止（禁自动解冲突）；冲突文件清单：\n%s\n' \
          "$(git -C "$WT" diff --name-only --diff-filter=U | sort -u)" >&2
        wait_for_index_lock "$WT" "$LOCK_WAIT" "私有 worktree" || return 4
        git -C "$WT" merge --abort || die 1 "merge --abort 失败，半合并态需人工处置"
        return 1
      fi
      case "$merge_out" in
        *index.lock*)
          (( attempt < RETRIES )) || die 1 "组树撞 .git/index.lock 重试耗尽（--retries $RETRIES）"
          log "组树撞 .git/index.lock（活锁）——等释放后重试"
          wait_for_index_lock "$WT" "$LOCK_WAIT" "私有 worktree" || return 4
          continue ;;
      esac
      die 1 "合并失败（非冲突）：$merge_out"
    fi

    merge_sha="$(git -C "$WT" rev-parse HEAD)"

    wait_for_index_lock "$MAIN" "$LOCK_WAIT" "主 checkout" || return 4
    log "快进：git -C $MAIN merge --ff-only $merge_sha"
    ff_out="$(git -C "$MAIN" merge --ff-only "$merge_sha" 2>&1)"
    ff_rc=$?
    [[ -n "$ff_out" ]] && printf '%s\n' "$ff_out"
    if (( ff_rc == 0 )); then log "成功：main 已快进到 $merge_sha"; return 0; fi
    case "$ff_out" in
      *"Not possible to fast-forward"*|*"not possible to fast-forward"*)
        (( attempt < RETRIES )) || die 1 "快进竞态重试耗尽（--retries $RETRIES）：main 持续推进"
        log "main 已前进（竞态）——从当前 main 重建组树重试（第 $((attempt + 1)) 次）"
        continue ;;
      *index.lock*)
        (( attempt < RETRIES )) || die 1 "快进撞 .git/index.lock 重试耗尽（--retries $RETRIES）"
        log "快进撞 .git/index.lock（活锁）——等释放后重试"
        wait_for_index_lock "$MAIN" "$LOCK_WAIT" "主 checkout" || return 4
        continue ;;
      *) die 1 "快进失败（非竞态）：$ff_out" ;;
    esac
  done
  die 1 "重试耗尽（--retries $RETRIES）"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
  exit $?
fi
