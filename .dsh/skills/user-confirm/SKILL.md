---
name: user-confirm
description: commander 发需求（R）前必须取得用户显式确认的两步动作，含判据、被拒后的补法、审计口径。当要登记 R 需求、或 register 报 E_NO_USER_CONFIRM 时使用。
---

# 技能：用户确认后才发需求

## 何时用
要 `register` 一条需求（`--kind R`），尤其来源是 commander——**不先确认必被拒**。

## 两步（不许合成一步）
```bash
coord confirm  --role <会话> --line D --title "<与 register 逐字一致>" --said "<用户原话>"
coord register --kind R --line D --title "<同上>" --origin commander
```

## 判据（四条同时成立才作数）
- **内容绑定**：指纹 = 线别 ＋ 标题；确认了 A 不能拿去发 B。
- **一次性**：发号那一刻消费（`used_by`）。
- **会过期**：默认 12 小时（`--ttl-hours` 可调）。
- **必带用户原话**（`--said`）：唯一的证据面，空话术不算。

## 被拒 `E_NO_USER_CONFIRM` 怎么办
去问用户，拿到原话再确认；标题必须与本条**逐字一致**（差一个字就是另一条）。
在手确认查 `coord confirms`；存量缺口由 `coord audit` 报。

## 边界与反例
- 只管 commander 来源的需求；`line`／`dba`／`ops`／`pm` 来源照旧登记；`raise` 不受影响。
- **不许自造原话**；不许复用一条确认发多条；不许确认 A 发 B。
- **线 pm 能否开 dev：用户口径未规定**——别自行推定。
- 确认人自报无鉴权（与 `--role` 同级）：原话会跟着需求一路可见，伪造一眼可见。
