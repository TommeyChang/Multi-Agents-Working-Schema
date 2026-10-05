---
name: entry-lifecycle
description: 条目从登记到验收的九步生命周期：每步谁做、前置是什么、常见拒码怎么解、返工轮次怎么算。当要登记/分析/定稿/认领/交付/验收一个条目，或某动词被拒时使用。
---

# 技能：条目生命周期

## 步骤（每步都由"该由的角色"发起）
| 步 | 谁 | 前置 | 要点 |
|---|---|---|---|
| `register` | 任何人 | — | 号当场原子发放；R=需求，T=任务；commander 发 R 见 `user-confirm` |
| `claim-analyze` | PM | 登记／阻塞 | 记认领人 |
| `define` | PM（须为认领人） | 分析中 | 四要素：白名单／不动清单／前置／验收；R 升 T；**清空认领人** |
| `freeze` | PM／TL／commander | 已定稿 | 固化范围与验收快照；**认领即冻结**，此后改动走新条目 |
| `claim-dev` | TL | 已定稿且**就绪** | 未批预算 ⇒ 拒（先 `coord quota --line <线> --budget N`） |
| `start` → `deliver` | TL（认领人） | — | deliver 只记证据，**不产生 ✅** |
| `verify` | PM | 已交付 | 证据形式与可读性先过 |
| `accept` | PM | **已验证** | 实质验收；**功能条目必须有闭环路径**（`closure` 验收项） |
| `block`／`unblock` | 相关角色 | 非终态 | 须给理由；`blocked_from` 记录回退目标 |

## 常见拒码怎么解
- `E_INCOMPLETE`：四要素缺项（无白名单不成条目）；`E_NO_CLOSURE_PATH`：补 `--acceptance 'closure:…'` 或声明 `no_closure_path`。
- `E_UNMET`：验收项形式不满足（负例缺描述／证据路径不存在／闭环项缺描述）。
- `E_NO_DEB`／依赖类：前置不存在或成环 ⇒ 先登记/修正前置。
- `E_DOC_UNSYNCED`：改了口径却没同批改文档。
- 串行族（迁移件，见绑定 §迁移）在飞占号 ⇒ `E_NUMBER_INFLIGHT`，先等落物或让号。

## 返工与轮次
返工轮次 `round` +1；`accept` **只认最新轮**；失败轮次的证据**保留**（那是溯源的一部分）。
`override` 是逃生口，**必须给 reason**，且是终态唯一出口。

## 反例
- 未定稿就认领开发；共享检出上改文件（要 worktree）。
- 跳过 `verify` 直接 `accept`（结构上不可达）。
- 拿"单元都绿"当闭环——功能条目要端到端断言。
