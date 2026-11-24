# Octop Docker Runtime Adapter 2.0.0 — 部署报告与稳定方案

日期：2026-11-23  
验证主机：`192.168.99.170`（s986170，smc，systemd user `octop.service`）  
安装目录：`/data/octop-docker-runtime-adapter`  
服务用户：`smc`  
部署形态：**每台服务器一个 adapter + 一个 Octop**

---

## 1. 方案摘要

本方案把 Octop 的 Docker 专家后端从“一专家一容器”扩展为：

- **一个专家所有者 = 一个共享 Docker 容器**
- 同一用户的多个专家在容器内使用各自的 `/home/smc/.octop/agents/<AgentID>` 工作目录
- 用户内容持久化在宿主：
  - `.../user-workspaces/<instance>/u_<key>/agents`
  - `.../user-workspaces/<instance>/u_<key>/shared`
- 容器内挂载：
  - `/home/smc/.octop/agents`
  - `/opt/octop-user`
- 公共 Python/Node 工具装在 `/opt/octop-user` 下，容器重建后仍保留
- 管理端固定创建专家：
  ```json
  {"type":"named","name":"sandbox"}
  ```
- 因此每台 Octop 必须有一个固定名为 `sandbox` 的 Docker 存储后端，指向本机 adapter 的 `sandbox_prefix`

已验证版本：

| 组件 | 版本 |
|---|---|
| Python | 3.12.16 |
| Octop | 1.0.3b7 |
| octop-harness | 1.0.11 |
| Docker SDK | 7.2.0 |
| Docker Engine | 29.10.0 |
| 沙箱镜像 | `octop-sandbox:2026.11-base`（root） |

六个已审阅源码 SHA-256 与实际安装源码一致；未修改 Octop / Harness / Docker SDK 源码。

---

## 2. 已验证能力（99.170 测试环境）

以下结果来自 2026-11-23 的实际部署、真实 Docker smoke 和真实专家会话：

| 项目 | 结果 |
|---|---|
| 30 项本地测试 | 30/30 通过，0 failure / 0 error / 0 skip |
| doctor | `ready=true`，instance/prefix 正确 |
| 真实 Docker smoke（`python:3.12-slim`） | `smoke_passed=true`，run `9339fe637155` |
| 真实 Docker smoke（`octop-sandbox:2026.11-base`） | `smoke_passed=true`，run `e94635c72448` |
| 同用户多专家共享容器 | 3 个专家同一完整容器 ID；新增第 3 个不重建、mounts 不变 |
| 不同用户隔离 | 不同容器 ID、不同挂载源、不同 user_key |
| 每专家 pwd | `/home/smc/.octop/agents/<AgentID>` |
| 文件读写 | 真实会话写入容器，宿主 bind 源可见；互不覆盖 |
| 公共 venv | A1 创建 `/opt/octop-user/venvs/default`，A2/A3 import 测试模块 = 42 |
| HTTPS | 用户容器访问 api.github.com 返回 200 |
| close / stop 专家 | 其他专家继续执行，用户容器不被删除 |
| 资源 | 2GB / 2 CPU / 512 PIDs 为用户全部专家合计 |
| 第二次 Octop 重启 | 容器 ID/created 不变，专家 `user_sandbox_reused`，线程历史保留 |
| 技能读取 | 专家读取 `systematic-debugging/SKILL.md` 并返回标题 |

验证时使用的测试专家（后续现场可能已删改，仅作证据）：

| 专家 | AgentID | 用户 | 容器 |
|---|---|---|---|
| DRA-Alice-A1 | DRJS8D | dra-alice | `2830f2f669fe...` |
| DRA-Alice-A2 | M4CFEX | dra-alice | 同一容器 |
| DRA-Alice-A3 | PTYPJE | dra-alice | 同一容器 |
| DRA-Bob-B1 | 22DQVG | dra-bob | `bb19339d7847...` |

---

## 3. 稳定部署配置

### 3.1 adapter 配置

```json
{
  "instance_id": "octop-bc01",
  "sandbox_prefix": "octop_sandbox_bc01",
  "workspace_root": "/home/smc/.octop/user-workspaces/octop-bc01",
  "container_workspace_root": "/home/smc/.octop/agents",
  "state_dir": "/data/octop-docker-runtime-adapter/state",
  "log_file": "/data/octop-docker-runtime-adapter/logs/adapter.jsonl",
  "network_name": "octop-agent-egress",
  "shared_target": "/opt/octop-user",
  "lock_timeout": 30
}
```

注意：

- `instance_id` / `sandbox_prefix` 每台机器唯一，创建后不要改
- `workspace_root` 不能等于或包含 `/home/smc/.octop/agents`
- 每台服务器只跑一套 adapter，不要在同一 Docker daemon 上放多套

### 3.2 固定后端契约

| 项 | 值 |
|---|---|
| 后端名 | **`sandbox`** |
| 类型 | Docker 沙箱 |
| scope | `user` |
| prefix | 本机 `sandbox_prefix` |
| 镜像 | `octop-sandbox:2026.11-base` |
| allow_network | `true` |
| memory | `2g` |
| cpus | `2` |
| pids_limit | `512` |
| command_timeout | `120` |

「高级配置」JSON：

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

**不要**在 JSON 里放 `type`、`username`、`container_name`、`sandbox_id`、`workspace_path`、`volumes`、`auto_remove`。

如果 `memory/cpus/pids_limit/allow_network` 缺失，DockerSandbox 会回落到 `512m / 1 CPU / 256 PIDs / none`，导致与已有 2g 容器 profile 不一致。

### 3.3 镜像

默认 root 版：

```text
octop-sandbox:2026.11-base
```

该镜像已随分发包以 `images/octop-sandbox-2026.11-base.tar.gz` 提供（约 553MB，
sha256 `e56c3df12456d692d7777ebd3862d63faa62920b7f3326782e362df1ae108c14`）。
镜像 ID 为 `sha256:15fd52d3a2a28bb48efb00eb25d7c631025d6c49fbc2056c8350c81c29f387ae`。

镜像已统一时区：

```text
TZ=Asia/Shanghai
/etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai
/etc/timezone = Asia/Shanghai
```

`setup.sh` 在镜像不存在时会自动 `docker load` 这个文件；也可以手工：

```bash
gzip -dc images/octop-sandbox-2026.11-base.tar.gz | docker load
```

包含：

- cairo / pango / pangoft2 / fontconfig / gdk-pixbuf / harfbuzz
- DejaVu + Noto CJK 字体
- ffmpeg 7.1.5、poppler-utils、ghostscript、tesseract 5.5（eng/chi_sim/osd）
- Node 20 / npm 9
- git/curl/wget/jq/unzip/zip/ripgrep
- build-essential、pkg-config、cairo/pango/ffi/ssl/python 开发头文件

持久化用户工具层（在 `/opt/octop-user`）：

```text
PIP_CACHE_DIR=/opt/octop-user/pip-cache
PYTHONUSERBASE=/opt/octop-user/pyuser
npm_config_prefix=/opt/octop-user/npm
npm_config_cache=/opt/octop-user/npm-cache
HF_HOME / TORCH_HOME / MPLCONFIGDIR
```

root 容器写出的文件宿主属 root，复制/覆盖使用 `sudo`。非 root 变体存在，但默认不使用。

---

## 3.4 已有用户 profile pinning（2026-11-24 启用）

问题背景：backend 从 `python:3.12-slim` 改为 `octop-sandbox:2026.11-base` 后，
已有用户容器仍是旧镜像/旧资源；strict 模式会在新增专家时拒绝复用旧容器，返回 500
`container_identity_or_profile_mismatch`。

现在启用 **pinned 模式**：

- drop-in 设置 `OCTOP_DRA_PIN_EXISTING=1`
- 已有容器是权威：adapter 从容器 inspect 读取实际
  `image / image_id / memory / nano_cpus / pids_limit / network`
- 校验容器自身标签、身份、挂载、安全设置；只要自洽就复用
- 新增专家直接在已有容器里创建 `<AgentID>` 目录，不再因为 backend 镜像/资源变化失败
- 没有容器的用户（新用户）仍按当前 backend 创建
- 日志事件：`user_profile_pinned`，包含 user_key / agent_id / container_id 和实际 profile
- 时区：新镜像创建的容器是 `Asia/Shanghai`；旧容器保持原样（UTC），因为 pinned 模式不会重建它们

语义：

| 用户状态 | 行为 |
|---|---|
| 已有容器 | 固定使用该容器的实际镜像/资源，直到显式迁移 |
| 没有容器（新用户） | 使用当前 backend 镜像/资源创建 |
| 想升级某用户 | 停专家 → 备份 → 删除该用户容器 → 归档 `state/users/u_<key>.json` → 再启动/创建 |

因此改 backend 镜像只影响新用户；存量用户不会因为镜像变化被强制重建。

---

---

## 4. setup.sh

`setup.sh` 用于新机器的一键部署。**默认不运行 30 项测试和真实 Docker smoke**，但保留版本/源码 hash、基线、doctor、网络、drop-in、服务加载事件和安全检查。

### 4.1 新机器

```bash
# 解包到固定路径
tar -xzf octop-docker-runtime-adapter-2.0.0-golden.tar.gz -C /data
cd /data/octop-docker-runtime-adapter
chmod u+x setup.sh scripts/*

# 方式 A：CLI
./setup.sh \
  --instance octop-bc02 \
  --prefix octop_sandbox_bc02 \
  --workspace /home/smc/.octop/user-workspaces/octop-bc02

# 方式 B：setup.env（推荐）
cp setup.env.example setup.env
chmod 600 setup.env
vi setup.env
./setup.sh
```

`setup.env` 中的值会被 CLI 覆盖。

### 4.2 setup.sh 做什么

1. 校验服务用户 `smc`、安装路径、Python、Octop/Harness/Docker SDK 版本与六个源码 hash
2. 校验 Docker daemon 和 `octop-sandbox:2026.11-base` 镜像存在；缺失时从
   `images/octop-sandbox-2026.11-base.tar.gz` 自动 `docker load`
3. 读取或创建 `config/adapter.json`
4. 如果是从其它机器复制来的工作树（config/state/baseline 已存在）：
   - 默认拒绝，要求 `--fresh`
   - `--fresh` 会把旧 `config/adapter.json`、`state/`、`logs/` 移到 `state-archive-<ts>/`，不删除容器或用户数据
5. 创建 0700 目录，盘点容器并生成基线（已有基线则校验 daemon/instance/prefix 后复用）
6. 创建/复用共享网络 `octop-agent-egress`
7. 运行 `doctor`
8. 可选 `--with-smoke` 才运行真实 smoke
9. 创建/核对固定后端 `sandbox`（调用 `scripts/ensure-backend.py`）
10. 备份 systemd unit/drop-in/wrapper/env
11. 安装独立 drop-in（不立即重启）
12. 默认重启 `octop.service`，等待 health（默认最多 180s，可用 `--health-timeout` 调整），检查 `adapter_hooks_registered`、`hook_installed`、doctor、assert-legacy
13. 输出管理端可用的 `named:sandbox` 契约

### 4.3 常用选项

```text
--no-restart        只安装 drop-in，不重启
--with-smoke        运行真实 Docker smoke
--fresh             新机器/复制工作树时归档旧 config/state/logs
--no-network        allow_network=false
--image IMAGE       默认 octop-sandbox:2026.11-base
--memory/--cpus/--pids-limit/--command-timeout
--health-timeout N  重启后等待 /api/health 的秒数，默认 180
```

### 4.4 幂等/安全

- 已有 `config/adapter.json` 且 instance/prefix/workspace 一致：复用，不覆盖
- 已有 `state/baseline.json` 且 daemon/instance/prefix 一致：复用，不重采
- 已有 `sandbox` 后端且解析结果一致：复用
- 后端名/prefix/资源不一致：默认拒绝，不静默改写
- 不删除任何业务容器或用户工作区

---

## 5. 管理端契约

管理服务器固定提交：

```json
{"type":"named","name":"sandbox"}
```

因此：

- `sandbox` 缺失 → 创建专家报 `storage backend 'sandbox' not found`
- 改名 → 同上
- `sandbox_prefix` 不是本机 adapter 值 → 专家落到原生容器，**没有 v2 持久化，而且不会报错**
- `sandbox_scope` 不是 `user` → adapter 不接管
- 改动镜像/资源 → 已有容器需要重建，不能热换

部署后必须用：

```bash
OCTOP_HOME=/home/smc/.octop /data/octop/.venv/bin/python \
  scripts/ensure-backend.py --config config/adapter.json --check
```

确认解析结果是 `docker + user + 本机 prefix + 指定镜像/资源`。

---

## 6. 目录结构

```text
/data/octop-docker-runtime-adapter/
  setup.sh                 # 新机器一键部署
  setup.env.example        # 每机参数模板
  REPORT.md                # 本报告
  config/
    adapter.example.json   # 模板
    adapter.json           # 本机生成（0600，不随包分发）
    octop-backend.example.json
    reviewed-source-hashes.json
  deploy/                  # systemd drop-in 模板
  docs/                    # 原包文档
  images/
    octop-sandbox-2026.11-base.tar.gz  # 随包分发的 root 镜像
  octop_docker_runtime_adapter/  # adapter 代码
  scripts/
    adapterctl
    ensure-backend.py      # 固定 sandbox 后端创建/核对
    install-dropin.sh
    rollback.sh
    smoke.sh
    test.sh
  state/                   # 本机运行态（不随包分发）
  logs/                    # 本机日志（不随包分发）
```

**分发时不要带 `state/`、`logs/`、`config/adapter.json`。** 如果已经带了，在新机器上用 `./setup.sh --fresh ...`。

---

## 7. 运维要点

- 容器镜像不能原地替换，镜像/资源/allow_network 变化必须删除重建用户容器
- 重建只影响容器层；用户内容在 `user-workspaces`，公共 venv 在 `shared`，不会丢
- 同一用户的全部专家必须使用相同镜像/资源/allow_network
- 修改后端高级 JSON 时务必展开 UI 的「高级配置」，否则可能丢失资源字段
- 本包没有自动 GC：删除专家/用户不会自动清理共享容器和数据
- 不用 `docker update` 改挂载或镜像
- root 容器写文件属 root，用 `sudo` 复制/覆盖；不要递归改 adapter 检查的 0700 目录权限
- 回滚：
  ```bash
  scripts/rollback.sh
  # 空闲窗口
  systemctl --user restart octop.service
  scripts/adapterctl assert-legacy
  ```
  原 unit/drop-in/wrapper 备份在 `state/private-backups/service-<ts>/`

---

## 8. 99.170 当前现场状态（重要）

验证完成后，现场又进行了若干操作，**当前目录不是可直接复制的干净状态**：

1. 后端已从 `dra-bc01-docker` 改名为 **`sandbox`**，并且当前已正确解析为
   `octop-sandbox:2026.11-base + user + octop_sandbox_bc01 + allow_network=true + 2g/2CPU/512PIDs/120s`。  
   `ensure-backend.py --check` 当前返回 OK。但已有 bc01 容器仍是旧镜像/旧资源，尚未迁移。

2. 旧生产容器 `octop_sandbox_smc-sz-hr21007` 已被删除（对应用户 33/专家已删除），但基线仍包含它：
   ```text
   doctor -> NotFound
   assert-legacy -> legacy_unchanged=false, changed=["missing"]
   ```
   没有重新采基线。

3. 现有 bc01 用户容器仍是旧 profile，和当前后端不一致：
   - `octop_sandbox_bc01_dra-alice`：旧 `python:3.12-slim`，2g/2CPU/512PIDs/egress
   - `octop_sandbox_bc01_scl-sz-hr16002`、`octop_sandbox_bc01_admin`：旧镜像，512m/1CPU/256PIDs/none
   - `octop_sandbox_bc01_smc-sz-hr12256`：新镜像，但仍是 512m/1CPU/256PIDs/none
   - `octop_sandbox_bc01_dra-bob`：旧镜像，2g/2CPU/512PIDs/egress（用户/agent 已删）
   `state/users` 记录也仍是旧 profile；`state/private-backups` 仍保留部分旧备份。

4. 2026-11-24 已启用 pinned 模式（`OCTOP_DRA_PIN_EXISTING=1`）并重启服务：
   - 已有容器全部 `user_profile_pinned` 复用成功，容器没有被重建
   - `scl-sz-hr16002` 的 8b9f... 仍为 python:3.12-slim / 512m/1/256/none，但新增专家可以正常复用它
   - 失败留下的半成品 agent `JNGEG4` 已删除
   - 新用户仍按当前 backend 创建 `octop-sandbox:2026.11-base + 2g/2/512/egress`，且新镜像使用 `Asia/Shanghai`
   - 旧容器保持 UTC（未重建），符合“旧容器不动”
   - `doctor` 仍因旧基线缺少 `octop_sandbox_smc-sz-hr21007` 而失败；这与 pinned 模式无关，需要单独决定是否重新采基线

**结论**：99.170 的“方案”已验证，但当前运行态需要一次维护迁移（停服务 → 备份 → 归档旧 profile → 删除旧用户容器 → 统一 sandbox 镜像/资源 → 启动重建）才能与稳定配置一致。不要把当前 `state/`、`baseline.json`、`config/adapter.json` 复制到其它机器。

---

## 9. 证据与复核命令

```bash
cd /data/octop-docker-runtime-adapter
scripts/adapterctl doctor
scripts/adapterctl assert-legacy
scripts/adapterctl inventory
systemctl --user status octop.service --no-pager
journalctl --user -u octop.service --since '-10 min' --no-pager | grep -E 'adapter_hooks_registered|hook_installed'
OCTOP_HOME=/home/smc/.octop /data/octop/.venv/bin/python \
  scripts/ensure-backend.py --config config/adapter.json --check
```

服务加载事件必须来自真实 Octop 服务 PID，包含：

- `adapter_hooks_registered`，version=2.0.0，instance/prefix 正确
- `hook_installed`：`octop.infra.backend.probe`、`octop.infra.agents.manager`、`octop_harness.backends.docker_sandbox`
- pinned 模式下还会出现 `user_profile_pinned`，记录实际容器 profile

---

## 10. 结论

- 方案在 99.170 完成真实 Docker + 真实专家验收
- 新机器使用 `setup.sh` 完成安装，默认跳过测试/smoke，但保留版本/hash/基线/doctor/服务加载检查
- 固定后端名 `sandbox`，固定管理端契约
- 默认 root 镜像 `octop-sandbox:2026.11-base`
- 99.170 当前运行态仍需一次维护迁移；迁移完成前不要重启 Octop，也不要把该工作树当作干净模板分发
