# 源码依据与版本边界

核对日期：2026-11-23。

1. 原 v1.0 包及部署文档：按 agent 的 ContainerCollection.run 拦截、独立目录挂载、共享 bridge、裸机 Octop、smc/user unit。旧文档中当时 54 容器不是新环境事实。
2. [Harness DockerSandbox 固定提交](https://github.com/TencentCloud/octop-harness/blob/562c8a399f632692046da20ee0a016673f6e9f1a/src/octop_harness/backends/docker_sandbox.py)：原生支持 user；user 命名使用 username；容器内工作区默认与宿主 workspace_dir 同名但不自动 bind；close 不删持久容器，destroy 会删；acquire 失败可能 orphan+create。v2 对受管理 user 替换 acquire/rebind/destroy 逻辑，保留 execute 的每专家 workdir。
3. [Octop manager 固定提交](https://github.com/TencentCloud/Octop/blob/30f4aecd2e14e7eeee639b8c6c33c9f404463df8/src/octop/infra/agents/manager.py)：`_owner_username` 从 agents.user_id 查用户；`_prepare_docker_backend` 注入所有者用户名。v2 在这一已解析边界核对 owner、显式补充 AgentID，并为进程内 backend 构造签名。
4. [docker_spec.py](https://github.com/TencentCloud/Octop/blob/30f4aecd2e14e7eeee639b8c6c33c9f404463df8/src/octop/infra/backend/docker_spec.py)：原生 user 分支只补 username，不同于 agent 分支补 agent_id；原生 `setdefault`/缺省填充不覆盖已有固定 username；裸机模式省略用户指定 volumes。这也是 v1 不能直接改一处标签判断的原因。
5. [probe.py](https://github.com/TencentCloud/Octop/blob/30f4aecd2e14e7eeee639b8c6c33c9f404463df8/src/octop/infra/backend/probe.py)：user 探测可用 test 或显式 username；随后设置 auto_remove=true 并 close。v2 为匹配 prefix 的探测生成 UUID fixed sandbox，避免与真实用户容器同名。
6. [backend 文档](https://github.com/TencentCloud/Octop/blob/30f4aecd2e14e7eeee639b8c6c33c9f404463df8/docs/agent-backend-file-io.md)：内容 backend 与宿主 sessions/memory/checkpoints 路径的区别。
7. [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)：bind 的源属于 daemon 主机，挂载会遮盖容器目标目录旧内容；本包只支持本机 daemon。不能把挂载新根目录等同于迁移旧内容。

通过 GitHub 插件读取固定提交并核对本地测试 wheel 文件 Git blob SHA，一致结果：

| 文件 | Git blob SHA |
|---|---|
| manager.py | e978be2a843412671b37b044a3f627f997896762 |
| docker_spec.py | b1a82e4fa5684126803f48389304e04d4e7034f3 |
| probe.py | 8fa3993552a11b7e3c6db95ae0f1d66de0b3e594 |
| docker_sandbox.py | 02eb150169f6172390fde95eb7f6267d5a3ffc79 |

交付同时固定 `octop_harness/backends/__init__.py` 和 Docker SDK `docker/models/containers.py` 的已测 wheel 源码摘要。六个 SHA-256 见 config/reviewed-source-hashes.json。它们是本版兼容范围，不表示其他版本天然不支持 user；其他版本需审阅适配。

这些链接只用于实现依据；目标服务器的实际文件摘要和端到端验证才决定能否启用本包。未远程连接业务主机。
