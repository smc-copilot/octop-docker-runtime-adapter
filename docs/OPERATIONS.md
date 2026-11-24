# V2 运维说明

## 容器状态与并发

每个用户的线程锁与文件锁覆盖 get→检查→创建/启动→创建工作目录；执行命令前只检查容器与挂载状态，执行期间不持有用户创建锁。可并发执行多个专家，不承诺每用户任务队列、内存公平分配或 Python 安装事务。

进程重启不丢失用户目录；同一实例的多个 worker 必须共享相同 state_dir 和 workspace_root。flock 不覆盖不同服务器或不同锁目录；Docker 409 冲突时仍要验证实例、用户、挂载和配置，绝不因名字相同就接受。

正常 close 只关闭专家的 backend 对象；显式 destroy 对管理的 user 容器返回错误。无自动 stop/remove/GC。只有 smoke 命令清理它自己随机命名空间内的测试容器。

## 用户记录

`state/users/u_<key>.json` 保存实例、控制面 user_id、username、容器名、挂载根、资源配置及镜像 ID。该目录不挂入容器，必须由 smc 拥有、0700。文件 0600。首次创建失败可能留下记录与空目录，重试相同配置可复用；不得为了让不同配置通过而直接删除记录。

`u_<key>` = `u_` + SHA256(JSON([instance_id, str(user_id)])) 前 32 位。它是存储键，不是登录凭证。容器名仍为 `{prefix}_{username}`。此组合既能检测同用户名对应不同数据库用户，也能阻止用户改名后无意创建第二个容器挂同一份数据。

## 诊断

```bash
scripts/adapterctl inventory
scripts/adapterctl doctor
scripts/adapterctl inspect-user --username alice
scripts/adapterctl assert-legacy
```

日志关键事件：`adapter_hooks_registered`、三个目标模块各自的 `hook_installed`、`user_sandbox_created`、`user_sandbox_reused`。阻断事件：`bootstrap_failed`、`user_acquire_blocked`、`shared_user_destroy_blocked`。部署期间最后一种只应来自专用验证；日常若出现应检查谁尝试按专家销毁共享容器。

启动日志必须属于实际 Octop 服务 PID，不能拿 CLI/smoke 的日志当作服务已注入。记录 user_key、AgentID、容器 ID；日志不输出命令、环境变量、模型 Key、完整 inspect、数据库 DSN。

## 常见阻断

| 错误 | 处理 |
|---|---|
| installed_source_differs_from_reviewed_contract | 获取安装版源文件差异，重新审阅；不要直接更新 hash 放行 |
| preexisting_container_requires_explicit_migration | 现存用户容器不自动接管，采用独立新 namespace 试点或另做迁移 |
| configured_username_differs_from_agent_owner | 清除共享 backend 的固定 username，由 Octop 注入所有者 |
| public_or_ownerless_agent_requires_separate_backend | 为公共专家明确所有者，或使用独立 agent/fixed backend |
| user_name_or_profile_changed_requires_explicit_maintenance | 所有者改名/资源/镜像发生变化，检查现有用户记录和运行容器 |
| container_identity_or_profile_mismatch | 同名容器、错误实例、资源或镜像不一致；不按名称强制接管 |
| container_mounts_mismatch / container_network_mismatch | inspect 差异并报告，不自动 network connect 或重建 |
| directory_requires_service_owner_and_0700 | 检查指定目录本身；不要递归改权限或把真实 Runtime 挂进去 |
| user_lock_timeout | 查活跃创建/启动、Docker 响应和锁持有者；不删除正在使用的锁文件 |
| custom_workspace_dir_not_supported_by_user_mount_layout | 自定义目录需另设计映射；当前仅 root/AgentID |

## 资源和镜像升级

修改 backend JSON 不会修改已创建容器。配置冲突时 v2 拒绝复用。升级要以用户为单位安排维护：确认同用户全部专家停止执行，备份挂载与 state/users 记录，记录当前镜像 ID/容器配置，制定保留旧容器及切换步骤，再由管理员执行。包括共享 venv 的 ABI 兼容性验证。包中不提供批量替换或自动删容器命令。

## 回滚

`scripts/rollback.sh` 只撤掉本版 drop-in 并 daemon-reload；当前 Octop 进程仍有适配，直到按原启动链重新启动。不会删挂载、网络、用户记录或容器，不恢复 v1 SDK 补丁。此前 v1 drop-in 的私有备份保存在 state 内。

关闭 v2 后，已存在的同名容器仍带其原 mounts；但后续新建/丢失后重建会回到原生逻辑，可能不带持久化挂载，也没有本版的用户身份/销毁检查。**回滚期间暂停新增 user 容器和用户级维护，不能将“服务能启动”视为持久化仍受保护。** 不建议用 v1 接管 user 容器，因为 v1 只识别 agent。

如需恢复已验证的 v2，只恢复同一包、配置和原基线，重新 doctor、挂载检查、服务空闲窗口再启用。不要重新采集基线来消除差异。
