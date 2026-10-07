"""Per-profile first-class Project store.

The schema is small and additive: column additions go through ``add_column_if_missing`` so
opening an old DB is always safe.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Literal, Optional

from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing, open_db, write_txn
from hermes_constants import get_hermes_home


def projects_db_path() -> Path:
    """The per-profile projects DB path (``$HERMES_HOME/projects.db``)."""
    return get_hermes_home() / "projects.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    description   TEXT,
    icon          TEXT,
    color         TEXT,
    board_slug    TEXT,
    primary_path  TEXT,
    created_at    INTEGER NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS project_folders (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    label       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_project_folders_path
    ON project_folders(path);

CREATE TABLE IF NOT EXISTS project_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Git repos found by scanning the filesystem (desktop "repo-first" discovery).
-- Cached here so the overview is instant after the first scan instead of
-- re-walking the disk every time the Projects view opens.
CREATE TABLE IF NOT EXISTS discovered_repos (
    root          TEXT PRIMARY KEY,
    label         TEXT,
    last_seen     INTEGER NOT NULL
);
"""

# Lowercase alphanumerics, hyphens, underscores; 1-64 chars; no leading separator. Strict enough to
# stop traversal/path separators, loose enough for kebab-case. Display formatting lives in ``name``.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
# Deterministic branch slug: lowercase, separators collapsed, capped.
_BRANCH_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_INITIALIZED_PATHS: set[str] = set()
# TEXT columns added to `projects` after v1; re-applied idempotently on every open so a legacy DB
# upgrades in place. The three policy columns (Step 21) are nullable: NULL means the policy is
# unresolved and ``resolve_project`` fails closed on them.
_OPTIONAL_PROJECT_COLUMNS = ("board_slug", "primary_path", "icon", "color",
                             "ownership_class", "default_access_mode", "canonical_source_type")
# Nullable TEXT columns that may be absent from a legacy row.
_OPTIONAL_ROW_FIELDS = ("description", "icon", "color", "board_slug", "primary_path",
                        "ownership_class", "default_access_mode", "canonical_source_type")
_ACTIVE_META_KEY = "active_id"
_DISCOVERY_POLICY_META_KEY = "repo_discovery_policy"


def _slugify(name: str) -> str:
    """Derive a slug candidate from a human name (best-effort)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-_")
    return s[:64].strip("-_") or "project"


def normalize_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _SLUG_RE.match(s):
        raise ValueError(
            f"invalid project slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with "
            f"'-' or '_'"
        )
    return s


def _now() -> int:
    return int(time.time())


def _normalize_path(path: str) -> str:
    """Absolute, user-expanded, separator-normalized path (no trailing sep)."""
    p = os.path.abspath(os.path.expanduser(str(path).strip()))
    return p.rstrip("/\\") or p


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and initialize if needed) the per-profile projects DB.

    WAL with DELETE fallback for network filesystems (``hermes_state`` helper). Schema init is
    idempotent (``CREATE TABLE IF NOT EXISTS`` + additive migrations) and cached per-path per-process.
    """
    path = db_path if db_path is not None else projects_db_path()
    resolved = str(path.resolve())

    def _initialize(conn: sqlite3.Connection) -> None:
        if resolved in _INITIALIZED_PATHS:
            return
        conn.executescript(SCHEMA_SQL)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        for col in _OPTIONAL_PROJECT_COLUMNS:
            if col not in cols:
                _add_column_if_missing(conn, "projects", col, f"{col} TEXT")
        _INITIALIZED_PATHS.add(resolved)

    return open_db(path, db_label="projects.db", foreign_keys=True, initialize=_initialize)


@contextlib.contextmanager
def connect_closing(db_path: Optional[Path] = None):
    """Open a projects DB connection and close it on exit (sqlite3's own context manager only
    commits/rollbacks, so long-lived gateway/dashboard processes would leak fds otherwise)."""
    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@dataclass
class ProjectFolder:
    path: str
    label: Optional[str] = None
    is_primary: bool = False
    added_at: int = 0

    def to_dict(self) -> dict:
        return {"path": self.path, "label": self.label, "is_primary": bool(self.is_primary), "added_at": self.added_at}


@dataclass
class Project:
    id: str
    slug: str
    name: str
    created_at: int
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    board_slug: Optional[str] = None
    primary_path: Optional[str] = None
    archived: bool = False
    # Policy metadata (Step 21). Nullable: NULL / "UNKNOWN" are unresolved and fail closed in
    # ``resolve_project``; they are never inferred. Vocabulary per HERMES_PROJECT_REGISTRY.md.
    ownership_class: Optional[str] = None
    default_access_mode: Optional[str] = None
    canonical_source_type: Optional[str] = None
    folders: List[ProjectFolder] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("id", "slug", "name", *_OPTIONAL_ROW_FIELDS)}
        return {**d, "archived": bool(self.archived), "created_at": self.created_at, "folders": [f.to_dict() for f in self.folders]}


def _load_project(conn: sqlite3.Connection, row: sqlite3.Row) -> Project:
    """Materialize a ``projects`` row together with its folders."""
    keys = row.keys()
    folders = conn.execute(
        "SELECT path, label, is_primary, added_at FROM project_folders WHERE project_id = ? ORDER BY is_primary DESC, added_at ASC",
        (row["id"],),
    ).fetchall()
    return Project(
        id=row["id"], slug=row["slug"], name=row["name"], created_at=row["created_at"],
        archived=bool(row["archived"]) if "archived" in keys else False,
        folders=[ProjectFolder(r["path"], r["label"], bool(r["is_primary"]), r["added_at"]) for r in folders],
        **{f: row[f] for f in _OPTIONAL_ROW_FIELDS if f in keys},
    )


def _unique_slug(conn: sqlite3.Connection, candidate: str) -> str:
    """Return ``candidate`` or ``candidate-2``, ``-3`` ... if taken."""
    n, slug = 1, candidate
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone() is not None:
        n += 1
        slug = candidate[: 64 - len(f"-{n}")].rstrip("-_") + f"-{n}"
    return slug


def _primary_path_key(path: str) -> str:
    """Comparison key for primary-path dedup (absolute + case/sep-normalized)."""
    return os.path.normcase(_normalize_path(path))


def find_by_primary_path(conn: sqlite3.Connection, path: str, *, include_archived: bool = False) -> Optional[Project]:
    """The first (oldest) project whose primary path matches ``path`` (separator/case normalized so
    equivalent Windows spellings don't slip past the dedup check), else None."""
    key = _primary_path_key(path)
    for proj in list_projects(conn, include_archived=include_archived) if key else ():
        primary = proj.primary_path or next(
            (f.path for f in proj.folders if f.is_primary), proj.folders[0].path if proj.folders else None
        )
        if primary and _primary_path_key(primary) == key:
            return proj
    return None


def create_project(
    conn: sqlite3.Connection, *, name: str, slug: Optional[str] = None, folders: Optional[Iterable[str]] = None,
    primary_path: Optional[str] = None, description: Optional[str] = None, icon: Optional[str] = None,
    color: Optional[str] = None, board_slug: Optional[str] = None, allow_duplicate_path: bool = False,
) -> str:
    """Create a project and return its id. ``folders`` are normalized to absolute paths; ``primary_path``
    is added to the folder set (if absent) and marked primary, else the first folder becomes primary."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    slug_candidate = normalize_slug(slug) if slug else _slugify(name)
    pid = "p_" + secrets.token_hex(4)
    now = _now()
    folder_paths = list(dict.fromkeys(p for p in map(_normalize_path, folders or []) if p))
    primary = _normalize_path(primary_path) if primary_path else None
    if primary and primary not in folder_paths:
        folder_paths.insert(0, primary)
    if primary is None and folder_paths:
        primary = folder_paths[0]
    existing = find_by_primary_path(conn, primary) if primary and not allow_duplicate_path else None
    if existing is not None:
        raise ValueError(
            f"folder already belongs to project '{existing.slug}' ({existing.id}); "
            "switch to it instead of creating a duplicate"
        )
    with write_txn(conn):
        conn.execute(
            "INSERT INTO projects (id, slug, name, description, icon, color, board_slug,  primary_path, created_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (pid, _unique_slug(conn, slug_candidate), name, description, icon, color,
             normalize_slug(board_slug) if board_slug else None, primary, now),
        )
        conn.executemany(
            "INSERT INTO project_folders (project_id, path, label, is_primary, added_at) VALUES (?, ?, ?, ?, ?)",
            [(pid, path, None, 1 if path == primary else 0, now) for path in folder_paths],
        )
    return pid


def list_projects(conn: sqlite3.Connection, *, include_archived: bool = False) -> List[Project]:
    sql = "SELECT * FROM projects" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY created_at ASC"
    return [_load_project(conn, r) for r in conn.execute(sql).fetchall()]


def get_project(conn: sqlite3.Connection, id_or_slug: str) -> Optional[Project]:
    """Look up a project by id first, then by slug."""
    row = (
        conn.execute("SELECT * FROM projects WHERE id = ?", (id_or_slug,)).fetchone()
        or conn.execute("SELECT * FROM projects WHERE slug = ?", (str(id_or_slug).lower(),)).fetchone()
    )
    return None if row is None else _load_project(conn, row)


def update_project(
    conn: sqlite3.Connection, project_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, board_slug: Optional[str] = None,
) -> bool:
    """Patch top-level project fields; only provided (non-None) fields change. ``icon``, ``color`` and
    ``board_slug`` take ``""`` to clear (store NULL) — ``None`` leaves the field untouched."""
    if name is not None:
        name = str(name).strip()
        if not name:
            raise ValueError("project name must not be empty")
    if board_slug is not None:
        board_slug = normalize_slug(board_slug) if board_slug.strip() else ""
    # (column, provided value, stored value) — "" clears icon/color/board_slug to NULL.
    fields = [
        (col, given, stored) for col, given, stored in (
            ("name", name, name), ("description", description, description), ("icon", icon, icon or None),
            ("color", color, color or None), ("board_slug", board_slug, board_slug or None),
        ) if given is not None
    ]
    if not fields:
        return False
    sets = ", ".join(f"{col} = ?" for col, _, _ in fields)
    return _execute_rowcount(conn, f"UPDATE projects SET {sets} WHERE id = ?", [f[2] for f in fields] + [project_id]) > 0


# Write-time policy vocabulary (Step 25). These are the ONLY accepted values when an
# operator explicitly sets a field. UNKNOWN is a legitimate explicit operator choice for
# ownership_class and canonical_source_type; it is NOT a valid default_access_mode. The
# resolver still fails closed on any stored UNKNOWN (it only resolves complete policy).
_POLICY_WRITE_VOCAB = {
    "ownership_class": ("HERMES_OWNED", "EXTERNAL_OWNED", "UNKNOWN"),
    "default_access_mode": ("READ_ONLY",),
    "canonical_source_type": ("LOCAL_FILE", "EXTERNAL_GIT_REPOSITORY", "UNKNOWN"),
}


def _validate_policy_value(field: str, value) -> str:
    """Exact, case-sensitive validation of one operator-supplied policy value.

    Rejects empty / whitespace-only / non-string / any value outside the write
    vocabulary. No inference, no Registry lookup, no filesystem/Git derivation.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty value")
    allowed = _POLICY_WRITE_VOCAB[field]
    if value not in allowed:
        raise ValueError(f"invalid {field}: {value!r}; expected one of {' | '.join(allowed)}")
    return value


def set_project_policy(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ownership_class: Optional[str] = None,
    default_access_mode: Optional[str] = None,
    canonical_source_type: Optional[str] = None,
) -> bool:
    """Atomically set the policy fields of an EXISTING project; only provided fields change.

    ``None`` leaves a field untouched. At least one field must be provided. The project must
    already exist (resolved via the exact ``get_project`` id-then-slug semantics — no path
    lookup, cwd, fuzzy/prefix, or implicit create). Only the three policy columns are ever
    written. One UPDATE in a single write txn. Returns ``False`` if no field was provided;
    raises ``ValueError`` for a missing project or an invalid value.
    """
    if get_project(conn, project_id) is None:
        raise ValueError("no such project")
    provided = (
        ("ownership_class", ownership_class),
        ("default_access_mode", default_access_mode),
        ("canonical_source_type", canonical_source_type),
    )
    fields = [(col, _validate_policy_value(col, val)) for col, val in provided if val is not None]
    if not fields:
        return False
    sets = ", ".join(f"{col} = ?" for col, _ in fields)
    return _execute_rowcount(
        conn, f"UPDATE projects SET {sets} WHERE id = ?", [val for _, val in fields] + [project_id]
    ) > 0


def _execute_rowcount(conn: sqlite3.Connection, sql: str, params) -> int:
    """Run one write statement in its own txn and return the affected row count."""
    with write_txn(conn):
        cur = conn.execute(sql, params)
    return cur.rowcount


def add_folder(conn: sqlite3.Connection, project_id: str, path: str, *, label: Optional[str] = None, is_primary: bool = False) -> str:
    """Add a folder to a project. Returns the normalized path."""
    norm = _normalize_path(path)
    if not norm:
        raise ValueError("folder path must not be empty")
    if get_project(conn, project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO project_folders (project_id, path, label, is_primary, added_at) VALUES (?, ?, ?, 0, ?)",
            (project_id, norm, label, _now()),
        )
        if label is not None:
            conn.execute("UPDATE project_folders SET label = ? WHERE project_id = ? AND path = ?", (label, project_id, norm))
        # An explicit primary, or the first folder of an empty project, becomes primary.
        if is_primary or conn.execute(
            "SELECT 1 FROM project_folders WHERE project_id = ? AND is_primary = 1", (project_id,)
        ).fetchone() is None:
            _set_primary_locked(conn, project_id, norm)
    return norm


def remove_folder(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    """Remove a folder from a project. Repoints primary if it was primary."""
    norm = _normalize_path(path)
    with write_txn(conn):
        was_primary = conn.execute(
            "SELECT is_primary FROM project_folders WHERE project_id = ? AND path = ?", (project_id, norm)
        ).fetchone()
        cur = conn.execute("DELETE FROM project_folders WHERE project_id = ? AND path = ?", (project_id, norm))
        if was_primary is not None and was_primary["is_primary"]:
            nxt = conn.execute(
                "SELECT path FROM project_folders WHERE project_id = ? ORDER BY added_at ASC LIMIT 1", (project_id,)
            ).fetchone()
            if nxt and nxt["path"]:
                _set_primary_locked(conn, project_id, nxt["path"])
            else:
                conn.execute("UPDATE projects SET primary_path = NULL WHERE id = ?", (project_id,))
    return cur.rowcount > 0


def _set_primary_locked(conn: sqlite3.Connection, project_id: str, path: str) -> None:
    """Set the primary folder (caller already holds a write txn)."""
    conn.execute("UPDATE project_folders SET is_primary = 0 WHERE project_id = ?", (project_id,))
    conn.execute("UPDATE project_folders SET is_primary = 1 WHERE project_id = ? AND path = ?", (project_id, path))
    conn.execute("UPDATE projects SET primary_path = ? WHERE id = ?", (path, project_id))


def set_primary(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    norm = _normalize_path(path)
    with write_txn(conn):
        if conn.execute("SELECT 1 FROM project_folders WHERE project_id = ? AND path = ?", (project_id, norm)).fetchone() is None:
            return False
        _set_primary_locked(conn, project_id, norm)
    return True


def archive_project(conn: sqlite3.Connection, project_id: str) -> bool:
    return _execute_rowcount(conn, "UPDATE projects SET archived = 1 WHERE id = ?", (project_id,)) > 0


def restore_project(conn: sqlite3.Connection, project_id: str) -> bool:
    return _execute_rowcount(conn, "UPDATE projects SET archived = 0 WHERE id = ?", (project_id,)) > 0


def delete_project(conn: sqlite3.Connection, project_id: str) -> bool:
    """Hard-delete a project and its folders (cascade)."""
    return _execute_rowcount(conn, "DELETE FROM projects WHERE id = ?", (project_id,)) > 0


# --- Active-project pointer + discovery policy (project_meta KV) --------------

def _upsert_meta_locked(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a project_meta row (caller already holds a write txn)."""
    conn.execute(
        "INSERT INTO project_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM project_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_active(conn: sqlite3.Connection, project_id: Optional[str]) -> None:
    """Set (or clear, when ``None``) the active project pointer."""
    with write_txn(conn):
        if project_id is None:
            conn.execute("DELETE FROM project_meta WHERE key = ?", (_ACTIVE_META_KEY,))
        else:
            _upsert_meta_locked(conn, _ACTIVE_META_KEY, project_id)


def get_active_id(conn: sqlite3.Connection) -> Optional[str]:
    return _get_meta(conn, _ACTIVE_META_KEY)


def get_discovery_policy_key(conn: sqlite3.Connection) -> Optional[str]:
    return _get_meta(conn, _DISCOVERY_POLICY_META_KEY)


def _clear_repos_locked(conn: sqlite3.Connection, clear: bool, policy_key: Optional[str]) -> None:
    """Optionally wipe the scan cache, then record the policy key when given (caller holds a write txn)."""
    if clear:
        conn.execute("DELETE FROM discovered_repos")
    if policy_key is not None:
        _upsert_meta_locked(conn, _DISCOVERY_POLICY_META_KEY, policy_key)


def reconcile_discovered_repos_policy(conn: sqlite3.Connection, policy_key: str, *, preserve_unversioned: bool = False) -> bool:
    """Clear cached scan rows when their discovery policy changes; pre-policy rows are retained only
    for the backward-compatible default policy. Returns whether rows were cleared."""
    current = get_discovery_policy_key(conn)
    if current == policy_key:
        return False
    cleared = current is not None or not preserve_unversioned
    with write_txn(conn):
        _clear_repos_locked(conn, cleared, policy_key)
    return cleared


def clear_discovered_repos(conn: sqlite3.Connection, *, policy_key: Optional[str] = None) -> None:
    with write_txn(conn):
        _clear_repos_locked(conn, True, policy_key)


def record_discovered_repos(
    conn: sqlite3.Connection, repos: Iterable[tuple[str, Optional[str]]], *, replace: bool = False,
    policy_key: Optional[str] = None,
) -> int:
    """Persist scanned ``(root, label)`` repo roots (normalized; label falls back to basename) and
    return the row count. ``replace`` = authoritative fresh scan: stale rows are deleted first so old
    eval/worktree noise doesn't live forever."""
    now = _now()
    rows = [
        (norm, label or os.path.basename(norm) or norm, now)
        for norm, label in ((_normalize_path(root), label) for root, label in repos) if norm
    ]
    with write_txn(conn):
        if replace:
            conn.execute("DELETE FROM discovered_repos")
        if rows:
            conn.executemany(
                "INSERT INTO discovered_repos (root, label, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(root) DO UPDATE SET label = excluded.label, last_seen = excluded.last_seen",
                rows,
            )
        _clear_repos_locked(conn, False, policy_key)
    return len(rows)


def list_discovered_repos(conn: sqlite3.Connection) -> List[dict]:
    """All cached discovered repo roots, most-recently-seen first."""
    return [dict(r) for r in conn.execute("SELECT root, label, last_seen FROM discovered_repos ORDER BY last_seen DESC").fetchall()]


def project_for_path(conn: sqlite3.Connection, path: str, *, include_archived: bool = False) -> Optional[Project]:
    """Return the project owning ``path``: a folder owns it when equal or an ancestor, and the longest
    folder wins so nested projects resolve to the innermost one."""
    if not str(path or "").strip():
        return None
    target = _normalize_path(path)
    sql = "SELECT pf.project_id AS pid, pf.path AS folder FROM project_folders pf JOIN projects p ON p.id = pf.project_id"
    if not include_archived:
        sql += " WHERE p.archived = 0"

    def owns(folder: str) -> bool:
        stem = folder.rstrip("/\\")
        return target == folder or target.startswith(stem + os.sep) or target.startswith(stem + "/")

    owners = [row for row in conn.execute(sql).fetchall() if owns(row["folder"])]
    return get_project(conn, max(owners, key=lambda r: len(r["folder"]))["pid"]) if owners else None


def branch_name_for(project: Project, task_id: str, *, title: str = "") -> str:
    """Deterministic ``<project-slug>/<task-id>[-<title-slug>]`` branch name for a project-linked kanban
    task (stable and human-meaningful, replacing the random ``wt/<task-id>`` fallback)."""
    base = f"{project.slug or _slugify(project.name)}/{task_id}"
    tslug = _BRANCH_SAFE_RE.sub("-", str(title).strip().lower()).strip("-")[:40].strip("-") if title else ""
    return f"{base}-{tslug}" if tslug else base


# ---------------------------------------------------------------------------
# Project resolution contract (Step 21) — read-path only.
#
# The resolver exposes policy metadata and never grants write authority:
# ``default_access_mode`` is a ceiling, not authorization, and every write
# requires a separate, current, project-specific approval (a later layer).
# Vocabulary per HERMES_PROJECT_REGISTRY.md — it is NOT consulted at runtime;
# the DB row is the source of truth for this contract.
# ---------------------------------------------------------------------------

class ProjectResolutionError(Exception):
    """Base type for all project-resolution failures."""


class ProjectUnknown(ProjectResolutionError):
    """No project with the exact id/slug ``ref`` exists."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"unknown project: {ref!r}")
        self.ref = ref


class ProjectArchived(ProjectResolutionError):
    """The project exists but is archived."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"project is archived: {ref!r}")
        self.ref = ref


class ProjectPolicyIncomplete(ProjectResolutionError):
    """A policy field is missing, explicitly UNKNOWN, or holds an invalid value."""

    def __init__(self, ref: str, field: str, reason: Literal["missing", "unknown", "invalid"],
                 value: Optional[str] = None) -> None:
        super().__init__(f"project policy incomplete: {ref!r} field={field!r} reason={reason!r}")
        self.ref = ref
        self.field = field
        self.reason = reason
        self.value = value


class ProjectAccessDenied(ProjectResolutionError):
    """The requested access mode is denied by the project's policy ceiling."""

    def __init__(self, ref: str, requested_access: Literal["read", "write"]) -> None:
        super().__init__(f"access denied for {requested_access!r}: {ref!r}")
        self.ref = ref
        self.requested_access = requested_access


# Allowed policy values (HERMES_PROJECT_REGISTRY.md vocabulary). UNKNOWN is a
# stored value meaning "explicitly unresolved" and is rejected below.
_PROJECT_OWNERSHIP_CLASSES = ("HERMES_OWNED", "EXTERNAL_OWNED")
_PROJECT_SOURCE_TYPES = ("LOCAL_FILE", "EXTERNAL_GIT_REPOSITORY")
_PROJECT_ACCESS_MODES = ("READ_ONLY",)


@dataclass(frozen=True)
class ResolvedProject:
    """Read-path resolution result: identity + verified policy metadata."""
    id: str
    slug: str
    name: str
    canonical_root: str
    canonical_source_type: str
    ownership_class: str
    default_access_mode: str


def _check_policy_field(ref: str, field: str, value: Optional[str], allowed: tuple[str, ...]) -> str:
    """Fail closed on one policy field; return the verified value."""
    if value is None or not str(value).strip():
        raise ProjectPolicyIncomplete(ref, field, "missing")
    v = str(value).strip()
    if v == "UNKNOWN":
        raise ProjectPolicyIncomplete(ref, field, "unknown", value=v)
    if v not in allowed:
        raise ProjectPolicyIncomplete(ref, field, "invalid", value=v)
    return v


def resolve_project(
    conn,
    ref: str,
    *,
    for_access: Literal["read", "write"] = "read",
    registry_path=None,
) -> ResolvedProject:
    """Resolve ``ref`` (exact Registry ``project_id``) to a frozen :class:`ResolvedProject`.

    Step 37: the canonical identity + policy source is
    ``HERMES_PROJECT_REGISTRY.md`` (single source of truth per Step 36). This
    resolver REUSES the ``hermes_cli.vault_hub`` Registry parser — it does not
    parse the Registry a second time and does NOT read the ``projects.db``
    policy columns (``ownership_class`` / ``default_access_mode`` /
    ``canonical_source_type`` are now non-authoritative here; the schema is left
    intact). ``projects.db`` remains the operational workspace store for the
    GUI/CLI/TUI/kanban consumers and is untouched by this read path.

    Read-path only: write requests are always denied at this layer — the
    ``default_access_mode`` ceiling (READ_ONLY) is not authorization, and every
    write requires a separate, current, project-specific approval handled by a
    later layer. Fails closed on unknown projects, archived/inactive projects
    (explicit Registry ``status``), and any missing / UNKNOWN / invalid policy
    value. No fuzzy, prefix, cwd, or path-based inference; only the exact
    Registry ``project_id`` resolves.

    ``conn`` is retained for API compatibility with the Step 21 signature and is
    unused by this (Registry-backed) implementation. ``registry_path`` (optional)
    overrides the Registry file (tests); default is the live Registry.
    """
    import os as _os
    from pathlib import Path as _Path

    from hermes_cli import vault_hub as _vault_hub

    project_id = str(ref if ref is not None else "").strip()
    if not project_id:
        raise ProjectUnknown(ref)
    # Identity space is Registry project_id only: reject path/cwd/prefix-style refs.
    if "/" in project_id or "\\" in project_id or ".." in project_id or _os.sep in project_id or project_id.startswith("~"):
        raise ProjectUnknown(ref)

    # Exact-id Registry lookup via the shared parser (no second parser).
    _rp = _Path(registry_path) if registry_path is not None else None
    try:
        entry = _vault_hub.resolve_registry_entry(project_id, registry_path=_rp)
    except _vault_hub.VaultHubProjectNotFound:
        raise ProjectUnknown(ref)

    # Archived / inactive: explicit Registry ``status`` token only (never inferred).
    status = (entry.get("status") or "").strip().lower()
    if status in ("archived", "inactive"):
        raise ProjectArchived(ref)

    if for_access == "write":
        raise ProjectAccessDenied(ref, "write")

    name = (entry.get("project_name") or "").strip() or project_id

    # canonical_root comes from the Registry, never from DB primary_path.
    root = (entry.get("canonical_root") or "").strip()
    if not root:
        raise ProjectPolicyIncomplete(ref, "canonical_root", "missing")
    if root.upper() == "UNKNOWN":
        raise ProjectPolicyIncomplete(ref, "canonical_root", "unknown", value=root)

    ownership_class = _check_policy_field(ref, "ownership_class", entry.get("ownership_class"), _PROJECT_OWNERSHIP_CLASSES)
    canonical_source_type = _check_policy_field(ref, "canonical_source_type", entry.get("canonical_source_type"), _PROJECT_SOURCE_TYPES)
    default_access_mode = _check_policy_field(ref, "default_access_mode", entry.get("default_access_mode"), _PROJECT_ACCESS_MODES)

    return ResolvedProject(
        id=project_id,
        slug=project_id,
        name=name,
        canonical_root=root,
        canonical_source_type=canonical_source_type,
        ownership_class=ownership_class,
        default_access_mode=default_access_mode,
    )


def is_within(root: str, path: str) -> bool:
    """True iff real path ``path`` is inside real path ``root`` (root itself excluded is
    allowed: a file equal to the root counts as within).

    Read-only, no DB access, no writes. Uses ``os.path.realpath`` so symlinks are
    resolved before comparison — a symlink that escapes ``root`` yields False.
    Sibling paths that merely share a longer prefix (e.g. ``/a/bc`` vs ``/a/b``)
    are rejected by comparing on a separator boundary, not raw string prefix.
    Empty / invalid inputs return False.
    """
    try:
        r = os.path.realpath(str(root)) if root else ""
        p = os.path.realpath(str(path)) if path else ""
    except (OSError, TypeError, ValueError):
        return False
    if not r or not p:
        return False
    r = r.rstrip(os.sep) or os.sep
    return p == r or p.startswith(r + os.sep)
