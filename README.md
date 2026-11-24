# Octop Docker Runtime Adapter 2.0.0 — 按用户共享容器

> **部署入口**：新机器使用本目录的 `setup.sh`（见 `REPORT.md`）。
> 固定后端名为 `sandbox`，默认镜像 `octop-sandbox:2026.11-base`。
> 如果目录是从其它机器复制来的，先用 `./setup.sh --fresh ...` 归档旧的 config/state/logs。


本版替代 v1.0 的“按专家创建时追加挂载”方案。目标为 **一个 Octop 实例内，一个专家所有者对应一个 Docker 容器；同一用户的多个专家保留各自工作目录**。

交付日期：2026-11-23。仅交付源码、部署脚本及验证脚本；没有连接或部署到业务服务器。先在新建测试用户上按 `docs/DSH-DEPLOY-PROMPT.md` 验证。

## 适用环境

- Linux 裸机 Octop，Python 3.12、Octop 1.0.3b7、octop-harness 1.0.11、Docker SDK 7.2.0，本机普通 rootful Docker daemon。
- 参考服务用户 `smc`、venv `/data/octop/.venv`、systemd **user** unit `octop.service`。这些来自上一版环境记录，现场须复核。
- 新版独立安装目录 `/data/octop-docker-runtime-adapter-v2.0.0`。保留旧 `/data/octop-docker-runtime-adapter` 及旧基线，避免覆盖已部署文件。
- 无额外服务端口、无独立 HTTP API、无新增数据库；使用 Python 标准库及现有 Docker SDK。不要为本包执行 pip 升级。
- 仅支持配置中一个准确的 `sandbox_prefix`。多业务中心分别部署一套配置、状态目录、`instance_id` 和 prefix；它们可共享 `octop-agent-egress` 网络。

## 核心变化

| 项目 | v1.0 | v2.0 |
|---|---|---|
| 管理作用域 | agent | user |
| 容器身份 | AgentID | 实例 + 数据库 user_id，核对 username |
| 挂载源 | 每专家一个目录 | 每用户的 agents 根目录 + shared 目录 |
| 后续新增专家 | 新建容器并挂载 | 原容器内创建子目录，原 mounts 不变 |
| 网络 | 强制统一出口 | allow_network=true 用共享 bridge；false 保持 none |
| 重复使用 | 主要拦截创建 | 创建及复用均检查身份、挂载、资源和网络 |
| 同时首次启动 | Docker 同名冲突向上传递 | 用户级线程锁 + flock，409 后严格验证复用 |
| 错误处理 | fail-open | 受管理 user 后端失败即报错，不退回未挂载容器 |
| 销毁 | 原生行为 | 受管理 user 的 destroy 拒绝执行；close 保留容器 |
| 存量基线 | 固定 54 个专家容器及专家清单 | 实际已有 sandbox 容器快照，支持零容器新环境 |

本版不补丁 Docker SDK 的全局 run/create。通过独立 `sitecustomize.py` 在进程内对 Octop 的所有者解析、DockerSandbox 生命周期和管理员 Docker 探测进行适配；**不改安装目录中的 Octop/Harness 源码**。检查包版本及六个已审阅源码文件 SHA-256，不以修改版本门槛代替兼容性评审。

## 挂载设计

例如实例 `octop-bc01`，Alice 的数据库 user_id 经哈希生成 `u_<32位hex>`，容器叫 `octop_sandbox_bc01_alice`：

| 宿主挂载源 | 容器挂载目标 | 用途 |
|---|---|---|
| `/home/smc/.octop/user-workspaces/octop-bc01/u_<key>/agents` | `/home/smc/.octop/agents` | 只存 Alice 的专家内容文件 |
| `/home/smc/.octop/user-workspaces/octop-bc01/u_<key>/shared` | `/opt/octop-user` | 用户公共 venv、缓存及公共文件 |

专家 A001 的实际内容文件落在源目录 `agents/A001/`，容器内路径仍为 `/home/smc/.octop/agents/A001/`；A002 同理。新增 A003 不改变容器挂载。

宿主原 `/home/smc/.octop/agents/A001/` 继续由 Octop 保存 sessions/checkpoints 等运行数据，**不被挂进沙箱**。这两个“同名绝对路径”位于不同文件系统。MEMORY.md 等具体文件的读写仍遵循安装版 Octop 的 backend/记忆规则，本包不迁移或重写记忆模块。

容器 Config.WorkingDir 设为挂载根；**每次 execute 的 workdir 仍由专家自身 `_workspace_root` 指定**。不要通过 Config.WorkingDir 或 `harness.agent.id` 判断共享容器属于哪个专家。共享容器不设置单个专家的 `harness.agent.id`，使用本包 user_key/instance 标签判断用户归属。

### 工作区隔离的含义

同一用户内仅为目录分开，Shell/Python 能访问同容器其他专家文件、进程和公共依赖。不同用户挂载不同的宿主源目录。共享 bridge 不自动隔离用户间网络，也不是仅允许公网的防火墙。高敏感专家可继续使用原生 agent 后端；v2 不为 agent/fixed 模式新增持久化挂载。

## 后端配置

管理界面 Docker 后端：镜像 `python:3.12-slim`（或已验证内部镜像），**用户名留空**。高级配置：

```json
{
  "sandbox_scope": "user",
  "sandbox_prefix": "octop_sandbox_bc01",
  "allow_network": true,
  "memory": "2g",
  "cpus": 2,
  "pids_limit": 512,
  "command_timeout": 120
}
```

不要配置固定 container_name、sandbox_id、workspace_path、volumes 或 auto_remove=true。所有受管理专家的 workspace_dir 必须是配置的 `container_workspace_root/<AgentID>`。已有自定义工作目录需要单独映射设计，本版会拒绝而不是猜路径。

如果使用原来的 `octop_sandbox`，需要同步修改 `config/adapter.json` 的 sandbox_prefix；只允许在 snapshot 之前修改。示例的 bc01 是部署命名空间，**不是自动迁移开关**。并行新部署应同时使用独立 instance_id、prefix、state_dir 和 workspace_root。不要直接改有数据的现存后端 prefix，否则专家会切到另一容器，旧文件不会自动出现。

同一用户的专家必须使用相同镜像、CPU/内存/PID 限制和 allow_network；否则后创建/复用的专家被拒绝。2GB、2 CPU 是该用户全部专家的合计额度。不同 backend 名称不会自动生成第二个用户容器。需要不同权限/资源边界时采用 agent 范围或明确不同的部署命名空间。

用户名要求 1–64 位 ASCII 字母/数字及 `._-`，首位为字母或数字；不静默使用上游字符清洗，以免两个名称映射成一个容器。中文显示名不受此限制。`agents.user_id=NULL` 的公共专家应使用单独 backend。此处按**专家所有者**共享，不按每次聊天的访问者共享。

## 公共依赖持久化

```bash
python -m venv /opt/octop-user/venvs/default
/opt/octop-user/venvs/default/bin/python -m pip install --cache-dir /opt/octop-user/pip-cache -r requirements.lock
```

同用户其他专家显式调用相同 Python。特殊专家使用自己的工作区 `.venv`。这些 venv 随相应挂载持久化；系统 Python 的 pip、apt 包、容器 `/tmp` 和未挂载 `/root` 仍属于容器层，重建会丢失。公共依赖安装应在用户任务空闲时由维护步骤统一执行，运行时锁仅协调容器获取，不串行所有 Shell，也不保证并发 pip 安装安全。

## 部署入口

1. 按 DSH 文档复核宿主版本、原服务启动链及现存容器。
2. `init-config` 后确认实例、prefix、所有路径，再 `prepare-dirs`、`inventory`、`snapshot`。
3. 创建/复用共享网络、运行本地测试、doctor、真实 Docker smoke。
4. 检查服务重启对旧容器的影响；安装独立 drop-in，在空闲窗口重启 Octop 服务。
5. 两个测试用户、Alice 至少三个专家，验证复用、挂载、网络、文件、依赖、会话及关闭专家行为。

完整命令、存量分支、验收和回滚见 [DSH 部署提示词](docs/DSH-DEPLOY-PROMPT.md)。

## 存量、回收和限制

- 已有用户容器缺少这些挂载时，不自动改名/删除/重建；doctor 会提示冲突。已创建容器不能通过本包热加 bind mount。
- agent→user 改配置不会把旧容器内容合并到新容器；必须单独迁移并核验数据。
- 本包没有自动空闲回收或引用计数清理任务。关闭/删除专家不删用户容器；删除用户也不自动清理数据。用户级回收由运维在确认所有专家/任务退出并备份后执行。
- 失败启动不会把容器重命名为 orphan 再新建；失败创建不会用原参数重试一次。
- 用户改名、迁移专家所有权、换镜像/限额/网络要做维护流程，不能靠第一次启动覆盖旧设置。
- root 容器写出的文件可能归 root，宿主 smc 不能保证直接编辑。保持原生 backend 读写；不要递归 chown 业务目录。
- 仅支持已审阅版本和本机 daemon；未声明支持 rootless、userns-remap、远程 Docker、Windows Docker Desktop、任意 composite 布局。

本地验证结果及真实 Docker 现场验证边界见 [TEST-REPORT.md](docs/TEST-REPORT.md)。
