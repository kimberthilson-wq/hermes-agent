"""Targeted tests for ``hermes_cli.vault_retrieval`` — the explicit caller boundary.

Isolation strategy: the caller API deliberately exposes no ``hermes_home`` /
``registry_path`` parameter (no path-injection surface). To point delegation at
a temp vault, an autouse fixture monkeypatches ``vault_retrieval.read_project_vault_hub``
(the name imported into the caller's namespace) with a closure that binds
``hermes_home=hh`` and delegates to the real primitive. All tests therefore
exercise the **real** bounded primitive against an isolated fixture; the live
vault is never touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_cli import vault_retrieval as vr
from hermes_cli import vault_hub as vh


# ---------------------------------------------------------------------------
# Fixtures (isolated temp vault)
# ---------------------------------------------------------------------------


def _entry(pid, hub, ownership="HERMES_OWNED", root="/x", access="READ_ONLY"):
    return "\n".join([
        "<!-- registry-entry -->",
        f"project_id: {pid}",
        f"ownership_class: {ownership}",
        f"canonical_root: {root}",
        "canonical_source_type: LOCAL_FILE",
        f"vault_hub: {hub}",
        f"default_access_mode: {access}",
        "status_scope: TEST",
        "<!-- /registry-entry -->",
    ]) + "\n"


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge-vault"
    (root / "projects" / "ok").mkdir(parents=True)
    (root / "projects" / "ok" / "HUB.md").write_text("A" * 300 + "\n", encoding="utf-8")
    (root / "HERMES_PROJECT_REGISTRY.md").write_text(
        _entry("ok_proj", hub="projects/ok/HUB.md")
        + _entry("unknown_hub", hub="UNKNOWN")
        + _entry("ghost", hub="projects/ghost/HUB.md"),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def hh(tmp_path: Path) -> Path:
    _build(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def bind_temp_vault(hh, monkeypatch):
    """Route the caller's delegation to the temp vault (caller API stays narrow)."""
    real = vh.read_project_vault_hub

    def bound(project_id, *, max_chars=vh.DEFAULT_MAX_CHARS,
              registry_path=None, hermes_home=None):
        # Tests that want to assert delegation pass hermes_home themselves;
        # otherwise bind to the fixture home.
        return real(project_id, max_chars=max_chars,
                    registry_path=registry_path, hermes_home=hermes_home or hh)

    monkeypatch.setattr(vr, "read_project_vault_hub", bound)
    yield
    monkeypatch.undo()


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): _sha(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# 1. exact project_id reaches read_project_vault_hub
# ---------------------------------------------------------------------------


def test_exact_project_id_reaches_primitive(hh, monkeypatch):
    calls: list[dict] = []
    real_bound = vr.read_project_vault_hub  # the autouse-bound closure

    def spy(project_id, *, max_chars=vh.DEFAULT_MAX_CHARS, registry_path=None, hermes_home=None):
        calls.append({"project_id": project_id, "max_chars": max_chars})
        return real_bound(project_id, max_chars=max_chars,
                          registry_path=registry_path, hermes_home=hermes_home or hh)

    monkeypatch.setattr(vr, "read_project_vault_hub", spy)
    result = vr.retrieve_project_vault("ok_proj")
    assert calls and calls[0]["project_id"] == "ok_proj"
    assert result.project_id == "ok_proj"
    assert "A" * 300 in result.content


# ---------------------------------------------------------------------------
# 2. returned content is bounded
# ---------------------------------------------------------------------------


def test_content_is_bounded(hh):
    r = vr.retrieve_project_vault("ok_proj", max_chars=100)
    assert len(r.content) == 100
    assert r.truncated is True
    # default bound also applies
    r2 = vr.retrieve_project_vault("ok_proj")
    assert len(r2.content) <= vh.DEFAULT_MAX_CHARS


# ---------------------------------------------------------------------------
# 3. provenance preserved unchanged
# ---------------------------------------------------------------------------


def test_provenance_preserved_unchanged(hh):
    direct = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    via_caller = vr.retrieve_project_vault("ok_proj", max_chars=10**9)
    assert via_caller.provenance == direct.provenance
    assert via_caller.provenance["source"] == "HERMES_PROJECT_REGISTRY.md"
    assert via_caller.provenance["ownership_class"] == "HERMES_OWNED"
    assert via_caller.provenance["default_access_mode"] == "READ_ONLY"


# ---------------------------------------------------------------------------
# 4 / 5. fail-closed propagation
# ---------------------------------------------------------------------------


def test_reference_missing_propagates(hh):
    with pytest.raises(vh.VaultHubReferenceMissing):
        vr.retrieve_project_vault("unknown_hub")


def test_unknown_project_propagates(hh):
    with pytest.raises(vh.VaultHubProjectNotFound):
        vr.retrieve_project_vault("does_not_exist")


def test_nonexistent_target_propagates(hh):
    with pytest.raises(vh.VaultHubTargetInvalid):
        vr.retrieve_project_vault("ghost")


# ---------------------------------------------------------------------------
# 6. arbitrary direct filesystem path is NOT accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "/home/ubuntu/.hermes/knowledge-vault/projects/ok/HUB.md",
    "../escape.md",
    "projects/ok/HUB.md",
    "C:\\vault\\HUB.md",
    "~/vault/HUB.md",
    "ok_proj/path",
    "OK_PROJ",           # uppercase -> not a valid Registry id
    "ok proj",           # space
    "ok proj\x00x",      # null byte
    "",
    "   ",
])
def test_path_like_or_malformed_input_rejected(hh, bad):
    with pytest.raises(vr.VaultHubInvalidProjectId):
        vr.retrieve_project_vault(bad)


def test_non_string_input_rejected(hh):
    with pytest.raises(vr.VaultHubInvalidProjectId):
        vr.retrieve_project_vault(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. no write side effects
# ---------------------------------------------------------------------------


def test_no_write_side_effects(hh):
    vault_root = hh / "knowledge-vault"
    reg = vault_root / "HERMES_PROJECT_REGISTRY.md"
    reg_before, tree_before = _sha(reg), _tree(vault_root)

    vr.retrieve_project_vault("ok_proj", max_chars=50)
    for bad in ("unknown_hub", "does_not_exist", "ghost",
                "/abs/path", "../x", "ok_proj/x"):
        with pytest.raises(vh.VaultHubError):
            vr.retrieve_project_vault(bad)

    assert _sha(reg) == reg_before
    assert _tree(vault_root) == tree_before
    # nothing newly created anywhere under the fixture home
    assert set(_tree(hh)) == set(
        {str(p.relative_to(hh)) for p in hh.rglob("*") if p.is_file()}
    )


# ---------------------------------------------------------------------------
# 8. existing Step 31/32 tests remain green  (verified in same pytest run)
# ---------------------------------------------------------------------------


def test_step31_primitive_still_green(hh):
    r = vh.read_project_vault_hub("ok_proj", hermes_home=hh)
    assert r.project_id == "ok_proj"
    with pytest.raises(vh.VaultHubReferenceMissing):
        vh.read_project_vault_hub("unknown_hub", hermes_home=hh)
