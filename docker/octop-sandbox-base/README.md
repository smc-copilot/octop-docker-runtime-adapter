# octop-sandbox:2026.11-base

Broad, stable sandbox base for Octop user-scope experts.

- Base: python:3.12-slim, pinned digest
- OS: Debian 13 (trixie)
- Tag: octop-sandbox:2026.11-base
- Image ID: sha256:15fd52d3a2a28bb48efb00eb25d7c631025d6c49fbc2056c8350c81c29f387ae
- Size: ~2.27 GB uncompressed
- Validated on 192.168.99.170 with adapter smoke run e94635c72448

## Included

- Cairo/Pango stack: libcairo2, libpango-1.0-0, libpangoft2-1.0-0,
  libfontconfig1, libgdk-pixbuf-2.0-0, libharfbuzz0b
- Fonts: DejaVu, Noto CJK, fontconfig, shared-mime-info
- Media/docs/OCR: ffmpeg 7.1.5, poppler-utils, ghostscript,
  tesseract 5.5 (+ eng, chi_sim, osd)
- Node 20 + npm 9 (pptxgenjs / web tooling)
- CLI: git, curl, wget, jq, unzip, zip, ripgrep, ca-certificates, locales, tzdata
- Build toolchain: build-essential, pkg-config, libcairo2-dev, libpango1.0-dev,
  libffi-dev, libssl-dev, python3-dev

## Persistent user-space (survives container recreation)

`/opt/octop-user` is the adapter's shared bind mount. The image sets:

    PIP_CACHE_DIR=/opt/octop-user/pip-cache
    UV_CACHE_DIR=/opt/octop-user/uv-cache
    PYTHONUSERBASE=/opt/octop-user/pyuser
    npm_config_prefix=/opt/octop-user/npm
    npm_config_cache=/opt/octop-user/npm-cache
    HF_HOME=/opt/octop-user/hf
    TORCH_HOME=/opt/octop-user/torch
    MPLCONFIGDIR=/opt/octop-user/matplotlib
    PATH=/opt/octop-user/bin:/opt/octop-user/npm/bin:/opt/octop-user/pyuser/bin:...

Recommended usage:

- Python per-user: python -m venv /opt/octop-user/venvs/default
- Python one-off: pip install --user ...
- Node global: npm install -g ...
- Static binaries: /opt/octop-user/bin
- Model/cache data: /opt/octop-user/{hf,torch,pip-cache,npm-cache}

These survive container recreation. `apt-get install` into the container does NOT.

## Extension policy (important)

| New dependency | Where | Image rebuild? |
|---|---|---|
| Python package (wheel or buildable) | shared venv or pip --user | No |
| Node package | npm -g to /opt/octop-user/npm | No |
| Static binary / script | /opt/octop-user/bin | No |
| C library / apt system tool | image | Yes (rolling per-user recreate) |
| TeX Live / Chromium / LibreOffice | separate heavy image or namespace | Yes, but isolated |

Do not bake every Python package into the image. The image is the stable OS
runtime; the shared mount is the mutable tool layer.

## Build

    docker build -t octop-sandbox:2026.11-base .

## Verify

    docker run --rm octop-sandbox:2026.11-base \
      python -c "import ctypes; [ctypes.CDLL(l) for l in ('libcairo.so.2','libpango-1.0.so.0','libpangoft2-1.0.so.0','libfontconfig.so.1','libgdk_pixbuf-2.0.so.0','libharfbuzz.so.0')]; print('OK')"

Adapter smoke:

    cd /data/octop-docker-runtime-adapter
    scripts/smoke.sh --image octop-sandbox:2026.11-base --url https://api.github.com/zen

## Rollout / image change

Existing containers use an old image ID. Changing the backend image is a profile
change; the adapter will not hot-swap it. Per user: stop experts, back up
user-workspaces and state/users record, remove the old user container, update the
backend image (keep user scope, prefix, resources), start experts, verify with
inspect-user. Data on the bind mount is preserved. Do not use docker update.

New hosts: use this image from day one; no conflict.

## Not included

- TeX Live / dvisvgm (manim-video)
- Chromium / Playwright browsers
- LibreOffice

Build a separate heavy image for these, or use a separate namespace/agent-scope
backend so a heavy skill does not force every user onto a multi-GB image.

## Timezone

This image sets `TZ=Asia/Shanghai`, `/etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai`, and `/etc/timezone = Asia/Shanghai`. Existing containers created from an older image keep their old timezone until explicitly recreated.
