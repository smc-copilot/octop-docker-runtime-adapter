"""Read the current Octop control plane without migrations or DB writes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import PolicyError

# Mirrors pinned Octop apply_docker_cfg_keys. Avoid importing backend.__init__,
# which imports the entire Harness runtime during a read-only DB query.
# The contract test compares this tuple with the installed Octop source.
DOCKER_KEYS = (
    "allow_network", "memory", "cpus", "pids_limit", "command_timeout", "max_output_bytes",
    "auto_remove", "workspace_path", "volumes", "environment", "environment_file", "agent_id",
    "container_name", "sandbox_scope", "sandbox_prefix", "username", "sandbox_id", "previewable",
)


@contextmanager
def readonly_database(home: Path, sqlite_path: Path | None = None):
    if sqlite_path is None:
        from octop.config import load_config
        from octop.infra.db.factory import resolve_sqlite_db_path
        from octop.infra.utils.env_file import apply_env_file, env_file_path
        from octop.infra.utils.paths import PathLayout

        paths = PathLayout(home)
        # load_config writes defaults when absent. Never take that branch here.
        if not paths.config.is_file():
            raise PolicyError("octop_config_missing_use_correct_octop_home")
        apply_env_file(env_file_path(paths.root))
        config = load_config(paths.config)
        if config.database.is_postgresql:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(config.database.postgresql_conninfo(),
                                 row_factory=dict_row, connect_timeout=10) as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '15000ms'")
                yield lambda sql, args=(): [dict(r) for r in conn.execute(sql.replace("?", "%s"), args)]
            return
        sqlite_path = resolve_sqlite_db_path(config, paths)
    db_path = sqlite_path.expanduser().resolve(strict=True)
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield lambda sql, args=(): [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def templates(home, sqlite_path=None):
    with readonly_database(home, sqlite_path) as query:
        return query("SELECT published_expert_id, slug, name, source_agent_id "
                     "FROM published_experts ORDER BY name, published_expert_id")


def resolve_docker_spec(raw, backends):
    if not isinstance(raw, dict):
        raise PolicyError("explicit_docker_backend_required")
    if raw.get("type") == "named":
        row = backends.get(raw.get("name"))
        if row is None or str(row.get("kind", "")).lower() != "docker":
            raise PolicyError("named_backend_missing_or_not_docker")
        config = json.loads(row.get("config_json") or "{}")
        if not isinstance(config, dict):
            raise PolicyError("invalid_named_backend_config")
        image = config.get("image") or row.get("bucket")
        if not image:
            raise PolicyError("named_docker_backend_image_missing")
        return {"type": "docker", "image": str(image),
                **{key: config[key] for key in DOCKER_KEYS if key in config}}
    return dict(raw)


def catalog(home, template_id, kind="published", sqlite_path=None, agent_ids=()):
    if kind not in {"published", "builtin"}:
        raise PolicyError("unknown_template_kind")
    with readonly_database(home, sqlite_path) as query:
        if kind == "published":
            rows = query("SELECT published_expert_id, slug, name, source_agent_id "
                         "FROM published_experts WHERE published_expert_id = ?", (template_id,))
            if len(rows) != 1:
                raise PolicyError("published_template_id_not_found")
            template = rows[0]
            field = "published_expert_id"
        else:
            template = {"template_name": template_id, "source_agent_id": None}
            field = "template_name"
        # field is one of the two constants above, never user-supplied SQL.
        agents = query("SELECT a.agent_id, a.user_id, a.name, a.enabled, a.kind, "
                       "a.config_json, a.last_state, a.published_expert_id, a.template_name, "
                       "u.username FROM agents a LEFT JOIN users u ON u.id = a.user_id "
                       f"WHERE a.{field} = ? ORDER BY a.user_id, a.agent_id", (template_id,))
        backends = {r["name"]: r for r in query("SELECT * FROM storage_backends")}
    source = template.get("source_agent_id")
    agents = [a for a in agents if a["agent_id"] != source]
    requested = set(agent_ids)
    if requested:
        if requested - {a["agent_id"] for a in agents}:
            raise PolicyError("requested_agent_not_created_from_this_template")
        agents = [a for a in agents if a["agent_id"] in requested]
    for agent in agents:
        try:
            config = json.loads(agent.pop("config_json") or "{}")
            if not isinstance(config, dict):
                raise ValueError("config must be object")
            agent["workspace_dir"] = config.get("workspace_dir")
            agent["backend"] = resolve_docker_spec(config.get("backend"), backends)
        except Exception as exc:
            agent["source_error"] = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
    return {"template_id": template_id, "template_kind": kind,
            "template": template, "agents": agents}
