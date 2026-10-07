"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


def _run(argv):
    """Build the project subparser, parse argv, and dispatch. Returns rc."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = projects_cmd.build_parser(sub)
    p.set_defaults(func=projects_cmd.projects_command)
    args = parser.parse_args(["project", *argv])
    return projects_cmd.projects_command(args)


def test_create_list_show(capsys, tmp_path):
    assert _run(["create", "My App", str(tmp_path), "--use"]) == 0
    out = capsys.readouterr().out
    assert "Created project" in out

    with pdb.connect_closing() as conn:
        projects = pdb.list_projects(conn)
        assert len(projects) == 1
        assert projects[0].name == "My App"
        # --use set it active.
        assert pdb.get_active_id(conn) == projects[0].id

    assert _run(["list"]) == 0
    assert "my-app" in capsys.readouterr().out

    assert _run(["show", "my-app"]) == 0
    assert "My App" in capsys.readouterr().out




def test_rename_and_archive(tmp_path):
    _run(["create", "Old Name", str(tmp_path)])
    assert _run(["rename", "old-name", "New Name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "old-name").name == "New Name"

    assert _run(["archive", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.list_projects(conn) == []
        assert len(pdb.list_projects(conn, include_archived=True)) == 1

    assert _run(["restore", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert len(pdb.list_projects(conn)) == 1


# ── Step 25: hermes project set-policy ───────────────────────────────────────

def _create_policy_project(tmp_path, name="Policy CLI"):
    """Create a folder-backed project (primary_path set) and return its slug."""
    _run(["create", name, str(tmp_path)])
    with pdb.connect_closing() as conn:
        return pdb.list_projects(conn)[0].slug


def test_set_policy_all_fields(capsys, tmp_path):
    slug = _create_policy_project(tmp_path)
    rc = _run(["set-policy", slug, "--ownership", "HERMES_OWNED",
               "--access", "READ_ONLY", "--source", "LOCAL_FILE"])
    assert rc == 0
    assert "Policy set for" in capsys.readouterr().out
    with pdb.connect_closing() as conn:
        p = pdb.get_project(conn, slug)
        assert (p.ownership_class, p.default_access_mode, p.canonical_source_type) == (
            "HERMES_OWNED", "READ_ONLY", "LOCAL_FILE")


def test_set_policy_partial(capsys, tmp_path):
    slug = _create_policy_project(tmp_path)
    assert _run(["set-policy", slug, "--ownership", "EXTERNAL_OWNED"]) == 0
    capsys.readouterr()
    with pdb.connect_closing() as conn:
        p = pdb.get_project(conn, slug)
        assert p.ownership_class == "EXTERNAL_OWNED"
        assert p.default_access_mode is None
        assert p.canonical_source_type is None


def test_set_policy_exact_id_and_slug_resolution(tmp_path):
    slug = _create_policy_project(tmp_path, name="Ref Lookup")
    with pdb.connect_closing() as conn:
        pid = pdb.get_project(conn, slug).id
    # Both the id and the slug resolve to the same project.
    assert _run(["set-policy", slug, "--ownership", "HERMES_OWNED"]) == 0
    assert _run(["set-policy", pid, "--access", "READ_ONLY"]) == 0
    with pdb.connect_closing() as conn:
        p = pdb.get_project(conn, slug)
        assert p.ownership_class == "HERMES_OWNED"
        assert p.default_access_mode == "READ_ONLY"


def test_set_policy_missing_project(capsys, tmp_path):
    rc = _run(["set-policy", "no-such-project", "--ownership", "HERMES_OWNED"])
    assert rc == 1
    assert "no such project" in capsys.readouterr().err


def test_set_policy_no_flags_errors(capsys, tmp_path):
    slug = _create_policy_project(tmp_path)
    rc = _run(["set-policy", slug])
    assert rc == 1
    assert "at least one" in capsys.readouterr().err


def test_set_policy_invalid_value_errors(capsys, tmp_path):
    slug = _create_policy_project(tmp_path)
    rc = _run(["set-policy", slug, "--access", "READ_WRITE"])
    assert rc == 2
    assert "invalid" in capsys.readouterr().err
    # Nothing was written.
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, slug).default_access_mode is None




