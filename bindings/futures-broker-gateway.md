# 工程绑定 — futures-broker-gateway

> **这一份是"本工程"的落地声明**：线别与工作面、门禁、域映射、迁移、受保护资产、部署窗口段序。
> 体系（MAWS）只规定**机制**；机制的**落点**在这里。**换一个工程，只需要新写一份这样的文件。**
>
> 人读部分在下，机读部分是末尾那个 ```json 块——两者必须一致，`coord bind` 会逐字段校验。

## 一、这份绑定为什么在体系侧

按设计，绑定应当**随工程版本走**（可评审、可 diff、可回滚），也就是放在主干侧
`<主干>/.maws/project.md`。当前它放在体系侧 `bindings/`，原因只有一条：
**本轮改动面限定在体系内，不动生产仓**。

解析顺序（`coord bind` 与协调器都按它走）：

1. `<主干>/.maws/project.md`（**主干侧，优先**——将来搬家后自动生效，机制不用改）；
2. `bindings/<工程目录名>.md`（**体系侧**，当前用的就是这条）。

## 二、线别与工作面

**（口径来源与确认状态）** 下表据**条目号前缀**（`T-A-*`／`T-C-*`／`T-D-*`／`T-E-*`／`T-ACL-*`／`OPS-*`）
与工程看板文件（`todo/lines/*.md`）、以及门禁的路径→域映射**推定**。
**请工程方校正**——校正后只改这份文件，体系侧一个字都不用动。

| 线 | 面 | 工作面（白名单的默认来源） |
|---|---|---|
| `A` | auth | `auth/`、`tests/auth/`、`docs/AUTH_DOMAIN.md` |
| `B` | notification | `notification/`、`tests/notification/`、`docs/NOTIFICATION.md` |
| `C` | data_access | `data_access/`、`tests/data_access/`、`docs/DATA_ACCESS.md` |
| `D` | 领域与组合根 | `broker_gateway/`、`tests/broker_gateway/`、`docs/BROKER_GATEWAY_*.md` |
| `E` | 基建（含 DFS） | `dfs/`、`scripts/tools/`、`tests/infra/` |
| `ACL` | 权限与端点矩阵 | `broker_gateway/acl/`、`tests/broker_gateway/acl/` |
| `OPS` | 运维 | `deploy/`、`scripts/ops/`、`scripts/dba/` |

## 三、门禁与域

- **入口**：`tools/gate.py`（本体系自持；默认目标仓就是本工程）；
- **快跑通道**：`pytest -m "not db"`——**真库档不得与其它会话并发**（工程原口径见 `docs/TESTING.md §3`）；
- **发布前**：全量（`tools/gate.py --scope full`）；
- **路径→域**：`auth/`→auth、`data_access/`→data_access、`notification/`→notification、
  `broker_gateway/`→broker_gateway、`dfs/`→infra、`scripts/tools/`→infra；
- **共享面**（改这些要跑全部域）：`main.py`、`settings.py`、`logging_config.py`、`pyproject.toml`、
  `alembic_metadata.py`；
- **静态腿**（不触库的秒级闸，清单是工程数据）：见机读块；
- **复杂度闸（本工程必须用 radon）**：工具 `radon`（**已在本仓 dev 依赖**：
  `pyproject.toml` 的 `[dependency-groups] dev` ＋ `uv.lock`），判据在
  `coordinator/tools/complexity.py`。
- **与工程侧 C20 的分工（两层，不是两套）**：

  | 层 | 谁 | 判什么 | 级别 |
  |---|---|---|---|
  | **存量台账** | 工程侧 `process_audit` 的 **C20** | 绝对阈值（CC ≥ 21、MI < 20）＋**冻结基线清单**（只减不增） | **报黄留痕**（不阻断） |
  | **新增闸** | 本体系 `tools/complexity.py`（门禁一条腿） | **相对分叉点不退化**：新增／变差到 C 以上 | **BLOCK** |

  一句话：**C20 是账本（存量有多大、谁在还），ratchet 是闸（不许再欠）**。
  两者**扫描面必须同一处**（见下），否则同一个函数一边黄一边绿。
- **扫描面 ＝ 生产面（对齐 C20 的 `RADON_SCAN_DIRS` ＋ `RADON_SCAN_FILES`）**：
  `auth`、`broker_gateway`、`data_access`、`notification`、`main`、`dfs`、
  `main.py`、`settings.py`、`logging_config.py`、`executors.py`。
  **不含** `tests/`·`research/`·**`scripts/`**——测试函数天然多分支、探针是一次性件，
  拉进来只会淹没有效信号（实测：全仓最烂的块全在 `scripts/ops/*`，F=61）。
  这条一致性由 `tests/test_complexity.py` 的同步闸钉住（读 C20 源码常量比对，对不上即红）。
- **口径**：本分支相对**分叉点**（`merge-base(origin/main, HEAD)`）新增或变差的块 ⇒
  **C 级及以上 BLOCK**、**B 级 WARN**；变小／消失不判。
- **为什么不是绝对阈值**：存量实测（生产面 262 文件／3264 块）
  **A 2927／B 254／C 78／D 4／E 1／F 0**——"不许有 C"会一次染红 82 个存量块，
  闸立刻变噪声被绕过（**假红比没闸更坏**；与工程侧 C20 台账的「C 档 78」同面同数，互相印证）。
  存量债走 C20 台账 ＋ 重构条目，本闸只管"新欠的债"。
- **在哪儿跑**：**分支侧**（dev／TL 在自己的 worktree 里跑 `tools/gate.py`，证据入档）。
  ⚠ **别在共享主检出里跑**：那里未提交面是**所有人**的在途改动，ratchet 会把别人的账
  算到你头上（实测首跑就逮到另一会话在途的新增 C 级函数）。
- **缺 radon 即红**：声明了就得跑，radon 不在目标仓解释器里 ⇒ 腿退出 2 ⇒ 门禁红。
  （C20 那边缺 radon 是"跳过＋notes"——那是它的面；**门禁整体仍红**，由本闸这条腿兜住。
  若要让 C20 也红，得改工程侧 `_checks_quality.py`，属产品仓改动，另立条目。）
  `--no-complexity` 是显式逃生口，证据里会少一条腿，评审看得见。
- **现状量级**（"域粒度为什么太粗"的依据，实测）：全仓 ≈4939 用例；
  `broker_gateway` 一个域 ≈2774 例（**56%**）、`data_access` 785、`infra` 744、`auth` 476。
  收窄测试范围（按影响面）的理由就来自这个量级。

## 四、迁移

- 目录：`alembic/versions/`；工具：`alembic`；命名：`<NNNN>_<slug>`，号经协调器取；
- **基线**：`origin/main`——三个用途：新增件该接在谁后面、**哪些件算"已落库"（冻结面）**、
  以及算本分支的**分叉点**（`merge-base(origin/main, HEAD)`，判"差异是本分支的还是落后的"）；
- **已知坑**：`alembic check` 只拿 head 与 metadata 比，**不校验降级保真**——降级后必报差异，
  别把那个差异当成"实现漂移"。

### 三个收口点（**放号只保证号唯一，保证不了链位唯一**）

迁移件的冲突和普通文件不一样：两个 DBA 各自**合法地**取号、各自把父节点接到当时的 head 上，
合起来就是**两个 head**——升级目标不唯一，谁也升不到头。所以要在三处收口：

| 收口点 | 谁执行 | 判什么 |
|---|---|---|
| **放号** | 协调器（`SERIAL_FAMILIES`） | 该族**同时只允许一个在飞占号**——号顺序就是链位顺序 |
| **提交** | `tools/commit_gate.py` | 本批含迁移件 ⇒ 单 head／父节点齐／全可达／新增件号单调且父节点 == 基线 head／**已落库件未被就地改写或删除** |
| **合并** | `merge.py` 链位闸（`tools/commit_gate` 同源判据） | 同上，且对的是**即将落上去的 main head** |

判据只有一处实现：`coordinator/bg_coordinator/migrations.py`（链位 `[1]`~`[5]`、保真 `[6]`）。

**本工程的"已落库"判据**：路径在 `origin/main` 树里 ⇒ 冻结。**未合入的新件可自由返工**
（那是 `[5]` 的地盘）——返工窗口就是"合入前"，这正是 R-D-90 那类误报要保住的东西。

**撞上了怎么办（收口动作）**：后落地的那件把 `down_revision` 改指到**新的 head**，
号保持（号已单调，不用重取）；若号被插队（比基线最大号还小），**让号重取**——
号是资源，插队会让号顺序与链位顺序脱钩。
**已落库件被改写／删除** ⇒ 不是"改回来"就完事：`0028` 那种已落两库的件被改，
`alembic check` 会在合入后报漂移。正当动作 = **新开下一 revision 补偿承载**；
纯注释／docstring 更正可直接改（闸会 AST 比对后放行并留痕）。

## 四·五、测试口径（供「自称与事实」闸用）

- **真库标记**：`db`；**自动打标**——采集期按夹具闭包判定（`tests/conftest.py`），
  文件里可以不写，所以"文件里没写 marker"**不等于**不在真库；
- **真库夹具**：`mysql_scratch`／`db_session`／`session_factory`／`db_engine`／
  `scratch_store`／`scratch_url`／`_rd_database`／`migrated_engine`／`db_url`（见机读块）；
- **基座**：`tests/conftest_mysql.py`；
- **硬要求（本条已转成闸）**：**并发用例必须在 MySQL 载体跑**——
  SQLite 忽略 `FOR UPDATE`，不得冒充并发正例。判据由 `tools/claims.py` 执行：
  自称并发 ∧ 引用 `for_update` ∧ 既没标真库也没用真库夹具 ⇒ **BLOCK**。
  **现状 0 命中 ＝ 这条此刻被遵守；闸的价值在下一个写错的人。**

### 四·五·一 测试分级（2026-10-05）

`static`（每次改动）→ `affected`（交付前，`-m "not db"` 快跑通道，只跑本域）→
`domain`（合并前，含真库档，真库档不并发）→ `full`（仅发布或命中公共面）。
**哪一级必须齐**由 `tools/testplan.py` 按上表算出；**交付证据申报级别**，合并前同级别复跑。

## 五、资产与命名空间（DBA 面）

- **受保护资产（GC 永不回收）**：`broker_gateway`、`broker_gateway_dev`、`bg_tmpl_*`（模板池）；
- **scratch 命名空间**：`bg_`（`bg_<族>_<sha8>_<pid>_<rand8>`，内嵌 pid 段）；
- 回收判据与执行口：`tools/scratch_gc.py`（默认 dry-run）。

## 六、部署窗口段序（OPS 面）

`停服 → 迁移 → 重铺 → 预检 → 起服 → 验证留痕`

**开窗前**跑 `tools/preflight.py`；窗口期间保持位必须存在（否则看护件可能中途把服务拉起来）。

**直连探活地址**（绕反代；存活探针用它）：`http://127.0.0.1:18080/health`
——`tools/probe.py --url` 的值从这里取，**不写进角色文件**。

## 七、工程自己的文档（本体系不复述）

| 文档 | 装什么 |
|---|---|
| `AGENTS.md` | 角色调度、共通纪律、回复风格、pm/dev/ops 职责基线 |
| `WORKTREE.md` | 工作区与合入口径 |
| `docs/TESTING.md` | 测试口径（含真库档并发纪律） |

> **重复面提醒**：`AGENTS.md` 的「共通纪律」与 `WORKTREE.md` 的合入/回收口径，
> 与本体系 `rules/SUBAGENT.md`／`rules/WORKSPACE.md` **内容重叠**。
> 按设计，机制应以体系为唯一权威、工程侧只留工程口径——
> 但**本轮不动生产仓**，这个重叠留待工程方允许改动时收口。

---

## 机读块（`coord bind` 逐字段校验的就是它）

```json
{
  "project": "futures-broker-gateway",
  "version": 1,
  "lines": {
    "A": {"name": "auth", "workface": ["auth/", "tests/auth/", "docs/AUTH_DOMAIN.md"]},
    "B": {"name": "notification", "workface": ["notification/", "tests/notification/", "docs/NOTIFICATION.md"]},
    "C": {"name": "data_access", "workface": ["data_access/", "tests/data_access/", "docs/DATA_ACCESS.md"]},
    "D": {"name": "领域与组合根", "workface": ["broker_gateway/", "tests/broker_gateway/", "docs/BROKER_GATEWAY_*.md"]},
    "E": {"name": "基建（含 DFS）", "workface": ["dfs/", "scripts/tools/", "tests/infra/"]},
    "ACL": {"name": "权限与端点矩阵", "workface": ["broker_gateway/acl/", "tests/broker_gateway/acl/"]},
    "OPS": {"name": "运维", "workface": ["deploy/", "scripts/ops/", "scripts/dba/"]}
  },
  "gate": {
    "entry": "tools/gate.py",
    "fast_marker": "not db",
    "domains": {
      "auth/": "auth",
      "data_access/": "data_access",
      "notification/": "notification",
      "broker_gateway/": "broker_gateway",
      "dfs/": "infra",
      "scripts/tools/": "infra"
    },
    "shared_faces": ["main.py", "settings.py", "logging_config.py", "pyproject.toml", "alembic_metadata.py"],
    "static_legs": [
      "tests/infra/test_dfs_funnel_gate.py",
      "tests/infra/test_td108_heal_guard.py",
      "tests/infra/test_db_marker_coverage.py",
      "tests/infra/test_query_secret_gate.py",
      "tests/infra/test_transaction_boundary_audit.py",
      "tests/infra/test_alloc_number.py",
      "tests/infra/test_process_audit.py",
      "tests/infra/test_git_hooks.py"
    ],
    "release": "全量 pytest（ruff ＋ 按目录分组，每目录独立进程）",
    "db_discipline": "真库档不得与其它会话并发（工程 docs/TESTING.md §3）",
    "complexity": {
      "tool": "radon",
      "base": "origin/main",
      "paths": ["auth", "broker_gateway", "data_access", "notification", "main", "dfs", "main.py", "settings.py", "logging_config.py", "executors.py"],
      "floor": "C",
      "warn_at": "B",
      "scan_face_owner": "product C20 (RADON_SCAN_DIRS + RADON_SCAN_FILES)：两处必须同集合，同步闸 tests/test_complexity.py",
      "baseline_measured": "生产面 A 2927 / B 254 / C 78 / D 4 / E 1 / F 0（3264 块，2026-10-04 实测）"
    }
  },
  "testplan": {
    "domains": {
      "auth": ["auth"],
      "broker_gateway": ["broker_gateway"],
      "data_access": ["data_access"],
      "notification": ["notification"],
      "infra": ["scripts", "deploy", "alembic"],
      "behavior": ["tests/behavior"]
    },
    "public": ["main", "dfs", "alembic/versions"],
    "note": "域→源路径前缀：单域⇒affected；跨域⇒domain；命中 public⇒full（2026-10-05 分级口径）。budget 待实测后填。"
  },
  "migrations": {
    "dir": "alembic/versions",
    "base": "origin/main",
    "tool": "alembic",
    "naming": "<NNNN>_<slug>",
    "gotchas": ["alembic check 只对 head 比 metadata，不校验降级保真"]
  },
  "tests": {
    "db_marker": "db",
    "db_fixtures": [
      "mysql_scratch", "mysql_scratch_db", "db_session", "mysql_scratch_url",
      "session_factory", "db_engine", "scratch_store", "scratch_url",
      "_rd_database", "migrated_engine", "db_url"
    ],
    "substrate": "tests/conftest_mysql.py",
    "auto_marker": "采集期按夹具闭包自动打 db 标（tests/conftest.py）；文件里可以不写",
    "claims": [
      "并发用例必须在 MySQL 载体跑——SQLite 忽略 FOR UPDATE，不得冒充并发正例"
    ]
  },
  "protected_assets": ["broker_gateway", "broker_gateway_dev", "bg_tmpl_*"],
  "scratch_namespace": "bg_",
  "window": ["停服", "迁移", "重铺", "预检", "起服", "验证留痕"],
  "shared_files": [
    "todo/**",
    "tests/README.md",
    "tests/conftest*.py",
    "docs/**",
    "AGENTS.md",
    "WORKTREE.md",
    "alembic/**"
  ]
}
```
