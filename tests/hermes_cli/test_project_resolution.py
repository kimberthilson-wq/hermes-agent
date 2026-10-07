"""Tests for the Registry-backed project resolution contract (Step 37).

Read-path only. The canonical identity/policy source is
``HERMES_PROJECT_REGISTRY.md``; this suite uses a temporary Registry file (and a
temporary DB where relevant) — it never touches the live Registry or live
projects.db, and performs no writes of any kind.

``resolve_project`` reuses the ``hermes_cli.vault_hub`` Registry parser; these
tests exercise that shared path, not a second parser.

Live-Registry entries (trion_application, etsy_atlas, trion_content, ...)
cannot be asserted in this isolated test environment: the test harness
redirects ``HERMES_HOME`` to a temp root (see ``tests/home_io_guard.py``), so
``_vault_root()`` does not point at the real knowledge-vault. Live Registry
behaviour is exercised separately, outside the isolated suite (Step 36 receipt).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from hermes_cli import projects_db as pdb
from hermes_cli import vault_hub as vhub


# ── temporary Registry fixtures ─────────────────────────────────────────────


def _write_registry(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "HERMES_PROJECT_REGISTRY.md"
    p.write_text(text, encoding="utf-8")
    return p


_VALID_REGISTRY = """\
# Registry (test)

<!-- registry-entry -->
project_id: valid_project
project_name: Valid Project
ownership_class: HERMES_OWNED
canonical_source_type: LOCAL_FILE
default_access_mode: READ_ONLY
canonical_root: /opt/valid
status: ACTIVE
vault_hub: projects/valid/README.md
<!-- /registry-entry -->
"""

_MISSING_OWNERSHIP = _VALID_REGISTRY.replace(
    "ownership_class: HERMES_OWNED", "ownership_class: UNKNOWN"
).replace("valid_project", "missing_ownership").replace("Valid Project", "Missing Ow")

_MISSING_SOURCE = _VALID_REGISTRY.replace(
    "canonical_source_type: LOCAL_FILE", "canonical_source_type: UNKNOWN"
).replace("valid_project", "missing_source").replace("Valid Project", "Missing Src")

_MISSING_ACCESS = _VALID_REGISTRY.replace(
    "default_access_mode: READ_ONLY", "default_access_mode: UNKNOWN"
).replace("valid_project", "missing_access").replace("Valid Project", "Missing Acc")

_ARCHIVED = _VALID_REGISTRY.replace(
    "status: ACTIVE", "status: ARCHIVED"
).replace("valid_project", "archived_project").replace("Valid Project", "Archived P")

_DUPLICATE = _VALID_REGISTRY + _VALID_REGISTRY  # same project_id twice

_NO_CANONICAL_ROOT = _VALID_REGISTRY.replace(
    "canonical_root: /opt/valid", "canonical_root: UNKNOWN"
).replace("valid_project", "no_root").replace("Valid Project", "No Root")


@pytest.fixture
def registry(tmp_path):
    return _write_registry(tmp_path, _VALID_REGISTRY)


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(db_path=tmp_path / "projects.db")
    try:
        yield c
    finally:
        c.close()


# ── valid resolution (Registry-backed) ──────────────────────────────────────


def test_valid_registry_project_resolves(registry, conn):
    r = pdb.resolve_project(conn, "valid_project", registry_path=registry)
    assert r.id == "valid_project"
    assert r.name == "Valid Project"
    assert r.canonical_root == "/opt/valid"
    assert r.ownership_class == "HERMES_OWNED"
    assert r.canonical_source_type == "LOCAL_FILE"
    assert r.default_access_mode == "READ_ONLY"


def test_valid_project_full_policy(registry, conn):
    r = pdb.resolve_project(conn, "valid_project", for_access="read", registry_path=registry)
    assert r.ownership_class == "HERMES_OWNED"
    assert r.canonical_source_type == "LOCAL_FILE"
    assert r.default_access_mode == "READ_ONLY"


def test_registry_project_without_db_row_resolves(tmp_path, registry):
    # Step 36: the Registry is canonical. No DB row is required.
    c = pdb.connect(db_path=tmp_path / "empty.db")
    try:
        r = pdb.resolve_project(c, "valid_project", registry_path=registry)
        assert r.id == "valid_project"
        # confirm the live DB genuinely has no row for this identity
        assert pdb.get_project(c, "valid_project") is None
    finally:
        c.close()


def test_canonical_root_from_registry_never_db(conn, tmp_path):
    # DB row exists with a DIFFERENT primary_path; the Registry canonical_root
    # must win.
    pid = pdb.create_project(conn, name="Valid Project", slug="valid_project", folders=["/db/other"])
    reg = _write_registry(
        tmp_path,
        _VALID_REGISTRY.replace("canonical_root: /opt/valid", "canonical_root: /registry/root"),
    )
    r = pdb.resolve_project(conn, "valid_project", registry_path=reg)
    assert r.canonical_root == "/registry/root"
    # the DB primary_path (/db/other) must NOT leak in.
    assert r.canonical_root != os.path.abspath("/db/other")
    # a DB row exists, yet is not used
    assert pdb.get_project(conn, pid) is not None


# ── fail-closed: identity ───────────────────────────────────────────────────


def test_unknown_project_fails_closed(registry, conn):
    with pytest.raises(pdb.ProjectUnknown) as ei:
        pdb.resolve_project(conn, "no-such-project", registry_path=registry)
    assert ei.value.ref == "no-such-project"


def test_fuzzy_prefix_project_id_fails_closed(registry, conn):
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(conn, "valid", registry_path=registry)


def test_path_style_ref_fails_closed(registry, conn):
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(conn, "/opt/valid", registry_path=registry)


def test_dotdot_ref_fails_closed(registry, conn):
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(conn, "valid_project/../other", registry_path=registry)


def test_db_only_project_does_not_resolve(registry, conn):
    # A workspace project that exists in the DB but NOT in the Registry must not
    # become a Worker-Core (Registry) project.
    pid = pdb.create_project(conn, name="DB Only", slug="db-only")
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(conn, "db-only", registry_path=registry)
    # And it still exists as a workspace (unchanged) — the DB surface is intact.
    assert pdb.get_project(conn, pid) is not None


# ── fail-closed: policy completeness ────────────────────────────────────────


def _expect_incomplete(tmp_path, text, ref, field):
    reg = _write_registry(tmp_path, text)
    with pytest.raises(pdb.ProjectPolicyIncomplete) as ei:
        pdb.resolve_project(None, ref, registry_path=reg)
    return ei.value


def test_unknown_ownership_fails_closed(tmp_path):
    e = _expect_incomplete(tmp_path, _MISSING_OWNERSHIP, "missing_ownership", "ownership_class")
    assert (e.field, e.reason) == ("ownership_class", "unknown")


def test_missing_source_fails_closed(tmp_path):
    e = _expect_incomplete(tmp_path, _MISSING_SOURCE, "missing_source", "canonical_source_type")
    assert (e.field, e.reason) == ("canonical_source_type", "unknown")


def test_missing_access_fails_closed(tmp_path):
    e = _expect_incomplete(tmp_path, _MISSING_ACCESS, "missing_access", "default_access_mode")
    assert (e.field, e.reason) == ("default_access_mode", "unknown")


def test_missing_canonical_root_fails_closed(tmp_path):
    e = _expect_incomplete(tmp_path, _NO_CANONICAL_ROOT, "no_root", "canonical_root")
    assert (e.field, e.reason) == ("canonical_root", "unknown")


def test_invalid_ownership_fails_closed(tmp_path):
    reg = _write_registry(
        tmp_path,
        _VALID_REGISTRY.replace("ownership_class: HERMES_OWNED", "ownership_class: SOMEONE_ELSE")
        .replace("valid_project", "bad_own").replace("Valid Project", "Bad Ow"),
    )
    with pytest.raises(pdb.ProjectPolicyIncomplete) as ei:
        pdb.resolve_project(None, "bad_own", registry_path=reg)
    assert (ei.value.field, ei.value.reason) == ("ownership_class", "invalid")


# ── access modes ────────────────────────────────────────────────────────────


def test_write_always_denied(registry, conn):
    with pytest.raises(pdb.ProjectAccessDenied) as ei:
        pdb.resolve_project(conn, "valid_project", for_access="write", registry_path=registry)
    assert (ei.value.ref, ei.value.requested_access) == ("valid_project", "write")


def test_read_with_read_only_succeeds(registry, conn):
    r = pdb.resolve_project(conn, "valid_project", for_access="read", registry_path=registry)
    assert r.default_access_mode == "READ_ONLY"


# ── archived / status ───────────────────────────────────────────────────────


def test_archived_registry_project_fails_closed(tmp_path):
    reg = _write_registry(tmp_path, _ARCHIVED)
    with pytest.raises(pdb.ProjectArchived):
        pdb.resolve_project(None, "archived_project", registry_path=reg)


def test_inactive_status_fails_closed(tmp_path):
    reg = _write_registry(
        tmp_path,
        _VALID_REGISTRY.replace("status: ACTIVE", "status: INACTIVE")
        .replace("valid_project", "inactive_p").replace("Valid Project", "Inactive P"),
    )
    with pytest.raises(pdb.ProjectArchived):
        pdb.resolve_project(None, "inactive_p", registry_path=reg)


# ── duplicate identity ──────────────────────────────────────────────────────


def test_duplicate_registry_project_id_fails_closed(tmp_path):
    reg = _write_registry(tmp_path, _DUPLICATE)
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(None, "valid_project", registry_path=reg)


# ── API contract preserved ──────────────────────────────────────────────────


def test_resolved_project_is_frozen(registry, conn):
    r = pdb.resolve_project(conn, "valid_project", registry_path=registry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.name = "mutated"


def test_all_errors_share_base_type(registry, conn):
    for exc in (pdb.ProjectUnknown, pdb.ProjectArchived, pdb.ProjectPolicyIncomplete, pdb.ProjectAccessDenied):
        assert issubclass(exc, pdb.ProjectResolutionError)
    with pytest.raises(pdb.ProjectResolutionError):
        pdb.resolve_project(conn, "ghost", registry_path=registry)
    with pytest.raises(pdb.ProjectResolutionError):
        pdb.resolve_project(conn, "valid_project", for_access="write", registry_path=registry)


# ── shared Registry source with the Vault retrieval path ───────────────────


def test_resolver_shares_registry_source_with_vault_hub(tmp_path):
    # Build a HERMES_HOME layout the vault_hub containment check can accept.
    hermes_home = tmp_path / "home"
    vault_root = hermes_home / "knowledge-vault"
    hub_file = vault_root / "projects" / "valid" / "README.md"
    hub_file.parent.mkdir(parents=True, exist_ok=True)
    hub_file.write_text("vault hub body", encoding="utf-8")

    reg = _write_registry(
        tmp_path,
        _VALID_REGISTRY.replace("vault_hub: projects/valid/README.md", "vault_hub: projects/valid/README.md"),
    )

    resolved = pdb.resolve_project(None, "valid_project", registry_path=reg)
    hub = vhub.read_project_vault_hub(
        "valid_project", registry_path=reg, hermes_home=hermes_home,
    )
    # Both come from the SAME Registry entry.
    assert resolved.id == hub.project_id
    assert hub.content == "vault hub body"
    # vault_hub binding is owned by the Registry (not the DB).
    assert hub.provenance["vault_hub"] == "projects/valid/README.md"
    assert hub.provenance["canonical_root"] == "/opt/valid"
    assert hub.provenance["ownership_class"] == "HERMES_OWNED"


@pytest.mark.parametrize("field,value", [
    ("project_id", "valid_project"),
    ("vault_hub", "projects/other/README.md"),
    ("ownership_class", "EXTERNAL_OWNED"),
    ("default_access_mode", "READ_ONLY"),
])
def test_duplicate_fields_do_not_authorize_resolution(tmp_path, field, value):
    text = _VALID_REGISTRY.replace(
        "<!-- /registry-entry -->",
        f"{field}: {value}\n<!-- /registry-entry -->", 1,
    )
    registry = _write_registry(tmp_path, text)
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(None, "valid_project", registry_path=registry)
