# Octop 沙箱镜像打包报告：2026.11-base

**核对日期：2026-11-24（Asia/Shanghai）｜用途：交给 ChatGPT 做镜像瘦身与可复现构建评审。** 本报告只调查，不修改镜像、安装脚本或现有沙箱配置。

## 1. 结论：它基于什么打包？

`octop-sandbox-2026.11-base.tar.gz` 是 **基于 Docker Official Image `python:3.12-slim`，在 Debian 13（trixie）、Linux/amd64 上加装文档、媒体、OCR、Node 和编译依赖** 的 Octop 沙箱镜像导出包。默认 Python 为 **3.12.16**。它不是基于 Ubuntu 构建，不是 `octop-python:3.12-slim-cairo-20261123` 的派生镜像，也不是 `2026.11-base-nonroot` 或某个手工装过依赖的运行中容器快照。

- `FROM`：`python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`。归档内 BuildKit/SLSA provenance 明确记录该基础镜像引用；归档前四个 RootFS `diff_id` 也与本机该基础镜像完全一致。
- 导出标签只有 `octop-sandbox:2026.11-base`。宿主还有 `octop-sandbox:2026.11-base-tz`，指向同一镜像；该别名也在构建证明中出现，但不在导出标签列表里。
- 镜像默认 root（镜像配置未指定 `USER`，实测 UID/GID `0:0`），默认 `CMD ["python3"]`，没有自定义 `ENTRYPOINT`。适配器可以为沙箱指定自己的运行命令和网络策略。时区 `Asia/Shanghai`，运行实测 `CST +0800`。

## 2. 交付物、身份与装载

| 项目 | 实测或归档内记录 |
|---|---|
| 外层分发 ZIP | `/data/octop-docker-runtime-adapter.zip`，579,610,993 字节 |
| ZIP 内镜像归档 | `octop-docker-runtime-adapter/images/octop-sandbox-2026.11-base.tar.gz` |
| 镜像归档大小 | 579,537,819 字节，**552.69 MiB**；约占整份 ZIP 的 **99.987%** |
| 归档 SHA-256 | `e56c3df12456d692d7777ebd3862d63faa62920b7f3326782e362df1ae108c14`；直接读取 ZIP 内条目及暂存文件得到同一值，与打包说明一致 |
| OCI 镜像索引摘要（本机 Docker 显示的镜像 ID） | `sha256:15fd52d3a2a28bb48efb00eb25d7c631025d6c49fbc2056c8350c81c29f387ae` |
| Linux/amd64 平台 manifest 摘要 | `sha256:3c38fa3e067dff3a22fbe79f9938304471f1e67d4b23359ac8fe9201ad002f28` |
| 镜像 config 摘要 | `sha256:a8ed63998a55412faa298c8df94b2630e38313c89e269756752b420260ede09f` |
| config 构建时间 | `2026-11-24T04:43:05.601856358Z`（北京时间约 12:43:05） |
| Docker `.Size` | 2,265,557,201 字节（约 2.27 GB），**不是**传输包大小，也不要当作归档层解压总量 |

内层是 Docker 可导入的镜像 TAR（同时有 `manifest.json` 和 OCI 布局，含 1 个 amd64 平台镜像及 1 个 provenance attestation）；其 7 个文件系统层已各自 gzip 压缩。TAR 再套一层 gzip，外层 ZIP 将此 `.tar.gz` 原样存储（ZIP 内压缩前后大小一致）。不是 `docker export` 的单一容器根文件系统。以下是与现有交付格式一致的**重建、导出示例**，未找到当时导出及重打 ZIP 的原始命令；实例中使用的 Docker 为 29.10.0 / buildx 0.39.0，不等于归档已证明由这些具体客户端版本生成。

```bash
cd /data/workspace/docker/octop-sandbox-base
docker build --platform linux/amd64 -t octop-sandbox:2026.11-base .
set -o pipefail
docker save octop-sandbox:2026.11-base | gzip -n > octop-sandbox-2026.11-base.tar.gz
sha256sum octop-sandbox-2026.11-base.tar.gz
# 离线部署（从拆包后的适配器目录执行）
gzip -dc images/octop-sandbox-2026.11-base.tar.gz | docker load
```

分发包内 `setup.sh` 会在本地找不到配置的镜像标签时，尝试装载固定文件名的 `.tar.gz`（也支持 `.tar`），然后检查标签是否存在。本地已有同名标签就跳过导入，该段也没有按预期 digest 检查同名标签。发布优化版时建议新标签、显式核验归档哈希与镜像标识；别只覆盖同名包就假定已部署的沙箱会更新。

## 3. 当前 Dockerfile（工作区源文件，省略注释）

```dockerfile
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libfontconfig1 \
      libgdk-pixbuf-2.0-0 libharfbuzz0b \
      fonts-dejavu-core fonts-noto-cjk fontconfig shared-mime-info \
      ffmpeg poppler-utils ghostscript \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
      nodejs npm \
      git curl wget jq unzip zip ripgrep ca-certificates locales tzdata \
      build-essential pkg-config libcairo2-dev libpango1.0-dev \
      libffi-dev libssl-dev python3-dev \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PIP_CACHE_DIR=/opt/octop-user/pip-cache \
    UV_CACHE_DIR=/opt/octop-user/uv-cache \
    PYTHONUSERBASE=/opt/octop-user/pyuser \
    npm_config_prefix=/opt/octop-user/npm \
    npm_config_cache=/opt/octop-user/npm-cache \
    HF_HOME=/opt/octop-user/hf \
    TORCH_HOME=/opt/octop-user/torch \
    MPLCONFIGDIR=/opt/octop-user/matplotlib \
    PATH=/opt/octop-user/bin:/opt/octop-user/npm/bin:/opt/octop-user/pyuser/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && printf '%s\n' "$TZ" > /etc/timezone \
 && date && date +%Z && cat /etc/timezone

RUN python -c "import ctypes; [ctypes.CDLL(l) for l in ('libcairo.so.2','libpango-1.0.so.0','libpangoft2-1.0.so.0','libfontconfig.so.1','libgdk_pixbuf-2.0.so.0','libharfbuzz.so.0')]; print('C libs OK')" \
 && node --version && npm --version \
 && ffmpeg -version | head -1 \
 && tesseract --version | head -1

LABEL org.opencontainers.image.title="octop-sandbox-base" \
      org.opencontainers.image.description="Broad Debian 13 sandbox base for Octop expert skills" \
      org.opencontainers.image.version="2026.11"
```

`/opt/octop-user` 是适配器挂载的用户共享目录，不是镜像自带的持久卷。原设计是系统级 apt 依赖进镜像；venv、`pip install --user`、`npm install -g`、用户二进制及模型缓存写到挂载目录，保留其跨容器重建的能力。仅设环境变量不代表已安装 UV、PyTorch、CairoSVG、WeasyPrint 或 Manim。实测镜像未内置后三个 Python 模块，也没有 Chromium、LibreOffice、TeX/dvisvgm。对 `--network none` 沙箱，所需 Python/Node 包别指望在运行时联网安装，应提前在允许的环境准备匹配平台和 ABI 的 wheel/依赖包，或用预建变体。手工在容器可写层装的 apt 包不会随重建保留。

## 4. 体积核对和主要来源

| 层（按基础到顶层） | 归档内层 gzip 大小 | 该层解压后 tar 大小 |
|---|---:|---:|
| 基础镜像 4 层合计 | 43,268,722 B（41.26 MiB） | 123,569,664 B（117.85 MiB） |
| Octop 的 `apt-get install` 层 | **540,467,879 B（515.43 MiB）** | **1,444,940,288 B（1,378.00 MiB）** |
| 时区配置层 | 192 B | 3,072 B |
| 构建冒烟层 | 28,989 B | 77,824 B |
| 文件系统层合计 | 583,765,782 B | 1,568,590,848 B |

APT 层占镜像内部压缩文件系统层总量的 **92.58%**。内部层加总与外部归档大小不相等，因为外层还有归档结构及再次压缩。镜像中已无 APT `Packages` 索引和 `.deb` 缓存，且已用 `--no-install-recommends`，重复清理缓存或进一步压缩外层不是主要收益点。

镜像内 `dpkg-query` 统计 865 个带数字 `Installed-Size` 的包记录。以下为热点线索（dpkg 的 KiB 不是 gzip 可节省值；不同能力可能共用依赖，不应直接加总作减重承诺）：

| 包或目录 | 量级与说明 |
|---|---|
| `libllvm19` / `mesa-libgallium` | 126,696 / 41,615 KiB。已查到 `ffmpeg → libavdevice61 → libgl1 → libglx0 → libglx-mesa0 → mesa-libgallium → libllvm19`；也有其他依赖路径，保留现有 Debian FFmpeg 时不能直接删共享库。 |
| `fonts-noto-cjk` | 91,217 KiB；字体目录 `du -sm` 为 109 MiB。可评估是否只需要简体中文，但要测试字体覆盖、字重、fallback 和许可证。 |
| GCC/G++/CPP 与开发库 | `gcc-14-x86-64-linux-gnu` 68,460 KiB，`g++-14-x86-64-linux-gnu` 36,931 KiB，`cpp-14-x86-64-linux-gnu` 34,503 KiB；`libicu-dev` 50,061 KiB；另有 libc/SSL 等开发包。 |
| Node/npm | `libnode115` 53,958 KiB；`/usr/share/nodejs` 为 157 MiB，评估 Debian npm 依赖闭包或独立 Node 变体时应做实测。 |
| Debian Python 3.13 | 镜像默认 `/usr/local/bin/python3` 为 3.12.16，但 `/usr/bin/python3` 另有 3.13.5，`/usr/lib/python3.13` 为 52 MiB；Debian `python3-dev` 给的是 3.13 头文件，而 `/usr/local/bin/python3-config --includes` 指向镜像自带的 3.12 头文件。 |
| `git` | 49,154 KiB；按确实需要的 CLI、兼容性和依赖树取舍。 |

## 5. 现有行为基线

- 本次使用与归档 OCI 索引相同的本地镜像，在 `--network none --read-only` 加 `/tmp` tmpfs 下复测：Cairo、Pango、PangoFT2、Fontconfig、GDK Pixbuf、HarfBuzz、GObject 7 个动态库均可加载；Debian 13 的 `libglib2.0-0t64` 提供 `libgobject-2.0-0`。未对适配器执行重新安装或完整上线重测。
- Python **3.12.16** / pip **25.0.1**；Node **20.19.2** / npm **9.2.0**；FFmpeg **7.1.5**，离线生成并用 ffprobe 确认 H.264 MP4；Poppler **25.03.0**、Ghostscript **10.05.1**，实际用 Ghostscript 生成 PDF，再用 `pdftotext` 读出文本；Tesseract **5.5.0**，启用 `chi_sim`、`eng`、`osd`。
- `fc-match :lang=zh-cn` 匹配到 `Noto Sans CJK SC`；此次刻意使用只读根文件系统，Fontconfig 有 cache 目录不可写警告，但仍匹配成功。如果优化版承诺支持只读 rootfs，应让缓存写入合适的可写用户目录或预热系统缓存，并复测中文渲染。
- 工作区原测试记录另有 CairoSVG -> PNG、WeasyPrint -> PDF、挂载目录中的 Python user install 和 npm 全局安装、适配器 smoke。它们是历史测试记录，不代表这些 Python 包在镜像中预装，也不是本次新跑了适配器 smoke。

## 6. 可以直接交给 ChatGPT 的优化任务

> 基于本报告的实测数据和 Dockerfile，请给出 `runtime`（常用运行依赖）、`builder/dev`（原生 wheel 或 npm addon 编译）及可选 `media/ocr` 重依赖变体的拆分方案，并比较与一个保持旧能力的 `full` 镜像如何并存。输出候选 Dockerfile/构建目标、保留与迁移的 apt 包、离线分发及回滚办法、可运行的验证脚本。不要把未实测的减重当成结果。

建议按下面顺序验证：

1. **检查能否去掉 Debian `python3-dev` 及其他只用于编译的包。** 使用与运行镜像相同的 Python 3.12 基础镜像构建 wheel；验证至少一个原生扩展确实以 CPython 3.12 编译并在运行镜像导入。如果实际依赖 `/usr/bin/python3` 或允许运行期源码构建，保留 `dev/full` 变体。用 `ldd` 和导入测试核对跨阶段复制的 wheel/工具所需共享库，不要复制整套构建环境。
2. **按需要拆分 FFmpeg、Ghostscript、OCR、Node/npm 与 CJK 字体。** 优先用变体隔离 FFmpeg 的大依赖闭包，而非在装完 FFmpeg 后手工删除 Mesa/LLVM；替换 FFmpeg、字体或 npm 来源时，做实际编码、中文 OCR、字体可读性、Node 兼容、许可证及供应链检查。若有跨变体使用场景，仍提供 `full`。
3. **冻结并记录 APT 来源。** `FROM` 已固定 digest，但 `apt-get update` 用构建当日源和未固定版本，不保证下次构建得到相同软件包；建议记录/锁定 Debian snapshot 或受控镜像与 dpkg 清单，保留有计划的安全更新流程、SBOM 和 provenance。优化前后使用同一平台、Docker 导出方式和压缩参数，比较包数、`docker image inspect`、导出字节数与冷机 import 时间。
4. **保持部署契约。** 默认 `linux/amd64`，保留 `/opt/octop-user` 路径、PATH 优先级和 `Asia/Shanghai`；改默认 UID/GID 或 ENTRYPOINT 必须独立验证适配器的命令、bind mount 权限和挂载目录持久性。用新 tag 在测试用户上验证，保留旧 tag 供回滚；既有容器不会随着同名标签更新自动变更镜像。
5. **至少回归这些测试。** 全功能版：7 个 `.so`、CairoSVG PNG、WeasyPrint 中文 PDF、Poppler 抽文本、Ghostscript 转换、FFmpeg 编码/探测、英/中文样例 OCR、用户挂载目录中的 Python 和 npm 安装/复用、时区，以及在干净 Docker on amd64 的离线 `docker load` 和适配器启动。精简版按各变体显式声明支持项，避免删掉必要能力却仍沿用旧标签。

## 7. 证据位置

- [Dockerfile](<docker/octop-sandbox-base/Dockerfile>)、[镜像说明](<docker/octop-sandbox-base/README.md>)、[历史测试记录](<docker/octop-sandbox-base/TEST-RESULTS.txt>)。
- [分发 ZIP](</data/octop-docker-runtime-adapter.zip>)、[暂存镜像归档](</data/zip-stage/octop-docker-runtime-adapter/images/octop-sandbox-2026.11-base.tar.gz>)、[归档说明](</data/zip-stage/octop-docker-runtime-adapter/images/IMAGE.md>)、[安装脚本加载逻辑](</data/zip-stage/octop-docker-runtime-adapter/setup.sh#L106-L120>)。
- 本报告直接核对 ZIP 内镜像归档的 SHA-256，并从对应暂存文件读取 `index.json`、Docker 兼容 manifest、OCI 平台 manifest、config、provenance 及层大小；运行检查使用宿主机已存在的同镜像，未覆盖导入现有标签。
