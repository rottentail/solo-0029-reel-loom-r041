# 单机容器交付与滚动替换演练

本目录把声画织场打包为可在单机 Linux 上长期运行的容器交付，沿用**单实例独占数据目录**的边界：
一个数据卷同时只允许一个服务进程（应用层 `reel_loom.lock` 实例锁兜底，编排层也必须把副本数固定为 1）。

## 边界与不变量

| 主题 | 约定 |
| --- | --- |
| 镜像根文件系统 | 只读运行（`read_only: true`），代码与解释器均不可变 |
| 唯一可写边界 | `/data`：`blobs/` 媒体字节、`scratch/` 渲染半成品、`tmp/` 进程管道文件、`reel_loom.sqlite3` WAL 数据库与 `reel_loom.lock` 实例锁 |
| 运行身份 | 非 root 固定账号 `uid/gid=10001`；入口仅在 root 启动时完成卷属主修正后立即降权 |
| 健康判定 | `/loom/v1/health` 只在**真实编码+解码预检**通过后返回 200；starting/draining 返回 503 并带 `phase`。端口能连通不等于可接单 |
| 滚动终止 | SIGTERM → 先翻闸门停止接单（仅保留 GET 观测与 cancel）→ 有界等待渲染/分析线程 → 终止残留 FFmpeg 进程组（SIGTERM 后 SIGKILL 兜底）→ 释放实例锁 |
| 任务正确性 | 终止期间不允许任何未完成渲染被记为成功；半成品只存在于 `scratch/` 生命周期，成功产物与成功状态仍在同一 SQLite 事务登记 |
| 重开恢复 | 新容器挂载同一卷启动时，旧 `running` 单据按既有语义标为 `failed/worker_interrupted`（曾请求取消则为 `cancelled`），由显式 `retry` 复用冻结快照继续交付 |

## 目录

```
Dockerfile                         只读生产镜像（多阶段 wheel 构建）
docker-compose.yml                 单机长期运行编排（固定单副本、只读根、健康检查、停止宽限）
deploy/
  entrypoint.py                    卷准备 + 降权 + exec 服务进程
  Dockerfile.broken-encoder        编码器不可用演练专用变体
  bin/
    01_build.sh                    构建并核验只读根/可写数据卷/非 root
    02_encoder_unavailable.sh      编码器损坏时启动拒绝、端口不接单演练
    03_rolling_replacement.sh      长任务在跑时 SIGTERM 滚动替换与恢复重试全链路
    loom_drill.py                  纯标准库 HTTP/ffmpeg 演练客户端（容器内运行）
    run_tests.sh                   宿主机零依赖生命周期单测入口
  tests/
    test_lifecycle.py              进程登记、排空、SIGKILL 兜底、信号量不泄漏
    test_application_lifecycle.py  阶段闸门、排空顺序、恢复语义、健康 503/200
    stubs/av.py                    无 PyAV 沙箱里的最小导入桩（容器内使用真实 PyAV）
    manual_lifecycle_sim.py        不起容器的端到端信号/闸门模拟器
```

## 可重复构建与运行

前置：Linux + Docker（含 Compose 插件），无需宿主机 Python/FFmpeg。

```bash
# 1. 构建并自检镜像（只读根、可写 /data、非 root）
./deploy/bin/01_build.sh

# 2. 长期运行：固定单副本，命名卷持久化媒体与数据库
docker compose up -d
docker compose ps          # healthy 必须来自预检 200，而非端口探测
curl -s http://127.0.0.1:8729/loom/v1/health | python3 -m json.tool
```

关键环境变量（均有默认值，见 `reel_loom/config.py`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LOOM_DATA` | `/data` | 独占数据目录，必须挂载可写卷 |
| `LOOM_FFMPEG` | `/usr/bin/ffmpeg` | 真实编码器路径；不可用时启动直接失败 |
| `LOOM_WORKERS` | `2` | 编码线程 1–4，容量信号量按此约束 |
| `LOOM_RENDER_TIMEOUT` | `600`（compose） | 单个媒体进程执行期限 |
| `LOOM_SHUTDOWN_TIMEOUT` | `30` | 终止宽限，需小于编排器 stop 宽限 |
| `LOOM_HOST` / `LOOM_PORT` | `0.0.0.0` / `8729` | 监听地址，默认仅映射到宿主回环 |

## 演练一：编码器不可用的启动拒绝

```bash
./deploy/bin/02_encoder_unavailable.sh
```

断言：进程以非零退出；日志含 `encoder_unavailable`；从未打印就绪日志，HTTP 端口不会对外提供服务。预检实际执行一次 libx264/AAC 编码并用 PyAV 全量探测产物，二进制存在但无法编码/解码同样会被拒绝。

## 演练二：滚动替换期间的任务观测与恢复

```bash
./deploy/bin/03_rolling_replacement.sh
```

脚本自动完成：

1. 新容器在同一命名卷上启动，轮询健康直到 `phase=ready`，并回读预检报告；
2. 用容器内 FFmpeg 生成 30 秒色条+音调，完成入库、建卷、版式、渲染入队；
3. 通过 `/health` 的 `active.media_processes` 观测到真实 FFmpeg PID 与渲染进度；
4. `docker stop --time 30` 发送 SIGTERM，校验实际释放时间小于宽限、日志终止报告 `"clean": true`；
5. 同卷重开第二个容器，断言原单据为 `failed + worker_interrupted`（**不是成功**）；
6. 显式 `retry` 后观察状态机走到 `succeeded`，产物登记，媒体与数据库计数跨替换持久化。

终止期间的观测入口（socket 关闭前仍可读）：

- `GET /health` → 503，`context.phase=draining`，`context.active` 含运行单据与媒体进程快照；
- `GET /tickets/{id}` 与 `/tickets/{id}/events` 全程可读；
- `POST /tickets/{id}/cancel` 在排空期仍允许；其余 POST/PUT 一律 503。

## 不依赖宿主机环境的本地校验

没有 Docker 的开发机仍可运行纯标准库生命周期测试（不触媒媒体解码）：

```bash
./deploy/bin/run_tests.sh
python3 -I deploy/tests/manual_lifecycle_sim.py   # 完整信号→闸门→排空模拟
```

`tests/stubs/av.py` 仅在宿主机缺少 PyAV 时用于通过导入，任何真实媒体调用都会显式失败；生产镜像安装的是 `pyproject.toml` 声明的真实 PyAV 与 Debian FFmpeg。
