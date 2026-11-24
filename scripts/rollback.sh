#!/bin/sh
# Disable only this exact v2 injection. Does not stop, delete or alter containers/data.
set -eu
umask 077
ADAPTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ADAPTER_TARGET=/home/smc/.config/systemd/user/octop.service.d/90-octop-docker-runtime-adapter.conf
if [ "$(id -un)" != smc ]; then
    echo 'Run as smc.' >&2
    exit 2
fi
if [ -f "$ADAPTER_TARGET" ]; then
    if [ -L "$ADAPTER_TARGET" ] || ! cmp -s "$ADAPTER_TARGET" "$ADAPTER_ROOT/deploy/90-octop-docker-runtime-adapter.conf"; then
        echo 'Refusing to disable an unrelated or changed drop-in.' >&2
        exit 2
    fi
    mv -- "$ADAPTER_TARGET" "$ADAPTER_ROOT/state/disabled-v2.$(date +%Y%m%dT%H%M%S).$$.conf"
fi
systemctl --user daemon-reload
echo 'V2 disabled for the next restart. Running process still has hooks.'
echo 'Pause new user containers until persistence is restored. Review rollback notes before restart.'
