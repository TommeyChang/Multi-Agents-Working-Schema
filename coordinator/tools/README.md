# tools — 本体系的工具

一工具一文件，各自可独立跑；**判据只写一处**，能被别处复用的绝不重写。

目录名用 `tools/` 而不是 `scripts/`：**`scripts` 是个中性词**（什么都装得下，也就什么都说明不了），
`tools` 说的是它是什么——**用来判、用来量的东西**。

| 脚本 | 一句话 | 来源 |
|---|---|---|
| `gate.py` | **门禁入口**：ruff ＋ 按范围跑 pytest，产出证据四件套（命令／退出码／摘要／落盘路径） | 本体系自持（吸收了目标仓门禁入口的用法） |
| `migration_gate.py` | **迁移闸**：静态判迁移图与号（唯一／父节点／单 head／全可达／取号单调／簿记比对） | 本体系自研（吸收了目标仓迁移闸的判据） |
| `calibrate.py` | 逐维压测 → 出并行度上限 W 与逐线预算**建议**（不写配额） | 本体系自研 |
| `db_ladder.py` | 触库**工作单元**：建 scratch 库 ＋ 全链迁移 ＋ 删库 | 改编自取样件 `ref/tools/scripts/tools/isolation_bench.py` |
| `impact_scope.py` | 测试**影响面**估计：直连 → 同名 → 退回整域 | 本体系自研（**域口径由本件持有**） |
| `commit_gate.py` | 提交前的**界内**闸：改动文件 vs 条目白名单／不动清单 | 改编自取样件 `ref/tools/.../precommit_checks/` |
| `merge_ff.sh` | 合并的 **git 机制**：私有 worktree 组树 → 主检出只快进 | 改编自取样件 `ref/tools/scripts/tools/merge_ff.sh` |
| `scratch_gc.py` | **孤儿 scratch 库回收**：命名空间／资产豁免／pid 活性／年龄 TTL／二次判定（**默认 dry-run**） | 改编自取样件 `ref/tools/scripts/dba/mysql_test_db_gc.sh` |
| `probe.py` | **存活探针**：直连探活、连续失败达阈告警——**没有任何重启路径** | 改编自取样件（`liveness_probe.sh` 的"只告警不重启"口径） |
| `preflight.py` | **开窗前预检**：五条腿把 fail-closed 判据前移，任一 BLOCK ⇒ 拒绝开窗 | 改编自取样件（`ops/preflight.py` 的"判据前移"口径） |
| `smoke.sh` | 冒烟：协调器端到端一口气跑通 | 本体系自研 |
| `watch.sh` | 监视器：脚本定时，**有 delta 才唤醒会话** | 本体系自研 |

---

## 一、改造索引：来源 → 保留 → 删除

**为什么要改造而不是直接引用主检出的原件**（三条，任一条都足以否决直接引用）：

1. **依赖绑死主检出**：绝对路径、它自己的 venv、以及测试内部（`import tests.conftest_mysql`）；
2. **判据绑死主仓体制**：`todo/registry.md` 取号簿、`T-*`／`R-*` 条目编号、DDL 标记、主仓路径表；
3. **只用得上其中一小部分**：原件为「一个仓的完整门禁」而写，本体系只需要其中与协作机制有关的那几条。

| 收进来的 | 来源原件 | **保留** | **删除**（理由） |
|---|---|---|---|
| `db_ladder.py` | `tools/tools/isolation_bench.py`（674 行） | 建／删 scratch 库、全链迁移调用、前缀守卫、残留清理、保护名单、机读输出 | 三种隔离原语（truncate／rollback／none）与用例体读写——那是**隔离原语选型**的问题，不是**并行预算**的问题；`import tests.conftest_mysql`——会绑到主检出测试内部，改为本地生成**同形状**库名（dba 的 GC 仍认）；表数敏感性开关与 spike 报告框架；**合成建表语句**（它量不到迁移链与 DDL 锁竞争，实测差一个数量级） |
| `commit_gate.py` | `git_hooks/precommit_checks/`（695＋211 行） | BLOCK：不在白名单／命中不动清单；WARN：动了设计面但没同批改文档、白名单为空；BLOCK/WARN 分级与退出码 | C1 取号同步、C2 迁移 revision 撞号、C4 DDL 标记、主仓 `SCOPE_GATE_SHARED_*` 路径表、row-drop 标记、commit message 与 merge 豁免、hook 安装口——**全是主仓体制需要的，本体系没有对应的物**；未发明替代品 |
| `merge_ff.sh` | `tools/tools/merge_ff.sh`（368 行） | 私有 worktree 对**当前 main** `--no-ff` 组树 ＋ 主检出只 `--ff-only`；脏 worktree 拒绝（不 stash）；竞态重试**每轮重新组树**；`index.lock` 活锁有界等待；`--dry-run` 零写；语义化退出码 | 主仓合并消息模板与任务编号措辞（复述＝第二份真相源）、`WORKTREE.md` 条文引用（本体系权威是 `rules/WORKSPACE.md`）、`tq_*` 领域件、假定主仓目录布局的分支；另有两项判断性精简：收尾复原入口 HEAD、空合并复核（内容复核是**合入者**的独立步骤，非 shell 职责） |
| `scratch_gc.py` | `scripts/dba/mysql_test_db_gc.sh`（227 行）＋ `scripts/tools/reap_stale_scratch.py`（147 行） | 命名空间／资产豁免（保护名单＋模板池）／pid 活性／年龄 TTL／**DROP 前二次判定**／DROP 会话 `lock_wait_timeout` | `mysql` 命令行依赖（改用 pymysql）、bash 版的前缀注入环境变量（改为 `--prefix`）、旧族命名空间默认值——**目标仓的历史包袱不留**；`--cleanup` 从 `db_ladder.py` 挪到这里（**一处口径**） |
| `probe.py` | `scripts/ops/liveness_probe.sh`（63 行） | 直连探活（绕反代）、连续失败达阈、结构化告警行、状态文件、**只告警不重启** | 硬编码的 URL／状态目录（改为 `--url`／`--state-dir`）、systemd timer 专有假设、`journal` 写入方式（改为 stderr 结构化行，谁采集谁决定） |
| `preflight.py` | `scripts/ops/preflight.py`（174 行） | "**把 fail-closed 判据前移到开窗前**"这个做法本身；任一 BLOCK 即拒绝开窗 | 目标仓专有的腿（路由矩阵归类、venv／端口／依赖可达、按自身树解析代码、`.bg-ddl.env` 库解析序）——**那些是那个仓的部署形态**；本体系换成自己有的五条腿（协调器自检／迁移图／触库可用／scratch 残留／宿主余量） |
| 判据（不新增文件） | `scripts/tools/alloc_number/_check.py`（395 行） | **同号双占**（同族同号两行 ⇒ `E_NUMBER_TWICE`）；**三态互斥且铺满**（`1..high` 每个号恰好属于 已落物／待建／已让号／空洞 之一，钉在测试里） | 不另立第二套账：取号、三态、让号留痕本体系**已有**（`engine.alloc_number`／`reserve`／`materialize`／`release`，让号本就要求理由）；「散文引用不计占用」在 `todo/registry.md` 簿上才成立，本体系无此簿，故不移植 |

**未收进来**的取样件（`ref/tools/` 里留着对样子）：`todo_status.py`（本体系已用 `coord report`／有界读替代）、
`reap_stale_scratch.py` ＋ `mysql_test_db_gc.sh`（回收执行权归 dba，本体系只记状态）、
`alloc_batch.py`／`alloc_number/`／`*_race_check.sh`（取号那套的完整实现，本体系只折了判据）、
`merge_integrity.py`（内容复核归合入者）。

---

## 二、调用关系（谁用谁）

```
gate.py       ──用──▶ impact_scope.py            门禁的"受影响面"由本体系自己算
gate.py       ──驱动──▶ 【目标仓】ruff／pytest     目标仓只提供负载（它的测试、它的配置）
calibrate.py  ──用──▶ db_ladder.py --unit         触库维度不自拼 SQL，单元只一处来源
migration_gate.py ──读──▶ 【目标仓】alembic/versions/**   静态判图与号，不连库、不执行
preflight.py  ──调──▶ migration_gate.py ＋ db_ladder.py ＋ scratch_gc.py   开窗前把判据跑一遍
scratch_gc.py ◀── dba 跑（默认 dry-run；--apply 才动手；协调器只记 reclaim 状态）
probe.py      ◀── ops 定时跑（只告警不重启）
commit_gate.py ◀── dev 提交前自己跑（读协调器状态，只读）
merge_ff.sh    ◀── TL 合入时跑（协调器只记状态，git 机制归本脚本）
watch.sh       ──探查──▶ 协调器状态根            有 delta 才唤醒会话
smoke.sh       ──覆盖──▶ 以上全部（端到端）
```

**自持边界**：本体系**不调外部工具**，只驱动**目标仓的内容**（测试、迁移件、diff）与系统工具 `git`。
权威只有一处：**状态与号归协调器，门禁／迁移／界内／合并的判据归本目录**——
不设"某件外部脚本说了算"的第二权威。这条由 `tests/test_selfcontained.py` 钉住。

## 三、约定

- 一律 `python3 tools/<名>.py --help` 可自证用法；退出码语义写在各自文件头。
- **只读的就说只读**（`commit_gate.py`／`impact_scope.py` 零写盘，有测试钉住）。
- **不猜默认值**：状态根与仓库路径只认显式参数或环境变量；
  猜错的代价是**改别人的工作区**（实测事故：一次 `publish` 把真实 `todo/` 13 个文件、2306 行覆盖成 23 行）。
- **量不出来就说量不出来**：标定器把「串行基准就失败」的维度排除在 `min(各维)` 之外并报原因，
  不把「本来就坏」算成机器上限。
