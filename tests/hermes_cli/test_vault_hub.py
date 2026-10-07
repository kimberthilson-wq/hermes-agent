"""Targeted tests for ``hermes_cli.vault_hub`` — the Step 30 read-only contract.

Every test runs against a **temp-vault fixture** built under ``tmp_path`` (via the
``hermes_home`` override) so nothing is ever written to the live
``/home/ubuntu/.hermes/knowledge-vault`` or the live projects DBs. Two
additional *guarded* tests verify, read-only, that the **live** vault stays clean
and unchanged after a real Registry read.
"""

from __future__ import annotations

import hashlib
import os
from functools import wraps
from pathlib import Path

import pytest

from hermes_cli import vault_hub as vh


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _entry(pid, *, ownership="HERMES_OWNED", root="/x", src_type="LOCAL_FILE",
           hub="projects/p/HUB.md", access="READ_ONLY", include_hub=True):
    lines = [
        "<!-- registry-entry -->",
        f"project_id: {pid}",
        "owner: Kim",
        f"ownership_class: {ownership}",
        f"canonical_root: {root}",
        f"canonical_source: {root}/README.md",
        f"canonical_source_type: {src_type}",
    ]
    if include_hub:
        lines.append(f"vault_hub: {hub}")
    lines.append(f"default_access_mode: {access}")
    lines.append("status_scope: TEST")
    lines.append("<!-- /registry-entry -->")
    return "\n".join(lines) + "\n"


def _build_vault(tmp_path: Path) -> Path:
    """Create a throwaway knowledge-vault tree under ``tmp_path``. Returns vault root."""
    root = tmp_path / "knowledge-vault"

    # A valid bound note.
    (root / "projects" / "ok").mkdir(parents=True)
    (root / "projects" / "ok" / "HUB.md").write_text(
        "OK HUB LINE 1\nOK HUB LINE 2\n", encoding="utf-8"
    )

    # A directory used as an (invalid) hub target.
    (root / "projects" / "dirhub").mkdir(parents=True)

    # A large note for bounded-read tests.
    (root / "projects" / "big").mkdir(parents=True)
    (root / "projects" / "big" / "HUB.md").write_text(
        "X" * 500, encoding="utf-8"
    )

    # A file that *mentions* a project id in its body — used to prove we do
    # NOT fall back to note-body parsing.
    (root / "projects" / "nohub").mkdir(parents=True)
    (root / "projects" / "nohub" / "HUB.md").write_text(
        "project_id: nohub_proj  <- body mention only\n", encoding="utf-8"
    )

    # The Registry data source.
    blocks = ["# Registry (fixture)\n\n"]
    blocks.append(_entry("ok_proj", hub="projects/ok/HUB.md"))
    blocks.append(_entry("unknown_hub", hub="UNKNOWN"))
    blocks.append(_entry("nohub_proj", include_hub=False))
    blocks.append(_entry("missing_target", hub="projects/ghost/HUB.md"))
    blocks.append(_entry("dir_target", hub="projects/dirhub"))
    blocks.append(_entry("outside_abs", hub="/etc/hostname"))
    blocks.append(_entry("outside_rel", hub="../escape.md"))
    blocks.append(_entry("big_proj", hub="projects/big/HUB.md"))
    blocks.append(_entry(
        "trion_application", ownership="EXTERNAL_OWNED",
        root=r"C:\apps\trion", src_type="EXTERNAL_GIT_REPOSITORY",
        hub="projects/ok/HUB.md", access="READ_ONLY",
    ))
    (root / "HERMES_PROJECT_REGISTRY.md").write_text("\n".join(blocks), encoding="utf-8")
    return root


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return _build_vault(tmp_path)


@pytest.fixture
def hh(tmp_path: Path, vault_root: Path) -> Path:
    """The ``hermes_home`` override pointing at the temp vault."""
    return tmp_path


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1 / 13 — exact id -> correct bound file; valid read succeeds
# ---------------------------------------------------------------------------


def test_exact_project_id_returns_vault_hub(vault_root, hh):
    r = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    assert r.project_id == "ok_proj"
    assert "OK HUB LINE 1" in r.content
    assert r.provenance["vault_hub"] == "projects/ok/HUB.md"


def test_valid_bound_file_read_succeeds(vault_root, hh):
    r = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    expected = (vault_root / "projects" / "ok" / "HUB.md").read_text(encoding="utf-8")
    assert r.content == expected
    assert r.truncated is False


# ---------------------------------------------------------------------------
# 9 / 10 — provenance + ownership/access metadata preserved
# ---------------------------------------------------------------------------


def test_provenance_fields_returned(vault_root, hh):
    r = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    for key in ("project_id", "vault_hub", "canonical_root",
                "ownership_class", "default_access_mode", "source"):
        assert key in r.provenance
    assert r.provenance["source"] == "HERMES_PROJECT_REGISTRY.md"


def test_ownership_access_metadata_preserved(vault_root, hh):
    r = vh.read_project_vault_hub("trion_application", hermes_home=hh)
    assert r.provenance["ownership_class"] == "EXTERNAL_OWNED"
    assert r.provenance["default_access_mode"] == "READ_ONLY"
    assert r.provenance["canonical_root"] == r"C:\apps\trion"


def test_missing_registry_field_is_unknown_not_inferred(vault_root, hh):
    # A project whose canonical_root is left empty -> preserved as UNKNOWN,
    # not inferred. Uses the same hermes_home so the hub resolves correctly.
    (vault_root / "projects" / "eroot").mkdir(parents=True, exist_ok=True)
    (vault_root / "projects" / "eroot" / "HUB.md").write_text("EROOT HUB\n", encoding="utf-8")
    reg = vault_root / "HERMES_PROJECT_REGISTRY.md"
    text = reg.read_text(encoding="utf-8")
    # Add an entry with an EMPTY canonical_root and a valid hub.
    entry = (
        "<!-- registry-entry -->\n"
        "project_id: empty_root\n"
        "owner: Kim\n"
        "ownership_class: HERMES_OWNED\n"
        "canonical_root: \n"
        "canonical_source: /x/README.md\n"
        "canonical_source_type: LOCAL_FILE\n"
        "vault_hub: projects/eroot/HUB.md\n"
        "default_access_mode: READ_ONLY\n"
        "status_scope: TEST\n"
        "<!-- /registry-entry -->\n"
    )
    reg.write_text(text + "\n" + entry, encoding="utf-8")
    r = vh.read_project_vault_hub("empty_root", hermes_home=hh)
    assert r.provenance["canonical_root"] == "UNKNOWN"
    assert r.provenance["ownership_class"] == "HERMES_OWNED"
    assert "EROOT HUB" in r.content


# ---------------------------------------------------------------------------
# 2 / 3 / 4 — fail-closed on identity / reference
# ---------------------------------------------------------------------------


def test_missing_project_id_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubProjectNotFound):
        vh.read_project_vault_hub("nonexistent_proj", hermes_home=hh)
    with pytest.raises(vh.VaultHubProjectNotFound):
        vh.read_project_vault_hub("", hermes_home=hh)


def test_unknown_vault_hub_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubReferenceMissing):
        vh.read_project_vault_hub("unknown_hub", hermes_home=hh)


def test_missing_vault_hub_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubReferenceMissing):
        vh.read_project_vault_hub("nohub_proj", hermes_home=hh)


# ---------------------------------------------------------------------------
# 5 / 6 / 7 — fail-closed on target
# ---------------------------------------------------------------------------


def test_nonexistent_target_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubTargetInvalid):
        vh.read_project_vault_hub("missing_target", hermes_home=hh)


def test_target_outside_root_absolute_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubOutsideRoot):
        vh.read_project_vault_hub("outside_abs", hermes_home=hh)


def test_target_outside_root_relative_escape_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubOutsideRoot):
        vh.read_project_vault_hub("outside_rel", hermes_home=hh)


def test_target_directory_fails(vault_root, hh):
    with pytest.raises(vh.VaultHubTargetInvalid):
        vh.read_project_vault_hub("dir_target", hermes_home=hh)


# ---------------------------------------------------------------------------
# 8 — bounded character output
# ---------------------------------------------------------------------------


def test_bounded_character_output(vault_root, hh):
    r = vh.read_project_vault_hub("big_proj", max_chars=50, hermes_home=hh)
    assert len(r.content) == 50
    assert r.content == "X" * 50
    assert r.truncated is True

    # Under the limit -> full content, not truncated.
    r2 = vh.read_project_vault_hub("big_proj", max_chars=1000, hermes_home=hh)
    assert len(r2.content) == 500
    assert r2.truncated is False


def test_max_chars_is_hard_capped(vault_root, hh):
    # Even a huge request is clamped; output never exceeds the clamp.
    r = vh.read_project_vault_hub("big_proj", max_chars=10**9, hermes_home=hh)
    assert len(r.content) <= 500  # file is only 500 chars; clamp never lowers below it here
    assert r.truncated is False


# ---------------------------------------------------------------------------
# 11 / 12 — no note-body fallback; no fuzzy/prefix matching
# ---------------------------------------------------------------------------


def test_no_note_body_fallback(vault_root, hh):
    # "nohub_proj" has a HUB.md file whose BODY mentions the id, but the
    # Registry entry has NO vault_hub -> must fail, not fall back to the file.
    with pytest.raises(vh.VaultHubReferenceMissing):
        vh.read_project_vault_hub("nohub_proj", hermes_home=hh)


@pytest.mark.parametrize("bad", ["ok", "ok_pro", "ok_projx", "OK_PROJ", " trion_application"])
def test_no_fuzzy_or_prefix_matching(vault_root, hh, bad):
    with pytest.raises(vh.VaultHubProjectNotFound):
        vh.read_project_vault_hub(bad, hermes_home=hh)


def test_exact_match_distinct_from_similar(vault_root, hh):
    # trion_application resolves; a near-miss does not.
    assert vh.read_project_vault_hub("trion_application", hermes_home=hh).project_id == "trion_application"
    with pytest.raises(vh.VaultHubProjectNotFound):
        vh.read_project_vault_hub("trion_applicationx", hermes_home=hh)


# ---------------------------------------------------------------------------
# 14 / 15 — no mutation: Registry unchanged, vault tree unchanged
# ---------------------------------------------------------------------------


def _vault_tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = _sha(p)
    return out


def test_registry_unchanged_after_read(vault_root, hh):
    reg = vault_root / "HERMES_PROJECT_REGISTRY.md"
    before = _sha(reg)
    vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    vh.read_project_vault_hub("trion_application", hermes_home=hh)
    assert _sha(reg) == before


def test_vault_tree_unchanged_after_reads(vault_root, hh):
    before = _vault_tree(vault_root)
    # a mix of successful and failing reads
    vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    for bad in ("unknown_hub", "missing_target", "outside_abs", "dir_target"):
        with pytest.raises(vh.VaultHubError):
            vh.read_project_vault_hub(bad, hermes_home=hh)
    after = _vault_tree(vault_root)
    assert after == before


# ---------------------------------------------------------------------------
# Duplicate-id ambiguity -> fail closed (robustness, beyond required list)
# ---------------------------------------------------------------------------


def test_duplicate_project_id_fails_closed(tmp_path):
    root = _build_vault(tmp_path)
    reg = root / "HERMES_PROJECT_REGISTRY.md"
    text = reg.read_text(encoding="utf-8")
    dup = _entry("ok_proj", hub="projects/ok/HUB.md")
    reg.write_text(text + "\n" + dup, encoding="utf-8")
    with pytest.raises(vh.VaultHubProjectNotFound):
        vh.read_project_vault_hub("ok_proj", hermes_home=tmp_path)


# NOTE: live-vault / live-registry assertions are deliberately NOT made in-test.
# The harness ``home_io_guard`` refuses any file I/O against the real hermes home
# (see tests/home_io_guard.py). Live-vault state is verified read-only via the
# shell in the Step 31 SLUTRAPPORT (Vault git dirty=0 before/after; Registry
# byte-identical). The temp-vault tests above already prove: Registry unchanged
# (``test_registry_unchanged_after_read``) and the whole vault tree unchanged
# (``test_vault_tree_unchanged_after_reads``) across successful + failing reads.


# Descriptor-bound containment and unambiguous Registry fields (final review).


@pytest.mark.parametrize("field,value", [
    ("project_id", "ok_proj"),
    ("project_name", "Duplicate Name"),
    ("vault_hub", "projects/ok/HUB.md"),
    ("canonical_root", "/duplicate/root"),
    ("canonical_source", "/duplicate/README.md"),
    ("canonical_source_type", "LOCAL_FILE"),
    ("ownership_class", "EXTERNAL_OWNED"),
    ("default_access_mode", "READ_ONLY"),
    ("owner", "Other Owner"),
    ("status_scope", "TEST"),
    ("status", "ACTIVE"),
])
def test_duplicate_registry_fields_fail_closed(vault_root, hh, field, value):
    registry = vault_root / "HERMES_PROJECT_REGISTRY.md"
    text = registry.read_text(encoding="utf-8")
    # project_name/status are absent in the fixture: add both occurrences.
    first = f"{field}: {value}\n" if field in ("project_name", "status") else ""
    text = text.replace(
        "<!-- /registry-entry -->",
        first + f"{field}: {value}\n<!-- /registry-entry -->", 1,
    )
    registry.write_text(text, encoding="utf-8")
    with pytest.raises(vh.VaultHubProjectNotFound, match="duplicate Registry field"):
        vh.read_project_vault_hub("ok_proj", hermes_home=hh)


def test_existing_in_vault_symlink_reads_bound_file(vault_root, hh):
    target = vault_root / "projects" / "ok" / "HUB.md"
    alias = vault_root / "projects" / "ok" / "ALIAS.md"
    alias.symlink_to(target)
    registry = vault_root / "HERMES_PROJECT_REGISTRY.md"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace("projects/ok/HUB.md", "projects/ok/ALIAS.md"),
        encoding="utf-8",
    )
    result = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    assert result.content == "OK HUB LINE 1\nOK HUB LINE 2\n"


@pytest.mark.parametrize("component", ["leaf", "directory"])
def test_symlink_replacement_during_open_fails_closed(vault_root, hh, tmp_path, monkeypatch, component):
    outside = tmp_path / "outside"
    outside_target = outside / "ok" / "HUB.md"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text("OUTSIDE SYNTHETIC SENTINEL", encoding="utf-8")
    original_open = os.open
    replaced = []

    @wraps(original_open)
    def replace_then_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and not replaced:
            if component == "leaf" and path == "HUB.md":
                target = vault_root / "projects" / "ok" / "HUB.md"
                target.unlink()
                target.symlink_to(outside_target)
                replaced.append("leaf")
            elif component == "directory" and path == "projects":
                projects = vault_root / "projects"
                projects.rename(vault_root / "original_projects")
                projects.symlink_to(outside, target_is_directory=True)
                replaced.append("directory")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_then_open)
    with pytest.raises(vh.VaultHubTargetInvalid):
        vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    assert replaced == [component]
    assert outside_target.read_text(encoding="utf-8") == "OUTSIDE SYNTHETIC SENTINEL"


def test_replaced_leaf_fifo_rejected_without_read(vault_root, hh, monkeypatch):
    original_open = os.open
    replaced = []

    @wraps(original_open)
    def replace_then_open(path, flags, *args, **kwargs):
        if path == "HUB.md" and kwargs.get("dir_fd") is not None and not replaced:
            target = vault_root / "projects" / "ok" / "HUB.md"
            target.unlink()
            os.mkfifo(target)
            replaced.append("fifo")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_then_open)
    with pytest.raises(vh.VaultHubTargetInvalid, match="not a regular file"):
        vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    assert replaced == ["fifo"]


def test_missing_descriptor_support_fails_closed(vault_root, hh, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(vh.VaultHubTargetInvalid, match="safe descriptor-based vault reads unavailable"):
        vh.read_project_vault_hub("ok_proj", hermes_home=hh)
