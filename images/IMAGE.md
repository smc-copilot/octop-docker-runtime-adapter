# Bundled Docker image

Tag: `octop-sandbox:2026.11-base` (TZ=Asia/Shanghai)
Image ID: `sha256:15fd52d3a2a28bb48efb00eb25d7c631025d6c49fbc2056c8350c81c29f387ae`

Archive: `octop-sandbox-2026.11-base.tar.gz`
SHA-256: `e56c3df12456d692d7777ebd3862d63faa62920b7f3326782e362df1ae108c14`

The image sets:

- `TZ=Asia/Shanghai`
- `/etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai`
- `/etc/timezone = Asia/Shanghai`

Load:

```bash
gzip -dc images/octop-sandbox-2026.11-base.tar.gz | docker load
```

`setup.sh` loads this automatically when the image is missing.
