# ops/qcta —— 宿主侧"大命令顶坏整机"修复件（取样，2026-10-04）

这批不是本体系写的，是**这台机器上真实的护栏**（qcta 部署）。收在这里的原因只有一条：
它们治的正是**代理跑大命令把宿主顶坏**——本体系每天都在派命令，早晚要自己有这套判据。

## 一、治的是什么（两次实测事故）

| 事故 | 现象 | 直接原因 |
|---|---|---|
| 2026-10-03 测试拖垮整机 | 吃光 7.4G 物理 ＋ 8G swap，进 D 状态 thrashing；dsh-web 假死 20s、bg 应用事件循环反复停摆 | 单个 pytest 跑 2004 例 → 3.4G 常驻 ＋ 7.2G swap；并发数失控（实测同时 10 个） |
| 2026-10-04 磁盘被写爆 | 1.5h 内 73G → 136G（39%→73%） | 两个**孤儿 pytest-xdist worker**（PPID=1）空转 1h49m／1h23m，累计写 40.0GB ＋ 32.0GB；`/tmp` 累积 10674 个 pytest 临时目录（从不回收） |
| 同日的结构性挤压 | dsh-web cgroup 4298MB（配额 5G），`memory.events high` 累计 **50 万次**限流 | DSH 的 bash 工具**硬编码 `bash -c`**，插件配置只暴露 cwd/timeoutMs/maxOutputBytes，**没有任何 cgroup/slice 开关**（0.1.5-rc.1 全部包核对：无一处 "cgroup"）⇒ 代理派生的一切命令都留在 dsh 自己的 cgroup 里 |

## 二、件与判据

| 件 | 判据（可核的地方） | 节拍 |
|---|---|---|
| `bin/qcta-test-guard` | ① 单进程 RSS > `MAX_RSS_MB`（默认 2048，正常 150~250MB）→ 杀；② pytest 并发 > `MAX_CONCURRENT`（默认 4）→ **从最占内存的开始杀**直到达标；③④ 2026-10-04 加固：**写盘**与孤儿 worker | 30s timer |
| `bin/qcta-tmp-reap` | 目录名 `/tmp/pytest-<PID>` **就是创建它的主进程 PID** ⇒ PID 死即回收。**刻意不用 mtime**（目录 mtime 不随内部写入更新，会误删活跃目录）；**刻意不用 du**（上万个目录要跑 >180s，本身就是负担） | timer |
| `bin/qcta-tests-adopt` | 每 N 秒把 dsh-web cgroup 里**除 dsh 本体外**的进程搬进池 cgroup；cgroup v2 归属 fork 继承 ⇒ 只搬父进程即可。**两道保险防搬错 dsh**（比对 MainPID ＋ 跳过 `comm="node"`）——搬错会让 dsh 被 3G 配额限制、且 systemd 认为服务已停 | 常驻 |
| `systemd/qcta-tests-pool.service` | 独立配额池：`MemoryHigh=2G`（先限流）／`MemoryMax=3G`（硬墙）；依据"正常 150~250MB×4 并发 ≈ 1GB，但单个失控会涨到 3.4G" | — |
| `bin/qcta-heavy-run` | 重型作业（备份／日报／ClickHouse 全量）**共用一把文件锁**（`/run/lock`，tmpfs 无死锁残留）；等不到锁以 **75** 退出（明确失败，不静默跳过）；`nice 10` ＋ `ionice 7` | 被各 unit 调用 |

配置在 `default/`（`MAX_RSS_MB`／`MAX_CONCURRENT` 等）。

## 三、本体系该吸收的四条（不是搬件，是搬判据）

1. **跑测试要同时卡三条**：单进程内存、并发数、**写盘量**——只卡内存会漏掉"孤儿 worker 写爆磁盘"。
2. **回收按"创建者是否还活着"判，不按时间/体积判**（PID 嵌在名字里就是天然判据）——
   与本体系 `scratch_gc.py` 的"pid 活性＋资产豁免"同一条思路。
3. **代理派生的进程应与被借用的宿主分离配额**：dsh 的结构缺口（`bash -c` 无 cgroup）
   意味着**代理跑测试是在拿宿主的寿命做抵押**。本体系的 `tools/gate.py` 已把原始输出落盘，
   但**没有**任何并发/内存/写盘上限——这是缺口。
4. **限流事件数（`memory.events high`）本身就是判据**：50 万次限流说明"配额一直在被顶"，
   比"服务还活着"更能说明问题。

> 取样性质：这些是**那台机器**的落点（路径、单位名、阈值都属工程侧）。
> 本体系若吸收，只吸收上表里的**判据形状**，具体阈值应进工程绑定。
