---
name: complexity-ratchet
description: 复杂度闸（radon ratchet）：改生产面代码前后怎么跑、怎么读分布、被判了怎么办。当改动触及生产面代码、或 gate/commit 报复杂度 BLOCK 时使用。
---

# 技能：复杂度 ratchet

## 何时用
改动触及绑定 §门禁 `complexity.paths` 的生产面（本工程：`auth`／`broker_gateway`／`data_access`／`notification`／`main`／`dfs` ＋ 四个根模块）。

## 口径
- **相对分叉点不退化**，不是绝对阈值：新增／变差到 **C 级 ⇒ BLOCK**，**B 级 ⇒ WARN**，变小不判。
- 为什么不用绝对线：存量实测 A 2927／B 254／C 78／D 4／E 1／F 0，"不许有 C"会一次染红 82 个存量块。
- **radon 只出数，判据在体系侧**（不引 Xenon，阈值不进别人的参数）。
- 扫描面 ≡ 工程侧 C20 的常量（有同步闸，对不上即红）。

## 怎么跑
```bash
python3 tools/complexity.py --target <仓>            # 口径从绑定取；--json 给分布
python3 tools/gate.py --scope static                 # 门禁腿；缺 radon ⇒ 退出 2 ⇒ 红
```
**只在私有 worktree 跑**：共享主检出的未提交面是所有人的，ratchet 会把别人的账算到你头上。

## 被判了怎么办
- `[新增]`／`[变差]`：拆小或提函数；别为了过闸把阈值抬上去。
- 存量块（不在本分支引入的）不归你——存量债走 c20 台账 ＋ 重构条目。
- 确属无法拆：另立条目说明，不要用 `--no-complexity` 逃（那会在证据里少一条腿）。

## 反例
- 把 `radon cc` 的绝对数字当闸（它只报告，不拦）。
- 在共享检出上跑（他人在途代码会被算成你的）。
- 只看告警不看分布：分布是"存量债有多大"的唯一证据。
