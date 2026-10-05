---
name: merge-flow
description: 合入流程：入队→队列闸→worktree 组树→主检出只快进→合并后内容复核→门禁复跑。当要把分支合回主干、合并被拒、或合并后发现内容丢失时使用。
---

# 技能：合入

## 一、入队（状态面）
```bash
coord merge-request --id <条目> --branch <分支> --commit <sha> --role <发起人> \
  --changed <文件…> --base <基座> --main-head <main head>
```
队列**串行**：同一时刻只有一次合并（`coord merge-next` 取队首；已有合并在进行即拒）。
六道闸：① 条目已 delivered／verified；② commit 改动 ⊆ 白名单；③ 门禁证据存在且可核；
④ 基座已含 main 最新；⑤ **迁移链位／保真**（含迁移件时）；⑥ 内容冲突即拒。

## 二、机制面（git）
```bash
tools/merge_ff.sh --worktree <私有 worktree> --branch <分支> --main <主检出>
```
私有 worktree 对**当前 main** 组树 → 主检出**只** `merge --ff-only`；脏 worktree 直接拒（不 stash）；
快进竞态每轮**重新组树**。冲突是语义问题 ⇒ 转 TL 在 worktree 解，**禁自动取侧**。

## 三、合并后复核（吃过亏的一步）
差异为空 **且** 第二亲有独有改动 ⇒ 疑竞态，**重组**（历史上出现过"合并提交的树等于第一亲、第二亲三个文件全丢"）。
逐项核对文件集：预期 vs 实际。

## 四、收口
合并后**重跑门禁**（`--scope full` 或与交付同口径）再推送；推送后回标状态（`coord merge-ok`）。
`merge-conflict` 记录冲突拒绝项并转动作项。

## 反例
- 在共享主检出上组树/解冲突（要私有 worktree）。
- `push --force` 到主干；共享分支 rebase 已推送历史。
- 只看"合并成功"不看树与第二亲（静默丢内容就是这样进来的）。
