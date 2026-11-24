#!/bin/sh
# Octop Docker Runtime Adapter 2.0.0 - one-shot deployment for a new host.
#
# Run as the Octop service user (smc), from /data/octop-docker-runtime-adapter.
# This script does NOT run the 30-test suite or the real-docker smoke test by
# default. It still validates versions/source hashes, creates/validates the
# baseline, checks doctor, installs the drop-in and restarts the user service.
#
# Typical new-host use:
#   ./setup.sh --instance octop-bc02 --prefix octop_sandbox_bc02 \
#              --workspace /home/smc/.octop/user-workspaces/octop-bc02
#
# If this directory was copied from another host (config/state/baseline exist),
# use --fresh to archive the host-specific state first. --fresh never deletes
# containers or user workspaces.
set -eu
umask 077

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ADAPTER_PY=${OCTOP_PY:-/data/octop/.venv/bin/python}
OCTOP_HOME=${OCTOP_HOME:-/home/smc/.octop}
export OCTOP_DRA_CONFIG="$ROOT/config/adapter.json"
BACKEND_NAME=sandbox
BACKEND_IMAGE=${BACKEND_IMAGE:-octop-sandbox:2026.11-base}
MEMORY=${MEMORY:-2g}
CPUS=${CPUS:-2}
PIDS_LIMIT=${PIDS_LIMIT:-512}
COMMAND_TIMEOUT=${COMMAND_TIMEOUT:-120}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}
SMOKE_URL=${SMOKE_URL:-https://api.github.com/zen}
INSTANCE=${INSTANCE_ID:-}
PREFIX=${SANDBOX_PREFIX:-}
WORKSPACE=${WORKSPACE_ROOT:-}
DO_RESTART=1
WITH_SMOKE=0
FRESH=0
NO_NETWORK=0

usage() {
    cat <<'EOF'
Usage: ./setup.sh --instance ID --prefix PREFIX --workspace DIR [options]

Options:
  --instance ID        adapter instance_id (unique per Octop/business center)
  --prefix PREFIX      sandbox_prefix (unique on this Docker daemon)
  --workspace DIR      persistent user content root
  --image IMAGE        Docker image (default octop-sandbox:2026.11-base)
  --memory SIZE        default 2g
  --cpus N             default 2
  --pids-limit N       default 512
  --command-timeout N  default 120
  --health-timeout N   seconds to wait for /api/health after restart (default 180)
  --no-network         allow_network=false (default true)
  --no-restart         install drop-in but do not restart Octop
  --with-smoke         run the real Docker smoke (skipped by default)
  --fresh              archive existing config/state/logs before starting
  -h, --help           show this help

A setup.env file in this directory is sourced first; CLI flags override it.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[setup] $*"; }

# Optional per-host defaults. Never put secrets here.
if [ -f "$ROOT/setup.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/setup.env"
    set +a
    : "${INSTANCE:=${INSTANCE_ID:-}}"
    : "${PREFIX:=${SANDBOX_PREFIX:-}}"
    : "${WORKSPACE:=${WORKSPACE_ROOT:-}}"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --instance) INSTANCE=$2; shift 2 ;;
        --prefix) PREFIX=$2; shift 2 ;;
        --workspace) WORKSPACE=$2; shift 2 ;;
        --image) BACKEND_IMAGE=$2; shift 2 ;;
        --memory) MEMORY=$2; shift 2 ;;
        --cpus) CPUS=$2; shift 2 ;;
        --pids-limit) PIDS_LIMIT=$2; shift 2 ;;
        --command-timeout) COMMAND_TIMEOUT=$2; shift 2 ;;
        --health-timeout) HEALTH_TIMEOUT=$2; shift 2 ;;
        --no-network) NO_NETWORK=1; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        --with-smoke) WITH_SMOKE=1; shift ;;
        --fresh) FRESH=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (use --help)" ;;
    esac
done

[ "$ROOT" = /data/octop-docker-runtime-adapter ] || die "install directory must be /data/octop-docker-runtime-adapter (got $ROOT)"
[ "$(id -un)" = smc ] || die "run as the Octop service user smc"
[ -x "$ADAPTER_PY" ] || die "Octop python not found: $ADAPTER_PY"
[ -x "$ROOT/scripts/adapterctl" ] || die "adapterctl missing"
[ -f "$ROOT/scripts/ensure-backend.py" ] || die "scripts/ensure-backend.py missing"

log "validating versions and reviewed source hashes"
PYTHONPATH="$ROOT" OCTOP_DRA_ENABLED=0 "$ADAPTER_PY" - <<'PY' || die "version/source contract check failed"
from octop_docker_runtime_adapter.compat import evidence
print(evidence()["versions"])
PY

docker info >/dev/null 2>&1 || die "cannot access local Docker daemon; ensure smc can use docker (or run via the existing sg docker path)"

# If the bundled image is present and the image is missing, load it.
if ! docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1; then
    for f in "$ROOT/images/octop-sandbox-2026.11-base.tar.gz" "$ROOT/images/octop-sandbox-2026.11-base.tar"; do
        [ -f "$f" ] || continue
        log "loading sandbox image from $f"
        case "$f" in
            *.gz) gzip -dc "$f" | docker load ;;
            *) docker load -i "$f" ;;
        esac
        break
    done
fi
docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1 || die "Docker image not present: $BACKEND_IMAGE (bundled image missing or load failed)"

if [ "$FRESH" = 1 ]; then
    if [ -e config/adapter.json ] || [ -e state ] || [ -e logs ]; then
        ARCHIVE="$ROOT/state-archive-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        mkdir -p "$ARCHIVE"
        chmod 700 "$ARCHIVE"
        log "archiving existing host state to $ARCHIVE"
        for p in config/adapter.json state logs; do
            [ -e "$p" ] && mv "$p" "$ARCHIVE/$(basename "$p")"
        done
        chmod -R go-rwx "$ARCHIVE" || true
    fi
fi

if [ -f config/adapter.json ]; then
    log "existing config/adapter.json found; validating"
    VALUES=$(PYTHONPATH="$ROOT" "$ADAPTER_PY" - <<'PY'
import json
c=json.load(open("config/adapter.json"))
print("|".join([c["instance_id"],c["sandbox_prefix"],c["workspace_root"],c["state_dir"],c["log_file"]]))
PY
)
    OLD_IFS=$IFS; IFS='|'; set -- $VALUES; IFS=$OLD_IFS
    C_INSTANCE=$1; C_PREFIX=$2; C_WORKSPACE=$3; C_STATE=$4; C_LOG=$5
    if [ -n "$INSTANCE" ] && [ "$INSTANCE" != "$C_INSTANCE" ]; then die "config instance_id=$C_INSTANCE differs from --instance=$INSTANCE; use --fresh for a new host"; fi
    if [ -n "$PREFIX" ] && [ "$PREFIX" != "$C_PREFIX" ]; then die "config sandbox_prefix=$C_PREFIX differs from --prefix=$PREFIX; use --fresh"; fi
    if [ -n "$WORKSPACE" ] && [ "$WORKSPACE" != "$C_WORKSPACE" ]; then die "config workspace_root=$C_WORKSPACE differs from --workspace=$WORKSPACE; use --fresh"; fi
    INSTANCE=$C_INSTANCE; PREFIX=$C_PREFIX; WORKSPACE=$C_WORKSPACE
    case "$C_STATE" in "$ROOT"/state) ;; *) die "config state_dir must be $ROOT/state (copied working tree? use --fresh)";; esac
    case "$C_LOG" in "$ROOT"/logs/*) ;; *) die "config log_file must be under $ROOT/logs (copied working tree? use --fresh)";; esac
else
    [ -n "$INSTANCE" ] || die "--instance is required for a new host"
    [ -n "$PREFIX" ] || die "--prefix is required for a new host"
    [ -n "$WORKSPACE" ] || die "--workspace is required for a new host"
    log "creating config/adapter.json"
    scripts/adapterctl init-config >/dev/null
    INSTANCE="$INSTANCE" PREFIX="$PREFIX" WORKSPACE="$WORKSPACE" ADAPTER_ROOT="$ROOT" "$ADAPTER_PY" - <<'PY'
import json, os
p=os.path.join(os.environ["ADAPTER_ROOT"], "config/adapter.json")
d=json.load(open(p))
d.update({
  "instance_id": os.environ["INSTANCE"],
  "sandbox_prefix": os.environ["PREFIX"],
  "workspace_root": os.environ["WORKSPACE"],
  "state_dir": os.path.join(os.environ["ADAPTER_ROOT"], "state"),
  "log_file": os.path.join(os.environ["ADAPTER_ROOT"], "logs/adapter.jsonl"),
})
open(p,"w").write(json.dumps(d, indent=2) + "\n")
PY
    chmod 600 config/adapter.json
fi

[ -n "$INSTANCE" ] && [ -n "$PREFIX" ] && [ -n "$WORKSPACE" ] || die "instance/prefix/workspace could not be resolved"
log "instance=$INSTANCE prefix=$PREFIX workspace=$WORKSPACE"

log "preparing directories"
scripts/adapterctl prepare-dirs >/dev/null

if [ -f state/baseline.json ]; then
    log "existing baseline found; validating daemon and identity"
    PYTHONPATH="$ROOT" "$ADAPTER_PY" - <<'PY' || die "baseline does not belong to this config/daemon; use --fresh on a new host"
import json, sys, docker
from octop_docker_runtime_adapter.config import Config
c=Config.load()
b=json.load(open(c.state_dir/"baseline.json"))
client=docker.from_env(timeout=30)
try:
    daemon=client.info()["ID"]
finally:
    client.close()
if b.get("schema") != 2 or b.get("instance") != c.instance_id or b.get("prefix") != c.sandbox_prefix:
    print("baseline instance/prefix mismatch"); sys.exit(1)
if b.get("docker_daemon_id") != daemon:
    print("baseline docker daemon mismatch"); sys.exit(1)
PY
else
    log "creating baseline (inventory + snapshot)"
    scripts/adapterctl inventory >/tmp/dra-setup-inventory.json
    scripts/adapterctl snapshot
    scripts/adapterctl assert-legacy >/dev/null
fi

log "ensuring shared network"
scripts/adapterctl ensure-network >/dev/null

log "doctor"
scripts/adapterctl doctor

if [ "$WITH_SMOKE" = 1 ]; then
    log "running real Docker smoke (optional)"
    scripts/smoke.sh --image "$BACKEND_IMAGE" --url "$SMOKE_URL"
fi

log "ensuring fixed backend $BACKEND_NAME"
set -- --config "$ROOT/config/adapter.json" --image "$BACKEND_IMAGE" \
     --memory "$MEMORY" --cpus "$CPUS" --pids-limit "$PIDS_LIMIT" \
     --command-timeout "$COMMAND_TIMEOUT"
[ "$NO_NETWORK" = 1 ] && set -- "$@" --no-network
OCTOP_HOME="$OCTOP_HOME" "$ADAPTER_PY" "$ROOT/scripts/ensure-backend.py" "$@"

log "backing up systemd unit/drop-in/wrapper"
B="$ROOT/state/private-backups/service-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$B"; chmod 700 "$B"
cp -p /home/smc/.config/systemd/user/octop.service "$B/" 2>/dev/null || true
cp -p /home/smc/.config/systemd/user/octop.service.d/*.conf "$B/" 2>/dev/null || true
cp -p /home/smc/.local/bin/octop-docker-run.sh "$B/" 2>/dev/null || true
cp -p /home/smc/.octop/env "$B/octop.env" 2>/dev/null || true
chmod 600 "$B"/* 2>/dev/null || true
sha256sum "$B"/* > "$B/SHA256SUMS" 2>/dev/null || true

log "installing v2 drop-in (no restart yet)"
scripts/install-dropin.sh

if [ "$DO_RESTART" = 0 ]; then
    log "drop-in installed; --no-restart set"
    echo "Next (idle window): systemctl --user restart octop.service"
    exit 0
fi

export XDG_RUNTIME_DIR=/run/user/$(id -u)
log "restarting octop.service"
RESTARTED_AT=$(date '+%Y-%m-%d %H:%M:%S')
systemctl --user restart octop.service

# systemd reports the unit "active" as soon as the Python process starts, but
# Octop only serves once uvicorn has bound the port and the app has loaded all
# users/agents. With dozens of existing agents that takes 30-60s on a busy
# host, so poll the real readiness signal instead of a fixed short window.
log "waiting for health endpoint (up to ${HEALTH_TIMEOUT}s)"
i=0
health_ok=0
while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    if curl -fsS -m 3 http://127.0.0.1:8088/api/health >/dev/null 2>&1; then
        health_ok=1
        break
    fi
    if systemctl --user is-failed --quiet octop.service; then
        break
    fi
    i=$((i+1))
    if [ $((i % 15)) -eq 0 ]; then
        log "  still starting (${i}s elapsed, unit $(systemctl --user is-active octop.service 2>/dev/null || true))"
    fi
    sleep 1
done

if [ "$health_ok" != 1 ]; then
    echo "---- octop.service status ----" >&2
    systemctl --user status octop.service --no-pager -l 2>&1 | tail -20 >&2 || true
    echo "---- journal since restart ----" >&2
    journalctl --user -u octop.service --since "$RESTARTED_AT" --no-pager 2>&1 | tail -30 >&2 || true
    die "Octop health endpoint did not become ready within ${HEALTH_TIMEOUT}s (unit: $(systemctl --user is-active octop.service 2>/dev/null || true)); re-run with --health-timeout N if the host needs longer"
fi
log "octop.service healthy after ${i}s"

log "checking adapter load events from the real service PID"
journalctl --user -u octop.service --since "$RESTARTED_AT" --no-pager 2>/dev/null \
    | grep -q 'adapter_hooks_registered' \
    || die "adapter_hooks_registered not found in service journal; check journalctl --user -u octop.service"
journalctl --user -u octop.service --since "$RESTARTED_AT" --no-pager 2>/dev/null \
    | grep -q 'hook_installed' \
    || die "hook_installed not found in service journal"

log "post-restart checks"
scripts/adapterctl doctor >/dev/null
scripts/adapterctl assert-legacy >/dev/null
OCTOP_HOME="$OCTOP_HOME" "$ADAPTER_PY" "$ROOT/scripts/ensure-backend.py" \
    --config "$ROOT/config/adapter.json" --check >/dev/null

cat <<EOF2
[setup] DONE
  instance: $INSTANCE
  prefix:   $PREFIX
  backend:  $BACKEND_NAME -> $BACKEND_IMAGE
  service:  $(systemctl --user is-active octop.service)
  state:    $ROOT/state
  logs:     $ROOT/logs/adapter.jsonl

Management server may now create experts with:
  backend: {"type":"named","name":"sandbox"}

Optional verification (not run by default):
  scripts/test.sh
  scripts/smoke.sh --image $BACKEND_IMAGE --url $SMOKE_URL
EOF2
