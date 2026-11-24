#!/usr/bin/env python3
"""Create or verify the fixed Docker storage backend named ``sandbox``.

The management server always submits backend={"type":"named","name":"sandbox"},
so every Octop instance must have exactly this row, pointing at that instance's
adapter sandbox_prefix.

Usage (run as the Octop service user, with Octop's venv):
  /data/octop/.venv/bin/python deploy-notes/ensure-backend.py --check
  /data/octop/.venv/bin/python deploy-notes/ensure-backend.py \
      --image octop-sandbox:2026.11-base

Default: create when missing; verify when present; refuse to overwrite a
mismatching existing row (use --update explicitly, after migration).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

NAME = "sandbox"


def adapter_config(path: Path) -> dict:
    data = json.loads(path.read_text())
    for key in ("instance_id", "sandbox_prefix"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise SystemExit(f"adapter config missing {key}: {path}")
    return data


def containers_for_prefix(prefix: str) -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={prefix}_", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/adapter.json")
    ap.add_argument("--image", default="octop-sandbox:2026.11-base")
    ap.add_argument("--memory", default="2g")
    ap.add_argument("--cpus", type=float, default=2)
    ap.add_argument("--pids-limit", type=int, default=512)
    ap.add_argument("--command-timeout", type=int, default=120)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify only; never write")
    ap.add_argument("--update", action="store_true", help="overwrite an existing mismatching row")
    ap.add_argument("--force", action="store_true", help="allow --update while prefix containers exist")
    args = ap.parse_args()

    cfg = adapter_config(Path(args.config))
    prefix = cfg["sandbox_prefix"]
    desired_cfg = {
        "sandbox_scope": "user",
        "sandbox_prefix": prefix,
        "allow_network": not args.no_network,
        "memory": args.memory,
        "cpus": args.cpus,
        "pids_limit": args.pids_limit,
        "command_timeout": args.command_timeout,
    }
    desired_spec = {"type": "docker", "image": args.image, **desired_cfg}

    from octop.cli.support.db import open_cli_services
    from octop.infra.backend.adapter import row_to_backend_spec
    from octop.infra.backend.resolver import resolve_agent_backend_spec
    from octop.infra.users.groups import (
        GroupAccess, RESOURCE_STORAGE_BACKEND, STORAGE_FEATURE_KEY,
    )

    with open_cli_services() as s:
        repo = s.storage_backend_repo
        row = repo.get_by_name(NAME)
        if row is None:
            if args.check:
                print(f"MISSING backend {NAME!r}; run without --check to create")
                return 2
            bid = repo.create(
                name=NAME, kind="docker", bucket=args.image,
                config_json=json.dumps(desired_cfg, sort_keys=True),
                note="DSH DRA managed backend; management server references named:sandbox",
            )
            repo.update(bid, enabled=True, bucket=args.image,
                        config_json=json.dumps(desired_cfg, sort_keys=True))
            row = repo.get(bid)
            print(f"CREATED backend {NAME!r} id={bid}")
        else:
            current = row_to_backend_spec(row)
            expected = desired_spec
            if current != expected or not row.enabled:
                print(f"BACKEND {NAME!r} MISMATCH")
                print("  current:", json.dumps(current, sort_keys=True) if current else None)
                print("  wanted :", json.dumps(expected, sort_keys=True))
                if not args.update:
                    print("Refusing to overwrite. Re-run with --update after the user migration plan.")
                    return 2
                existing = containers_for_prefix(prefix)
                if existing and not args.force:
                    print("Existing containers with this prefix:", ", ".join(existing[:10]))
                    print("A profile/image change requires per-user recreation. Refusing --update without --force.")
                    return 2
                repo.update(
                    row.id, bucket=args.image,
                    config_json=json.dumps(desired_cfg, sort_keys=True),
                    enabled=True,
                    note="DSH DRA managed backend; management server references named:sandbox",
                )
                row = repo.get(row.id)
                print(f"UPDATED backend {NAME!r} id={row.id}")
            else:
                print(f"OK backend {NAME!r} id={row.id}")

        if not args.check:
            ga = GroupAccess(s.repos)
            ga.clear(RESOURCE_STORAGE_BACKEND, STORAGE_FEATURE_KEY)
            ga.set_visibility(RESOURCE_STORAGE_BACKEND, NAME, "all", [])

        row = repo.get_by_name(NAME)
        resolved = resolve_agent_backend_spec({"type": "named", "name": NAME}, repo=repo)
        if resolved != desired_spec:
            print("RESOLVE MISMATCH")
            print("  resolved:", json.dumps(resolved, sort_keys=True))
            print("  wanted  :", json.dumps(desired_spec, sort_keys=True))
            return 2
        print("RESOLVED", json.dumps(resolved, sort_keys=True))
        print(f"VISIBILITY all; enabled={bool(row.enabled)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
