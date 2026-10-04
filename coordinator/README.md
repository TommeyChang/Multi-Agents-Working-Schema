# 任务协调器（bg-coordinator)

> 多角色调度的**协议层与存储层**。设计文档见同目录 `DESIGN.md`；协作规则见同目录 `Multi-Agents-Working-Schema/`。
> **独立目录、仓库之外**——本模块不属于 `futures-broker-gateway` 仓库，不进它的 git。

## 它解决什么

| 问题 | 实测证据 |
|---|---|
| 共享文件写竞争 | `todo/` 相关提交 **4268**；近 40 条提交 **40/40** 触及 `todo/` |
| 合并仪式过重 | 近 80 条合并中 `pm/*` : `dev/*` = **19 : 2**（80% 是为 `todo/` 打的） |
| 资源枯竭 | 128 个 worktree 目录；scratch 库靠手动回收；MDL 残留曾致库长期占用 |

**根因**：markdown 表格是**"直读"逼出来的编码格式**——把结构化字段压进一行文本，
为维持它累积了一整套加固纪律（C3／C8／C9／转义切分／precommit 删除守护腿／号段三分类）。

**协调器把"编码"还原成"数据"**，于是那一整套加固纪律**整类退役**。

## 三条核心原则

1. **协调器只执行动词，不做决策**——判定规则住在配置与校验里。
2. **"现在是什么"归协调器，"曾经发生过什么"归 commit。**
3. **协调器只判「形式有效」，不判「内容正确」**——机械通过 ≠ 实质正确。

## 安装与自检

```bash
cd /root/futures-broker-gateway-workspace/coordinator

# 测试与门禁（用仓内 venv，纯标准库实现、无第三方依赖）
../../futures-broker-gateway/.venv/bin/python -m pytest -q
../../futures-broker-gateway/.venv/bin/python -m ruff check .

# 初始化状态根
../../futures-broker-gateway/.venv/bin/python -m bg_coordinator.cli init
../../futures-broker-gateway/.venv/bin/python -m bg_coordinator.cli health
```

装成命令（可选）：

```bash
../../futures-broker-gateway/.venv/bin/python -m pip install -e .
coord health
```

## 状态落点

```
/root/futures-broker-gateway-workspace/
├── futures-broker-gateway/          ← 仓库
│   └── todo/ reports/               ← 【视图】单向渲染，只读语义
└── coordinator/                     ← 【权威】仓库之外
    ├── events.jsonl                 ← append-only，**权威**
    ├── state.json                   ← 可重算缓存
    ├── quota.json                   ← 资源配额策略
    ├── snapshots/                   ← events 快照（发布时同步进仓库一份）
    └── .lock                        ← flock
```

> **`events.jsonl` 只准追加。任何就地编辑都是事故**——状态机会被静默污染且不可重建。
> 反过来，`state.json` 丢了没关系：`coord health` 会从事件重建。

**为什么在仓库之外**：仓库内任何路径都可能被 `git clean -xfd` 清掉；
工作区根不在 git 对象面内，天然免疫。且它不在任何 worktree 里 ⇒ **worktree 副本这个变量彻底消失**。

## 动词表

### 条目生命周期

| 动词 | 角色 | 前置状态 | 说明 |
|---|---|---|---|
| `register` | 任何人 | — | 登记；发号原子化 |
| `claim-analyze` | PM | registered／blocked | 认领分析 |
| `define` | PM | analyzing | **四要素齐，缺项即拒**；R→T（共用 id）；**定稿即释放 owner** |
| `claim-dev` | TL | defined 且就绪 | 跑五条就绪判定 |
| `start` | TL | claimed | — |
| `deliver` | TL | in_progress | 记证据（commit／门禁／路径），**不标 ✅** |
| `verify` | TL | delivered | **形式校验**：证据在／路径可读／退出码 0 |
| `reverify` | TL | delivered／verified | 返工：`round` +1，**失败轮次保留** |
| `accept` | PM | **只能来自 verified** | 实质验收 → ✅ |
| `block`／`unblock` | 协调器／TL | 非终态 | 回退到进入前的状态 |
| `complete` | OPS | — | OPS 线自主闭环 |
| `review` | DBA | — | 评审即入库 |
| `set-priority` | PO／commander | 非终态 | P0/P1/P2 |
| `override --reason` | commander | 任意 | **唯一强制转换**，reason 必填 |

### 资源租约

| 动词 | 说明 |
|---|---|
| `acquire` | 申请。配额满 → **先即时回收**再排队 |
| `release` | 显式释放 |
| `reclaim` | **执行层回调**（dba 真正 DROP 之后调用）；`--mdl` 表示因元数据锁失败 |
| `leases` | 只读：用量／可回收清单 |

### 合并队列

| 动词 | 说明 |
|---|---|
| `merge-request` | 显式触发。**六闸** |
| `merge-next` | 取队首。**串行闸**：已有 merging 即拒 |
| `merge-ok` | 记录结果提交 |
| `merge-conflict` | **冲突即拒** → 转动作项给 TL。**永不自动解冲突** |
| `merge-status` | 队列可观测面 |

### 查询

| 动词 | 说明 |
|---|---|
| `status` / `health` | 一行摘要／自检 |
| `ready --role-name tl` | 可认领清单（按优先级排序） |
| `trace <id>` | **正查**：这条目凭什么算完成？谁验的？ |
| `why <path>` | **反查**：这个文件为什么是这个样？谁授权的？ |
| `audit [--summary]` | **只产异常**；通过项静默 |
| `report` | 人读报告（七节，**只列异常与待决**） |
| `publish` | 渲染视图 → 显式路径提交 → events 快照 |

## 端到端示例

```bash
C=../../futures-broker-gateway/.venv/bin/python
coord() { $C -m bg_coordinator.cli "$@"; }

coord init
coord register      --id T-D-236 --role po:po --title "K 线取数" --line D --priority P1
coord claim-analyze --id T-D-236 --role pm:pm-D:D
coord define        --id T-D-236 --role pm:pm-D:D \
                    --whitelist 'data_access/**' --frozen 'main.py' \
                    --acceptance 'test:uv run pytest -q' \
                    --acceptance 'negative:未注入仍 200 ⇒ 必红'
coord ready --role-name tl
coord claim-dev     --id T-D-236 --role tl:TL-D:D
coord start         --id T-D-236 --role tl:TL-D:D
coord deliver       --id T-D-236 --role tl:TL-D:D \
                    --commit de1f3a2 --gate-cmd 'uv run pytest -q' --gate-exit 0 \
                    --evidence worktrees/T-D-236.SUBMISSION.md \
                    --changed data_access/kline/follow.py
coord verify        --id T-D-236 --role tl:TL-D:D
coord accept        --id T-D-236 --role pm:pm-D:D
coord trace T-D-236
coord report
```

**退出码**：0 ＝ 通过；1 ＝ 业务拒绝（stderr 一条 `REJECTED [E_XXX] …`）；2 ＝ 环境/参数错误。

## 模块结构

| 文件 | 职责 |
|---|---|
| `models.py` | 数据模型——**存储格式即数据本身**，不再是编码 |
| `errors.py` | 拒绝码与异常（结构化、可枚举） |
| `statemachine.py` | 状态机——**纯函数**，§8 推导表的直译 |
| `validators.py` | 八条不变量 ＋ 白名单匹配 ＋ 依赖图 |
| `readiness.py` | 就绪判定与额度闸（**把纪律变成函数**） |
| `engine.py` | 核心引擎——`(state, verb, params) → (state', event)`，纯函数 |
| `storage.py` | 锁、原子写、events 追加、状态投影、发布（**唯一碰磁盘**） |
| `merge.py` | 合并队列（六闸／串行／冲突拒绝） |
| `audit.py` | 溯源（正查／反查）与审核（**只产异常**） |
| `render.py` | 视图 A（兼容，保现有工具零改造）＋ 视图 B（人读报告） |
| `cli.py` | 唯一写入口 |

**组织原则**：引擎是纯函数，所以状态机与校验规则可以**脱离环境完整测试**；
所有副作用集中在 `storage.py`。

## 测试

```bash
../../futures-broker-gateway/.venv/bin/python -m pytest -q      # 184 passed
```

| 测试文件 | 覆盖 |
|---|---|
| `test_statemachine.py` | **可达性／无死锁／确定性**（§8.4 的断言化）；`accept` 只来自 `verified` |
| `test_validators.py` | 八条不变量；`*` 不跨目录；依赖环 |
| `test_engine.py` | 端到端七步；每条拒绝路径；幂等／竞态／冻结／返工 |
| `test_storage.py` | **事件只追加**；幂等；`state.json` 丢失后重建；锁排他；快照 |
| `test_lease.py` | pid 活性；TTL；配额满时**先回收再放行**；MDL 路径 |
| `test_merge.py` | 六闸逐条；**串行闸**；FIFO；冲突转动作项；**队列项不持所有权** |
| `test_audit.py` | 正查／反查；**健康态零异常**；各类异常 |
| `test_render.py` | 视图 A 字节级兼容（7 格／token 首字符／转义）；**报告不出现原材料** |
| `test_cli.py` | 全动词端到端；退出码契约；幂等；`expect-ver` |

## 已知限制（明说）

| 限制 | 说明 |
|---|---|
| **`--role` 自报无鉴权** | 任何会话可声称任意角色。**比现状弱**（现在"谁写的"有 git 提交者可对照）。缓解：角色声明记入事件流 |
| **协调器是单点** | 它挂了任务写入停摆（代码工作不受影响）。状态全在磁盘、可重建 |
| **MDL 层未解决** | 租约解决①库残留，**未碰③MDL 残留**（DROP 被元数据锁挂住）——须单独立项 |
| **审计滞后于 live** | 投影发布前，新条目对 `process_audit` 不可见。报告显式标注快照点 |
| **pid 复用** | `kill -0` 在 pid 复用时误判存活。缓解：TTL 兜底 |
| **配额数值未标定** | `quota.enabled=false` 默认只观测。**先观测后启用** |
| **git 延迟仍在** | worktree 需 `pull` 才见最新视图；但已从"合并义务"降为"拉取事项" |

## 迁移阶段（本模块对应阶段一~五）

| 阶段 | 内容 | 本模块状态 |
|---|---|---|
| 零 | 修泄漏源（回收器判据合并＋定时化） | 租约判定层已就绪；执行层归 dba |
| 一 | 只读快照 | ✅ 已实现（`status`／`health`／`audit`／`trace`） |
| 二 | 视图落盘（装成旧格式） | ✅ 已实现（`render_compat` ＋ `publish`） |
| 三 | 接管写入 | ✅ 已实现（全部动词）；**切换需另开条目** |
| 四 | 资源租约 | ✅ 已实现；**配额启用待标定** |
| 五 | 合并队列 | ✅ 已实现 |
| 六 | 读者切换 → 渲染器简化 | 工具脚本（`tools/`） |
