"""Tests for the per-profile Projects store (hermes_cli/projects_db)."""

from __future__ import annotations

import os

import pytest

from hermes_cli import projects_db as pdb


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(db_path=tmp_path / "projects.db")
    try:
        yield c
    finally:
        c.close()






def test_discovery_policy_change_clears_only_discovered_rows(conn):
    project_id = pdb.create_project(conn, name="Explicit", folders=["/www/explicit"])
    pdb.record_discovered_repos(
        conn, [("/www/scanned", "scanned")], policy_key="policy-a"
    )

    assert pdb.reconcile_discovered_repos_policy(conn, "policy-b") is True
    assert pdb.list_discovered_repos(conn) == []
    assert pdb.get_project(conn, project_id) is not None
    assert pdb.get_discovery_policy_key(conn) == "policy-b"






def test_create_get_list(conn):
    pid = pdb.create_project(conn, name="Hermes Agent", folders=["/tmp/hermes"])
    proj = pdb.get_project(conn, pid)

    assert proj is not None
    assert proj.slug == "hermes-agent"
    assert proj.name == "Hermes Agent"
    # First folder becomes primary.
    assert proj.primary_path == os.path.abspath("/tmp/hermes")
    assert [f.path for f in proj.folders] == [os.path.abspath("/tmp/hermes")]
    assert proj.folders[0].is_primary is True

    # Lookup by slug too.
    assert pdb.get_project(conn, "hermes-agent").id == pid
    assert len(pdb.list_projects(conn)) == 1












def test_project_for_path_skips_archived(conn):
    pid = pdb.create_project(conn, name="P", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    assert pdb.project_for_path(conn, "/www/app/src") is None
    # Archived hidden from the default list but visible with include_archived.
    assert pdb.list_projects(conn) == []
    assert len(pdb.list_projects(conn, include_archived=True)) == 1

    pdb.restore_project(conn, pid)
    assert pdb.project_for_path(conn, "/www/app/src").id == pid


def test_create_dedups_by_primary_path(conn):
    pid = pdb.create_project(conn, name="GeoTrace", folders=["/www/geotrace"])

    # Same folder again (any name): refused, existing project named in error.
    with pytest.raises(ValueError, match="already belongs to project 'geotrace'"):
        pdb.create_project(conn, name="GeoTrace", folders=["/www/geotrace"])
    with pytest.raises(ValueError, match="already belongs"):
        pdb.create_project(conn, name="Other Name", primary_path="/www/geotrace")

    # Trailing-separator spelling of the same folder is still a duplicate.
    with pytest.raises(ValueError, match="already belongs"):
        pdb.create_project(conn, name="GeoTrace", primary_path="/www/geotrace/")

    # Deliberate duplicates stay possible.
    dup = pdb.create_project(
        conn, name="GeoTrace", folders=["/www/geotrace"], allow_duplicate_path=True
    )
    assert dup != pid
    assert len(pdb.list_projects(conn)) == 2


def test_create_dedup_ignores_archived_and_other_paths(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    # Archived project no longer blocks the path.
    fresh = pdb.create_project(conn, name="App", folders=["/www/app"])
    assert fresh != pid

    # Different folder is never a collision; folder-less projects don't match.
    pdb.create_project(conn, name="Elsewhere", folders=["/www/other"])
    pdb.create_project(conn, name="No Folder")


def test_find_by_primary_path(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])

    assert pdb.find_by_primary_path(conn, "/www/app").id == pid
    assert pdb.find_by_primary_path(conn, "/www/app/").id == pid
    assert pdb.find_by_primary_path(conn, "/www/nope") is None
    assert pdb.find_by_primary_path(conn, "") is None






def test_per_profile_isolation(tmp_path):
    # Two distinct DB paths stand in for two profiles' HERMES_HOME.
    a = pdb.connect(db_path=tmp_path / "a" / "projects.db")
    b = pdb.connect(db_path=tmp_path / "b" / "projects.db")
    try:
        pdb.create_project(a, name="Only In A", folders=["/a"])
        pdb.record_discovered_repos(a, [("/a/scanned", "scanned")])

        assert [p.slug for p in pdb.list_projects(a)] == ["only-in-a"]
        assert pdb.list_projects(b) == []
        assert [row["root"] for row in pdb.list_discovered_repos(a)] == [
            os.path.abspath("/a/scanned")
        ]
        assert pdb.list_discovered_repos(b) == []
    finally:
        a.close()
        b.close()


# ── Step 25: operator policy write (set_project_policy) ─────────────────────

def _complete(conn, name="Policy Project"):
    """Create a folder-backed project (so primary_path is set) — policy still NULL."""
    return pdb.create_project(conn, name=name, folders=["/opt/proj"])


def test_set_all_three_policy_fields(conn):
    pid = _complete(conn)
    assert pdb.set_project_policy(
        conn, pid, ownership_class="HERMES_OWNED",
        default_access_mode="READ_ONLY", canonical_source_type="LOCAL_FILE",
    ) is True
    p = pdb.get_project(conn, pid)
    assert (p.ownership_class, p.default_access_mode, p.canonical_source_type) == (
        "HERMES_OWNED", "READ_ONLY", "LOCAL_FILE")


def test_partial_update_leaves_others(conn):
    pid = _complete(conn)
    pdb.set_project_policy(conn, pid, ownership_class="EXTERNAL_OWNED")
    p = pdb.get_project(conn, pid)
    # Only ownership changed; the other two remain NULL (untouched).
    assert p.ownership_class == "EXTERNAL_OWNED"
    assert p.default_access_mode is None
    assert p.canonical_source_type is None
    # Now a second partial call fills one more, leaving ownership intact.
    pdb.set_project_policy(conn, pid, canonical_source_type="EXTERNAL_GIT_REPOSITORY")
    p = pdb.get_project(conn, pid)
    assert p.ownership_class == "EXTERNAL_OWNED"
    assert p.canonical_source_type == "EXTERNAL_GIT_REPOSITORY"
    assert p.default_access_mode is None


def test_unknown_stored_when_explicit(conn):
    pid = _complete(conn)
    assert pdb.set_project_policy(conn, pid, ownership_class="UNKNOWN",
                                  canonical_source_type="UNKNOWN") is True
    p = pdb.get_project(conn, pid)
    assert p.ownership_class == "UNKNOWN"
    assert p.canonical_source_type == "UNKNOWN"
    # But default_access_mode has no UNKNOWN; it stays NULL until explicitly set.
    assert p.default_access_mode is None


def test_invalid_ownership_rejected(conn):
    pid = _complete(conn)
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, ownership_class="SOMEONE_ELSE")
    assert pdb.get_project(conn, pid).ownership_class is None


def test_read_write_access_rejected(conn):
    pid = _complete(conn)
    with pytest.raises(ValueError, match="invalid default_access_mode"):
        pdb.set_project_policy(conn, pid, default_access_mode="READ_WRITE")
    assert pdb.get_project(conn, pid).default_access_mode is None


def test_lowercase_value_rejected(conn):
    pid = _complete(conn)
    # Exact, case-sensitive: lowercase is an invalid value, not a normalization target.
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, ownership_class="hermes_owned")
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, canonical_source_type="local_file")


def test_empty_string_rejected(conn):
    pid = _complete(conn)
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, ownership_class="")
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, ownership_class="   ")


def test_missing_project_rejected(conn):
    with pytest.raises(ValueError, match="no such project"):
        pdb.set_project_policy(conn, "ghost", ownership_class="HERMES_OWNED")


def test_no_fields_returns_false(conn):
    pid = _complete(conn)
    assert pdb.set_project_policy(conn, pid) is False
    p = pdb.get_project(conn, pid)
    assert (p.ownership_class, p.default_access_mode, p.canonical_source_type) == (None, None, None)


def test_only_policy_columns_change(conn):
    pid = _complete(conn)
    before = pdb.get_project(conn, pid)
    pdb.set_project_policy(conn, pid, ownership_class="HERMES_OWNED")
    after = pdb.get_project(conn, pid)
    # Identity/relationship fields are untouched.
    assert after.id == before.id
    assert after.slug == before.slug
    assert after.name == before.name
    assert after.primary_path == before.primary_path
    assert [f.path for f in after.folders] == [f.path for f in before.folders]
    assert after.board_slug == before.board_slug


def test_resolve_project_is_registry_backed_not_db_backed(conn, tmp_path):
    # Step 37: the resolver reads canonical identity/policy from the Registry,
    # NOT from projects.db. A DB row with a complete policy is therefore NOT
    # enough for Worker-Core resolution — it is a workspace project, not a
    # Registry project. The DB pid fails closed (ProjectUnknown).
    pid = _complete(conn)
    pdb.set_project_policy(
        conn, pid, ownership_class="HERMES_OWNED",
        default_access_mode="READ_ONLY", canonical_source_type="LOCAL_FILE",
    )
    # The DB row is fully populated, yet...
    assert pdb.get_project(conn, pid).ownership_class == "HERMES_OWNED"
    with pytest.raises(pdb.ProjectUnknown):
        pdb.resolve_project(conn, pid)
    # ...and the SAME reference resolves fine against a matching Registry.
    reg = tmp_path / "HERMES_PROJECT_REGISTRY.md"
    reg.write_text(
        "<!-- registry-entry -->\n"
        f"project_id: {pid}\n"
        "project_name: From Registry\n"
        "ownership_class: HERMES_OWNED\n"
        "canonical_source_type: LOCAL_FILE\n"
        "default_access_mode: READ_ONLY\n"
        "canonical_root: /registry/canonical\n"
        "status: ACTIVE\n"
        "<!-- /registry-entry -->\n",
        encoding="utf-8",
    )
    r = pdb.resolve_project(conn, pid, registry_path=reg)
    assert r.ownership_class == "HERMES_OWNED"
    assert r.default_access_mode == "READ_ONLY"
    assert r.canonical_source_type == "LOCAL_FILE"
    assert r.canonical_root == "/registry/canonical"


def test_set_policy_rejects_unknown_access_value(conn):
    # UNKNOWN is NOT a valid default_access_mode (unlike ownership/source).
    pid = _complete(conn)
    with pytest.raises(ValueError):
        pdb.set_project_policy(conn, pid, default_access_mode="UNKNOWN")
