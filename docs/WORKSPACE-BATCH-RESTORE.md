# 按专家模板批量恢复工作区 ZIP

功能版本：1.0.0。基于用户上传、已经实机验证的 octop-docker-runtime-adapter 现场包增量开发。

## 功能与覆盖语义

1. 按已发布专家模板 ID 查询当前 Octop 实例的用户、专家编号。默认关联 `agents.published_expert_id`；内置模板可显式选择 `template_name`。
2. 使用现有 Runtime.identity 计算用户 key，再核对 `state/users` 记录、Docker daemon、容器标签、profile 和两条实际挂载。
3. 将一个 ZIP 恢复到目标专家目录：同名文件覆盖，缺少的文件/目录创建，ZIP 没有的目标文件保留。包括 `.octop` 隐藏目录和二进制文件，不执行 ZIP 中的脚本。
4. 逐专家完整备份，暂存及校验，再用 Linux `renameat2(RENAME_EXCHANGE)` 原子交换目录。失败返回非零退出码并输出报告；支持批次回滚。

这是工作区文件分发，不复制专家数据库记录，不更改员工所有者、模型/MCP/知识库绑定，不安装 Python 依赖，不复制用户共享目录 `/opt/octop-user`。ZIP 中如包含 USER.md、MEMORY.md、凭据或运行数据，这些文件会按整包要求一起覆盖；请使用适合员工分发的模板 ZIP。

本次保持 runtime `__version__ = 2.0.0` 以及容器标签、哈希、挂载规则不变。新功能版本独立为 1.0.0，避免仅新增命令就让已有容器标签失配。

## 安装到已有现场项目

压缩包顶层仍为 `octop-docker-runtime-adapter/`。新文件为 `scripts/workspace-batch`、四个 `workspace_*.py` 模块、本文和测试。原现场 `setup.sh`、`REPORT.md` 以及已修复的 DockerClient/editable-install 逻辑保留。

先备份项目源码，再将更新包解压覆盖到 `/data`。更新包不包含本机 `config/adapter.json`、`setup.env`、`state/`、`logs/`，不要删除这些现场文件。无需为了新增命令重新运行 setup、重采 baseline 或重建用户容器。

```bash
cd /data
unzip -o octop-docker-runtime-adapter.zip -d /data
cd /data/octop-docker-runtime-adapter
chmod u+x scripts/workspace-batch
```

执行程序使用 `/data/octop/.venv/bin/python`；可用 `OCTOP_DRA_PYTHON` 指定同一 Octop 环境的解释器。需要 Linux、Python 3.12 和原包锁定的 Octop/Harness/Docker SDK 版本。

## 1. 找到“超级助手”的准确模板 ID

在当前服务器，以 Octop 服务用户（例如 smc）执行：

```bash
cd /data/octop-docker-runtime-adapter
./scripts/workspace-batch --octop-home /home/smc/.octop templates
```

返回 `published_expert_id`、`slug`、`name`、`source_agent_id`。使用名字为“超级助手”的那条记录的准确 ID。显示名可重复，工具不按显示名批量写入。

默认从指定 OCTOP_HOME 的 config.json 和 env 读取数据库连接，支持 SQLite 与 PostgreSQL。SQLite 使用只读连接；PostgreSQL 使用只读、可重复读事务；不执行迁移。若数据库配置仅放在 systemd 的环境变量中，运行 CLI 时也必须提供同样配置。可显式指定 SQLite 文件：

```bash
./scripts/workspace-batch --sqlite /home/smc/.octop/octop.db templates
```

不要把密码写进命令行。错误输出不会打印连接串或配置内容。

## 2. 查询用户、专家编号、adapter hash 和目录清单

将下面的 `PUBLISHED_TEMPLATE_ID` 替换为上一步返回的实际 ID。

```bash
./scripts/workspace-batch list \
  --template-id PUBLISHED_TEMPLATE_ID \
  --output /data/super-assistant-targets.json
```

每条目标包括：

| 字段 | 含义 |
|---|---|
| user_id / username | 专家所有者，来自当前 Octop 数据库 |
| agent_id | 员工专家编号，例如 BDZ8X1 |
| user_key | 完整目录 key，例如 `u_<32位hex>` |
| adapter_hash | 去掉 `u_` 的 32 位 hash |
| host_workspace_dir | 已校验的宿主机专家目录 |
| container_workspace_dir | 容器内专家工作目录 |
| container_id / container_name | 实际用户共享容器 |
| mounts | 核对通过的用户目录挂载 |
| ready / error | 是否可恢复、阻塞原因 |

哈希由 `instance_id` 和字符串形式的所有者 `user_id` 计算，不含专家编号；同一用户的多个专家共用一个 hash，最后以 `agents/<agent_id>` 区分。不会使用管理员的登录身份推导员工路径。

已发布模板默认排除源专家。包含启用和停用的匹配专家，但不会修改 enabled 或数据库运行状态。公共/无所有者专家、team/managed 类型、错误 backend、缺失目录、未初始化用户容器等会列为 blocked；任何 blocked 目标都会阻止整份清单执行。

只选择部分专家时，可重复 `--agent-id`：

```bash
./scripts/workspace-batch list \
  --template-id PUBLISHED_TEMPLATE_ID \
  --agent-id BDZ8X1 --agent-id ANOTHER_ID
```

指定的每个编号都必须确实关联该模板。对于 Octop 自带的内置专家模板，使用 `--template-kind builtin --template-id <template_name>`。

## 3. 使用 ZIP 生成执行清单

把“超级助手.zip”放在本机可读、用户挂载目录之外的位置，例如 `/data/超级助手.zip`：

```bash
./scripts/workspace-batch plan \
  --template-id PUBLISHED_TEMPLATE_ID \
  --zip /data/超级助手.zip \
  --output /data/super-assistant-plan.json
```

此命令只读数据库、Docker 和专家工作区，不停止服务、不覆盖专家文件。它流式校验 ZIP 中每个文件，记录 ZIP SHA256、单文件 SHA256、目录映射、容器 ID、profile 与用户记录摘要。

ZIP 内应直接是 `AGENTS.md`、`.octop/`、`scripts/` 等。若确实多了一层 `超级助手/`，重新生成清单时显式加 `--strip-components 1`；不会自动猜测并剥离目录。默认最多 100000 条 ZIP 成员，展开上限 4096 MiB，分别可用 `--max-entries`、`--max-expanded-mib` 调整。

绝对路径、`..`、反斜杠路径、符号链接/特殊文件、加密 ZIP、重复目标路径、文件与目录冲突会拒绝。原目录中有符号链接、硬链接或特殊文件也会拒绝，以免复制时越过当前专家边界。

清单文件必须是新文件；不能手工改目标路径后执行。需要修改范围时，使用新的输出文件重新生成。apply 会重查原清单内的专家，不会悄悄加入清单生成后新建的员工专家。

## 4. 在维护窗口批量执行

建议先对一个测试员工的 BDZ8X1 生成带 `--agent-id BDZ8X1` 的清单，完成真实验收后再处理所有目标。

确认没有需要保留的进行中任务后执行：

```bash
cd /data/octop-docker-runtime-adapter
sudo ./scripts/workspace-batch apply \
  --plan /data/super-assistant-plan.json \
  --stop-runtime
```

使用 sudo 是为了读取、备份和覆盖由容器 root 创建的文件，并保留原 UID/GID/权限；不修改用户根目录的 0700 权限，也不递归 chown 整个 user-workspaces。若所有原文件已由服务用户可读写且能保留属主，也可直接用服务用户运行。

`--stop-runtime` 的实际影响：

- 停止本机 Octop 服务（默认 smc 的 systemd user `octop.service`），因此该实例所有员工会短时不可用。
- 停止清单涉及的用户容器。同一容器中的其他专家也会暂停；不会删除容器、修改镜像或安装包。
- 完成后启动原本运行的容器，再恢复原本运行的 Octop 服务。原本停止的容器/服务不自动启动。
- 不提供业务任务排空或续跑保证；请在新员工初始化阶段或已排空任务的维护窗口操作。

如服务不是默认单元，在生成 plan 之前指定全局参数，例如：

```bash
./scripts/workspace-batch --service-scope system --service-unit octop.service \
  plan --template-id PUBLISHED_TEMPLATE_ID --zip /data/超级助手.zip \
  --output /data/super-assistant-plan-system.json
```

服务设置随 plan 固定，apply/rollback 以该设置为准。工具不会修改 systemd 单元。

也可以由运维手工停止服务及受影响用户容器，然后不传 `--stop-runtime` 执行 apply；此时工具只校验停机条件，不负责启动。

## 提交、备份与报告

每个专家按以下顺序执行：

1. 再次核对当前数据库所有者、模板关联、backend、用户记录、挂载和容器 ID。
2. 保存完整原工作区备份；复制原目录到同文件系统的暂存目录。
3. 在暂存目录覆盖 ZIP 全部文件，保留 ZIP 之外的文件。
4. 校验、fsync 后原子交换暂存目录与专家目录。
5. 验证内容，移除临时目录，记录成功。

成功退出码为 0，阻塞或失败为 2。执行结束输出真实报告路径：

```text
<adapter.state_dir>/workspace-batches/<batch-id>/report.json
```

备份位于同一批次下 `<agent_id>/backup/`，不在任何用户容器挂载内。批次报告逐专家记录状态、备份、前后摘要、失败原因、运行时恢复结果。

提交按专家进行，整个批次不是跨专家事务。某专家失败时会停止后续处理；已经成功的专家保持更新，当前专家若已交换则尝试回滚。需要撤销整批时执行下节 rollback。若无法安全回滚当前专家，会保留停机状态并输出错误，不冒险自动恢复运行。

至少准备“目标原目录完整备份 + 单个专家暂存副本 + ZIP 展开文件”的额外磁盘空间；备份累计保留所有目标的旧内容，工具不自动清理历史批次。Linux 文件系统必须支持原子目录交换，不能以两个非原子 rename 自动降级。

## 5. 回滚与进程中断恢复

使用上一步输出的实际 report.json 路径：

```bash
sudo ./scripts/workspace-batch rollback \
  --report /data/octop-docker-runtime-adapter/state/workspace-batches/BATCH_ID/report.json \
  --stop-runtime
```

原子交换前后均写持久日志。进程被终止时，专家目录保持完整旧版本或完整新版本；可以用同一个 rollback 命令核对并恢复。异常退出可能使服务或容器保持停止，回滚成功后按批次最初的运行状态恢复。

如果恢复后员工又产生了新内容，默认拒绝覆盖回滚。需要回滚时可显式增加 `--overwrite-changed`：工具会先把当前全部内容另行备份到 `before-rollback-*`，再还原旧备份。

容器被删除/重建、所有者/模板/profile/挂载已经变化时，回滚同样拒绝猜测，需先恢复一致的映射。报告和备份应保留，不能复制给其他服务器直接执行。

## Python 依赖与验收

- 工作区内实际包含的 vendor 文件会复制；工作区 ZIP 不包含容器系统 site-packages 或 `/opt/octop-user` 的共享环境。
- Octop 原生导出会跳过 `.venv`、`node_modules`、`.git`、`__pycache__`；缺失的原文件无法从 ZIP 恢复。
- 原生导出不能完整保留原文件执行权限。工具保留目标已有文件的权限；新文件采用 ZIP 中有效普通权限或常规默认权限，不保留 setuid/setgid。需要直接执行的新脚本应核对权限，或使用 `python script.py` / `bash script.sh`。
- 不自动改写脚本中的原专家绝对路径、原用户名或凭据；模板脚本应使用相对路径或当前工作目录。
- 验收：检查员工专家 Skills 和子智能体可见；执行代表性 Python 任务；验证新数据写入自己的 user-workspaces；确认邻接专家目录内容未被覆盖。

本功能的本地测试覆盖真实 SQLite、ZIP、文件系统、原子目录交换和进程强制退出；Docker/systemd 由测试替身验证。尚未在你们的生产服务器执行此新增功能，PostgreSQL 只读连接有契约测试，尚无真实 PostgreSQL 集成验收。

## 常见阻塞

| 错误 | 处理 |
|---|---|
| published_template_id_not_found | 用 templates 查询当前实例准确 ID，不填显示名 |
| backend_profile_differs_from_registered_user | 按原现场报告维护镜像/资源及用户记录的一致性；本工具不修改 profile |
| expert_not_managed_by_this_adapter | 检查 type=docker、scope=user、本机 prefix |
| FileNotFoundError / NotFound | 检查用户记录、专家目录和容器；先让新专家正常初始化 |
| targets_backend_or_mounts_changed_regenerate_plan | 当前归属或运行配置发生变化，重新查询并生成清单 |
| filesystem_atomic_directory_exchange_not_supported | 当前文件系统不支持所需原子交换，停止操作并调整部署 |
| root_required_to_preserve_file_ownership / PermissionError | 在维护窗口使用 sudo 运行，不递归放宽用户根目录权限 |
| runtime_resume_failed | 查看 report 的 runtime_resume_errors 及 systemd/Docker 状态；数据提交状态与启动失败分别记录 |

原 `REPORT.md` 第 8 节是历史现场状态，不代表本次已经替你迁移或修复了那台机器。原 setup 的其他行为也保持原样。
