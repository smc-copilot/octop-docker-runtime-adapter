#!/bin/sh
# Replaces only the exact known v1 drop-in or installs the v2 drop-in. No restart.
set -eu
umask 077
ADAPTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$ADAPTER_ROOT" != /data/octop-docker-runtime-adapter ] || [ "$(id -un)" != smc ]; then
    echo 'Use the reviewed installation path and service user smc.' >&2
    exit 2
fi
"$ADAPTER_ROOT/scripts/adapterctl" doctor
"$ADAPTER_ROOT/scripts/adapterctl" assert-legacy
ADAPTER_TARGET=/home/smc/.config/systemd/user/octop.service.d/90-octop-docker-runtime-adapter.conf
ADAPTER_SOURCE="$ADAPTER_ROOT/deploy/90-octop-docker-runtime-adapter.conf"
if [ -L "$ADAPTER_TARGET" ]; then
    echo 'Refusing a symlink drop-in.' >&2
    exit 2
fi
if [ -f "$ADAPTER_TARGET" ]; then
    if cmp -s "$ADAPTER_TARGET" "$ADAPTER_SOURCE"; then
        echo 'Identical v2 drop-in already installed; service not restarted.'
        exit 0
    fi
    if ! cmp -s "$ADAPTER_TARGET" "$ADAPTER_ROOT/deploy/v1-dropin-reference.conf"; then
        echo 'Existing custom drop-in requires a reviewed merge; file not overwritten.' >&2
        exit 2
    fi
    ADAPTER_BACKUP="$ADAPTER_ROOT/state/previous-dropin.$(date +%Y%m%dT%H%M%S).$$.conf"
    cp -- "$ADAPTER_TARGET" "$ADAPTER_BACKUP"
    chmod 0600 "$ADAPTER_BACKUP"
fi
mkdir -p "$(dirname -- "$ADAPTER_TARGET")"
install -m 0600 "$ADAPTER_SOURCE" "$ADAPTER_TARGET.tmp.$$"
mv -- "$ADAPTER_TARGET.tmp.$$" "$ADAPTER_TARGET"
systemctl --user daemon-reload
echo 'V2 drop-in installed. Octop NOT restarted. Complete DSH deployment checks first.'
