# V2.0.0 验证记录

日期：2026-11-23。当前工作环境 Python 3.12.14；隔离依赖 Octop 1.0.3b7、octop-harness 1.0.11、Docker SDK 7.2.0。没有安装或升级业务服务器依赖。

## 已执行

- 30 个自动化测试全部通过，0 failure、0 error、0 skip。
- Python 文件 compileall 通过；所有交付 shell 脚本语法检查通过。
- GitHub 固定提交四个关键文件 blob SHA 与测试 wheel 相同；六个部署校验文件 SHA-256 固定。

测试采用安装版 Harness **完整 DockerSandbox 类源码**执行，协议响应基类与 Docker daemon 使用替身；实际构造、execute 工作目录、close、destroy、rebind 代码运行。另执行实际 `_build_docker` 函数，验证内部所有者绑定和 workspace_dir 能到达构造器。Docker SDK HostConfig/NetworkMode/Binds 使用真实 7.2.0 转换，仅 daemon HTTP 操作替换。

| 覆盖 | 结果 |
|---|---|
| 两专家同用户共享；不同用户独立；后来第三专家不重建 | 通过 |
| 每专家 execute workdir 不同，挂载目标不随首个专家改变 | 通过 |
| 8 线程首次获取只创建一次、409 后身份检查复用 | 通过 |
| 独立进程 flock 超时与释放后获取 | 通过 |
| close 保留、destroy 阻断、失败启动不 rename/recreate | 通过 |
| 容器丢失后的重新获取保持相同挂载源 | 通过（假 daemon） |
| 网络/镜像/CPU/内存/PID 差异拒绝，allow_network=false 不被覆盖 | 通过 |
| 错误 mounts、身份冲突、旧基线同名对象拒绝且不启动旧容器 | 通过 |
| 用户名规范、无 owner、固定用户名、伪造绑定、自定义目录限制 | 通过 |
| 用户改名保护、宿主 source symlink 拒绝 | 通过 |
| admin probe 随机独立名字、agent 原生生命周期透传 | 通过 |
| 真 Docker SDK 参数转换、lazy import hook、真实 sitecustomize 子进程启动 | 通过 |
| 空环境基线、不可覆盖基线、配置漂移检测、错误配置退出78 | 通过 |

完整输出：本包 docs/test-output.txt。

## 未执行，须由 DSH 现场执行

- 实际 Docker Engine 的镜像启动、bind mount 权限、DNS/HTTPS 和跨用户文件访问验证。
- 删除专用 smoke 容器后的真实数据及公共 venv 持久性。
- systemd user unit/sg docker 的真实注入和启动链。
- 完整 Octop 控制面所有者解析→named backend→真实专家→文件工具/技能→会话的端到端测试。
- 已有用户或专家容器迁移（本包不自动执行）。

当前工作环境没有 Docker CLI，`/var/run/docker.sock` 不可用，因此没有声称真实 Docker smoke 已通过。30 项本地检查不是生产验收。

现场应先执行 scripts/test.sh、doctor、scripts/smoke.sh，再按 DSH 提示词以两个测试用户和四个专家完成真实验收。禁止以代码编译或页面能访问替代这些检查。
