"""CLI for read-only template discovery and explicit bulk workspace restoration."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .baseline import load_baseline
from .cli import client
from .compat import evidence
from .config import Config, PolicyError
from .workspace_batch import (Service, build_plan, config_digest, error_code, execute_batch,
                              rollback_batch, validate_plan)
from .workspace_files import atomic_json, no_symlinks, zip_manifest
from .workspace_source import catalog, templates


def source_args(args):
    return {"octop_home": str(Path(args.octop_home).absolute()),
            "sqlite_path": str(Path(args.sqlite).absolute()) if args.sqlite else None,
            "agent_ids": sorted(set(args.agent_id or [])),
            "service_unit": args.service_unit, "service_scope": args.service_scope}


def get_catalog(source, template_id, kind):
    return catalog(Path(source["octop_home"]), template_id, kind,
                   Path(source["sqlite_path"]) if source["sqlite_path"] else None,
                   source.get("agent_ids", []))


def parser():
    ap = argparse.ArgumentParser(description="按模板列出用户/专家/挂载目录，并批量恢复工作区 ZIP（默认仅查询）")
    ap.add_argument("--config", help="adapter.json；默认沿用 OCTOP_DRA_CONFIG")
    ap.add_argument("--octop-home", default=os.environ.get("OCTOP_HOME", "/home/smc/.octop"))
    ap.add_argument("--sqlite", help="可选：显式只读 SQLite 数据库路径；否则读取当前 Octop 数据库配置，支持 PostgreSQL")
    ap.add_argument("--service-unit", default="octop.service")
    ap.add_argument("--service-scope", choices=["user", "system"], default="user")
    commands = ap.add_subparsers(dest="action", required=True)
    commands.add_parser("templates", help="列出已发布模板的准确 ID")
    for name in ("list", "plan"):
        sub = commands.add_parser(name, help="查询挂载清单" if name == "list" else "检查 ZIP 并生成固定恢复清单")
        sub.add_argument("--template-id", required=True)
        sub.add_argument("--template-kind", choices=["published", "builtin"], default="published")
        sub.add_argument("--agent-id", action="append", help="可重复；只选择属于此模板的指定专家")
        sub.add_argument("--output", required=name == "plan", help="保存 JSON 清单；文件必须尚不存在")
        if name == "plan":
            sub.add_argument("--zip", required=True)
            sub.add_argument("--strip-components", type=int, default=0)
            sub.add_argument("--max-expanded-mib", type=int, default=4096)
            sub.add_argument("--max-entries", type=int, default=100000)
    apply = commands.add_parser("apply", help="执行固定清单；必须在维护窗口使用")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--stop-runtime", action="store_true", help="停止当前 Octop 服务和目标用户容器，完成后恢复原运行状态")
    rollback = commands.add_parser("rollback", help="从批次备份回滚工作区，不回滚数据库或容器配置")
    rollback.add_argument("--report", required=True)
    rollback.add_argument("--stop-runtime", action="store_true")
    rollback.add_argument("--overwrite-changed", action="store_true", help="先备份恢复后新增的数据，再允许覆盖回滚")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = Config.load(args.config)
        no_symlinks(cfg.state_dir)
        owner = cfg.state_dir.stat().st_uid
        if os.geteuid() not in {0, owner}:
            raise PolicyError("run_as_octop_service_user_or_root")
        baseline = load_baseline(cfg)
        if evidence() != baseline["compatibility"]:
            raise PolicyError("compatibility_changed")
        if args.action == "templates":
            result = templates(Path(args.octop_home), Path(args.sqlite) if args.sqlite else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        with client() as docker:
            if args.action in {"list", "plan"}:
                source = source_args(args)
                current = get_catalog(source, args.template_id, args.template_kind)
                archive = None
                if args.action == "plan":
                    if args.max_expanded_mib <= 0 or args.max_entries <= 0:
                        raise PolicyError("zip_limits_must_be_positive")
                    archive = zip_manifest(Path(args.zip).absolute(), strip_components=args.strip_components,
                                           max_bytes=args.max_expanded_mib * 1024**2, max_files=args.max_entries)
                result = build_plan(cfg, current, docker, archive=archive, source=source)
                if args.output:
                    output = Path(args.output).absolute()
                    if output.exists():
                        raise PolicyError("output_exists_use_new_plan_filename")
                    atomic_json(output, result, owner=owner)
                    print(json.dumps({"output": str(output), "target_count": result["target_count"],
                                      "ready_count": result["ready_count"],
                                      "targets": result["targets"]}, ensure_ascii=False, indent=2))
                else:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["target_count"] and result["ready_count"] == result["target_count"] else 2
            if args.action == "apply":
                path = no_symlinks(Path(args.plan).absolute())
                plan = json.loads(path.read_text())
            else:
                path = no_symlinks(Path(args.report).absolute())
                plan = json.loads(path.read_text())["plan"]
            validate_plan(plan)
            if config_digest(cfg) != plan["config_digest"]:
                raise PolicyError("adapter_config_changed_since_plan")
            source = plan["source"]
            service = Service(owner, source["service_unit"], source["service_scope"])
            def refresh():
                # Only the reviewed IDs. Newly created agents require a new plan.
                current_source = {**source, "agent_ids": [t["agent_id"] for t in plan["targets"]]}
                current = get_catalog(current_source, plan["template_id"], plan["template_kind"])
                return build_plan(cfg, current, docker, source=source)
            if args.action == "apply":
                report_path, result = execute_batch(cfg, plan, docker, service, owner,
                                                    manage_runtime=args.stop_runtime, refresh=refresh)
            else:
                report_path, result = rollback_batch(cfg, path, docker, service, owner,
                                                     manage_runtime=args.stop_runtime,
                                                     overwrite_changed=args.overwrite_changed, refresh=refresh)
            print(json.dumps({"report": str(report_path), "status": result["status"],
                              "error": result.get("error"), "rollback_error": result.get("rollback_error"),
                              "runtime_resume_errors": result.get("runtime_resume_errors", []),
                              "results": result["results"]}, ensure_ascii=False, indent=2))
            return 0 if result["status"] in {"completed", "rolled_back"} else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": error_code(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
