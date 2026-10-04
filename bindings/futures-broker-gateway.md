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
- **现状量级**（"域粒度为什么太粗"的依据，实测）：全仓 ≈4939 用例；
  `broker_gateway` 一个域 ≈2774 例（**56%**）、`data_access` 785、`infra` 744、`auth` 476。
  收窄测试范围（按影响面）的理由就来自这个量级。

## 四、迁移

- 目录：`alembic/versions/`；工具：`alembic`；命名：`<NNNN>_<slug>`，号经协调器取；
- **已知坑**：`alembic check` 只拿 head 与 metadata 比，**不校验降级保真**——降级后必报差异，
  别把那个差异当成"实现漂移"。

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
    "db_discipline": "真库档不得与其它会话并发（工程 docs/TESTING.md §3）"
  },
  "migrations": {
    "dir": "alembic/versions",
    "tool": "alembic",
    "naming": "<NNNN>_<slug>",
    "gotchas": ["alembic check 只对 head 比 metadata，不校验降级保真"]
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
