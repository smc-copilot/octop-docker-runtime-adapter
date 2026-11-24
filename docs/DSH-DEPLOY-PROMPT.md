# DSH 部署提示词：Octop Docker Runtime Adapter 2.0.0（user scope）

请在本次指定的 Octop 服务器部署 `octop-docker-runtime-adapter-v2.0.0.zip`，实现一个专家所有者一个共享 Docker 容器、每专家独立工作目录、用户工作区与公共 venv 持久化。完整阅读包内 README、本文、OPERATIONS 和测试报告后执行，不要只给建议。

原环境参考：`192.168.99.171` / `https://ai.superic.com:8092`，smc 用户，裸机 Octop，`/data/octop/.venv/bin/python`，systemd **user** `octop.service`，原包装脚本 `/home/smc/.local/bin/octop-docker-run.sh` 经 `sg docker` 启动，内部 8088。若本次是新服务器，以已指定的新主机为准；不要凭此旧记录去操作其他机器。

## 1. 任务边界

- 按用户共享已经由业务选择；无需再次讨论 agent 与 user 是否可行。
- 不恢复 `OCTOP_IN_CONTAINER=1`；不改 Octop/Harness/Docker SDK 安装文件；不升级软件包。
- 无独立 Adapter API/Compose 服务/新增端口，不改 Nginx、Docker daemon 配置、防火墙或 DNS。
- 保留所有已有容器及数据；不批量 rm/prune，不迁移或“修复”旧容器，不对旧容器 network connect/disconnect。
- 本次代码不自动迁移已有用户/专家容器。仅随机 smoke 容器可以由 smoke 脚本删除。真实测试用户/专家留下供人工检查。
- 暂停新建专家配置变更窗口，等在途任务自然完成后做必要的 Octop 主服务重启；不要停止整个 Docker daemon。
- 不回显凭证、全量环境变量、Config.Env 或模型 Key。配置备份只保存在 smc 可读的私有目录。

## 2. 确认当前环境，选择正确分支

只读复核 Python/Octop/Harness/Docker SDK 版本、服务用户、OCTOP_HOME、专家 workspace_dir、Docker daemon、安全选项、原 user unit/drop-in、ExecStart/sg 链及 PYTHONPATH/EnvironmentFile。

支持版本：Python 3.12 / Octop 1.0.3b7 / octop-harness 1.0.11 / Docker SDK 7.2.0。任一版本或文件摘要不同，需要先报告差异，不自行改 `compat.py`、hash 文件或重新生成基线放行。

| 当前情况 | 实施方式 |
|---|---|
| 新服务器，没有业务 sandbox | 采用 user 模式和新的 prefix，基线允许 0 个容器 |
| 仍有原 agent 模式容器 | 保留；为新测试用户/新专家创建独立 user 后端。不要把旧专家批量改成新 backend |
| 已经创建了 user 模式容器 | 先 inspect 它们的挂载。不能把换 JSON 当成已完成持久化；同 prefix 的旧容器会阻断 doctor |
| 已部署 v2 后重复执行 | 复用原 config/state/baseline，禁止重采基线；先 doctor 和 inspect-user |

旧文档的“54 个容器”是当时环境记录，本版不再硬编码 54。以本次真实盘点数量、状态为基线；不要为了凑数创建/删除容器。

如果已有 user 容器，但需要先验证新包，可选择一个**全新 namespace 和测试用户/新专家**，新 namespace 应同时使用新的 instance_id、prefix、独立的 state_dir 和 workspace_root，保持旧专家 backend 引用不变。若目标是直接替换已有生产用户容器，完成下面非破坏性准备后交付具体迁移清单，不能把试点成功说成存量迁移完成。后文第 9 节给出迁移要求。

## 3. 安装代码并配置

保留 `/data/octop-docker-runtime-adapter` v1 目录。将新 ZIP 解压至：

```text
/data/octop-docker-runtime-adapter-v2.0.0/
```

ZIP 文件直接位于根，无额外同名层。以 smc 身份执行；需要 Docker 组权限时沿用已有 sg docker 方式，不创建 root-owned 配置。

```bash
cd /data/octop-docker-runtime-adapter-v2.0.0
sha256sum -c SHA256SUMS
chmod u+x scripts/*
scripts/adapterctl init-config
```

如 config/adapter.json 已存在，init-config 会拒绝覆盖；读取并核对它，不重复初始化。

编辑 `config/adapter.json`，确认以下项：

```json
{
  "instance_id": "octop-bc01",
  "sandbox_prefix": "octop_sandbox_bc01",
  "workspace_root": "/home/smc/.octop/user-workspaces/octop-bc01",
  "container_workspace_root": "/home/smc/.octop/agents",
  "state_dir": "/data/octop-docker-runtime-adapter-v2.0.0/state",
  "log_file": "/data/octop-docker-runtime-adapter-v2.0.0/logs/adapter.jsonl",
  "network_name": "octop-agent-egress",
  "shared_target": "/opt/octop-user",
  "lock_timeout": 30
}
```

- `instance_id` 在各业务中心唯一；`sandbox_prefix` 在同 Docker daemon 上唯一，并与稍后 Octop 后端精确一致。
- 若继续使用 `octop_sandbox`，先确认该 prefix 下不存在需要保留的 user 容器，且不与其他实例冲突。
- `workspace_root` 是新增内容持久目录；不能等于或包含宿主 Runtime `/home/smc/.octop/agents`，也不能挂整个 `.octop`。
- 若实际服务用户、安装目录或启动命令不同，先调整全部关联模板并记录改动。包内 systemd 脚本默认只接受已审阅的 smc/路径，不能盲目执行。

```bash
scripts/adapterctl prepare-dirs
scripts/adapterctl inventory
scripts/adapterctl snapshot
scripts/adapterctl assert-legacy
```

`prepare-dirs` 创建私有目录；已有目录权限不合格时拒绝。不递归 chmod/chown。`snapshot` 记录当时所有带 sandbox 标签的容器，包含 agent、user、fixed 和 stopped 状态，以及 daemon/版本/配置摘要，不读取业务文件或写业务数据库。创建后不再修改配置；配置变化会使基线检查失败。

## 4. 网络与部署前验证

先检查宿主路由、已有 Docker 网段和镜像，确认 Docker bridge 自动分配地址不会与业务网络重叠。默认镜像 `python:3.12-slim` 必须已在本机；正式生产可使用另经验证的内部基础镜像。

```bash
scripts/adapterctl ensure-network
scripts/test.sh
scripts/adapterctl doctor
scripts/smoke.sh
scripts/adapterctl assert-legacy
```

网络存在则验证复用，不重建；不存在只创建一个共享 local bridge。测试不得 skip，doctor 必须 `ready=true`。当旧 user 容器与当前 prefix 冲突时不继续注入。

`smoke.sh` 在随机 namespace 创建 Alice 两专家及 Bob 一专家，检查并发首次启动、同用户共享/不同用户分开、后来新增第三专家无需重建、不同 cwd、跨用户文件不可见、公共 venv 可被同用户复用、HTTPS、close 不删容器、destroy 被拒绝；然后仅删除该随机测试用户容器以证明工作区和 shared venv 重建后保留。结束清理自己创建的测试容器，保留测试工作区及记录。其他容器不动。

公网测试默认 `https://api.github.com/zen`；企业网络需要时改用批准的无鉴权 HTTPS 站点：

```bash
scripts/smoke.sh --url https://已批准测试站点
```

不要关闭 TLS 校验或把 HTTPS 失败当成通过。管理员“测试可用性”的随机固定容器只测试原生连通性，不代表本 Adapter 的绑定挂载、公共 venv 或真实专家已验收。

## 5. 检查启动链并启用

先备份本次涉及的 user unit/drop-in/启动包装到 state 下私有备份目录，不输出内容。检查 EnvironmentFile 是否覆盖 PYTHONPATH。最终必须加载本版且只加载本版：不能把 v1/v2 两套同名 Python 包同时置于 PYTHONPATH，也不能丢失原有自定义 sitecustomize 行为。

本包使用 sitecustomize，仅当 `OCTOP_DRA_ENABLED=1` 且真实 Python 入口 argv 是 `octop ... run ...` 才启用。若服务实际以 `python -m ...` 或其他包装入口启动，应先适配明确的启动检查；不能因为 unit 显示 active 就认为已生效。

复核重启 Octop 对旧专家的原生启动行为。已审阅版本会恢复 enabled 且 last_state 非 stopped 的专家，可能启动当前 exited 的旧容器。把它们的容器状态和控制面 enabled/last_state 交叉核对；若会违反“旧容器不动”，停在未激活状态并报告对应 ExpertID/调用点，不私自改旧专家状态绕过。

确认窗口内没有正在执行的任务后：

```bash
scripts/install-dropin.sh
systemctl --user restart octop.service
systemctl --user is-active octop.service
scripts/adapterctl assert-legacy
```

安装脚本不自行重启。它只安装同名独立 drop-in；若当前文件与随包的 v1 模板完全一致，会备份后替换为 v2；自定义文件需先评审合并，不能覆盖。保留 sg docker 的原有 ExecStart。重启只针对 Octop user service。

本版 bootstrap 配置/版本检查失败会终止该 Octop 启动进程（78），不默默继续运行未适配沙箱。若服务配置 Restart=always 出现循环，先撤掉本包 drop-in、检查 doctor 和启动环境，遵照第 8 节恢复；不要改成 fail-open 掩盖问题。

真实 Octop 服务 PID 的启动日志需要出现：

- `adapter_hooks_registered`，version=2.0.0、正确 instance/prefix；
- `octop.infra.agents.manager` 的 `hook_installed`；
- 首次加载 Docker 后的 `octop_harness.backends.docker_sandbox` 的 `hook_installed`；
- 调用后台测试时 `octop.infra.backend.probe` 的 `hook_installed`。

按实际加载顺序出现，不能拿 smoke PID 的日志替代。任何 bootstrap_failed、user_acquire_blocked 都需说明原因。不要把缺少加载事件解释为“可能已生效”。

## 6. Octop 后台配置及真实专家验收

创建或选择一个**专用于本次新测试专家**的 Docker storage backend；不要改变旧专家正在引用的共享配置。

- 镜像：与 smoke 验证的一致。
- sandbox_scope：user。
- sandbox_prefix：与 adapter.json 一致，例如 octop_sandbox_bc01。
- 用户名（可选覆盖）：留空。
- allow_network=true，memory=2g，cpus=2，pids_limit=512。
- 不填固定 container_name、sandbox_id、workspace_path、volumes；auto_remove 不启用。

完整直接 backend JSON 样例在 `config/octop-backend.example.json`；named 存储后端管理页应使用相应字段，不要把 UI 不接收的 type 等整段强塞进输入框。

使用合法管理员/用户会话创建两个明确标注验收用途的测试用户，用户名使用合法 ASCII 标识。创建 Alice-A1、Alice-A2，稍后新增 Alice-A3；Bob-B1。专家必须有真实 agents.user_id。公共 expert 与访问者共享的业务规则不在本次变更中。

验收并记录：

| 检查 | 必须结果 |
|---|---|
| Alice 三个专家 | 同一个完整容器 ID；新增 A3 不重建/改 mounts |
| Bob | 与 Alice 容器 ID、挂载源、user_key 不同 |
| pwd | 各专家自己的 `/home/smc/.octop/agents/<AgentID>` |
| 文件工具 | 各写入不同内容，读取与文件树一致，宿主对应 user-workspaces 下可见 |
| 真实会话 | 技能读取、文件读写、一轮真实对话正常 |
| 公共 venv | 显式调用 `/opt/octop-user/venvs/default/bin/python`，同用户专家可 import 测试模块 |
| 不同用户 | Bob 不得读取 Alice 测试文件；不误把共享 bridge 当网络隔离 |
| 网络 | allow_network=true 的用户容器在唯一共享网络并可 HTTPS |
| 关闭 A1 | A2/A3 会话与执行继续正常；用户容器不被删除 |
| 资源 | 2GB/2 CPU/512 PIDs 是用户合计，inspect 与配置一致 |
| 存量 | assert-legacy 通过，或准确解释与任务无关的业务状态变化；不“修复”差异 |

```bash
scripts/adapterctl inspect-user --username 实际测试用户名
scripts/adapterctl assert-legacy
```

容器 ID 请通过 backend 与用户标签确认，不用共享容器的 `harness.agent.id` 推导全部专家。真实测试专家和容器保留；重建持久性以专用 smoke 已验证结果为依据，不为验收删除真实专家容器。

## 7. 同用户配置约束和回收

同用户不同专家不得使用不同镜像/配额/allow_network 并期待仍是一容器；v2 会拒绝不一致配置。特殊专家需要 agent 独立容器时另选原生 backend；本包只增强 user 模式。

系统 pip/apt、/tmp 不自动持久化。公共依赖放 shared venv，特殊依赖放专家自己的 .venv；安装过程需协调，不能同时多专家修改同一个 venv。

禁止按“最后一个 Python wrapper 已关闭”自动删除用户容器；任务可能仍在执行或其他 worker 仍持有句柄。此包无自动 GC。用户级回收需确认所有专家/任务停止、备份并经过维护流程，不能由删除一个专家顺带销毁。

## 8. 失败与回滚

若还没安装 drop-in，无需重启回滚。已装但服务未重启时，撤掉 drop-in 即可保持原进程。

```bash
scripts/rollback.sh
# 仅在任务空闲且旧容器启动行为允许的窗口：
systemctl --user restart octop.service
scripts/adapterctl assert-legacy
```

脚本只禁用确认为 v2 的 drop-in，不自动恢复 v1，也不删除任何用户数据、容器、网络。配置错误导致启动失败时，在同一次部署维护窗口按原服务启动链恢复；不要因此重启 Docker 或改旧容器。

**回滚到原生逻辑后暂停新 user 容器创建及容器重建**，否则新容器可能没有持久化挂载。旧容器已有 mounts 不受 drop-in 删除影响，但身份/销毁保护已不生效。保留 v2 原配置、state、基线和日志供恢复。

## 9. 已有 user 容器的数据迁移要求（本次不自动执行）

如果已改为 user 且容器内有正式数据，不能在已有容器上用 docker update 追加 bind。也不能把新 prefix 的空容器当成迁移成功。先输出逐用户清单：控制面 user_id/username、所有专家 ID/真实 workspace_dir、原容器完整 ID/镜像/资源/网络、内容目录、系统依赖和活动任务。

迁移需要明确的维护方案：停止该用户全部任务写入，备份并核验每个专家内容目录；排除宿主 sessions/checkpoints 与内容重复混拷；如需保留系统 pip/apt，应先固化镜像或导出可复现依赖；把内容还原到新用户 agents 子目录，并把需持久化公共环境放 shared；保留原容器；用新 namespace/已审阅的用户记录方案切换后端；逐专家校验文件数量/摘要、技能、记忆路径、会话和权限。必须给出失败时回到原 backend/原容器的具体路径。

本包不包含自动导出、关停、改名或删除存量容器的迁移脚本；不能为跳过 baseline 冲突而删基线。若确定是无数据测试环境，仍需明确哪些容器可丢弃后才执行单独清理，不混进默认部署脚本。

## 10. 最终交付报告

一次输出以下结果：实际主机与版本、实例/prefix、旧容器基线数量、版本/源码匹配、30 项本地测试、doctor/smoke 结果、共享网络与子网、服务 PID/启动时间/加载事件、两个用户及四个专家容器对应关系、挂载源/目标、文件与 venv/HTTPS/会话结果、所有改动文件和回滚位置。

明确区分：已验证、未验证、是否只完成新用户试点、是否仍需存量迁移。缺少管理员会话或真实 Docker 联调时如实列出，不声称部署全部完成。
