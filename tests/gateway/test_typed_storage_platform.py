"""Platform compatibility and private storage gates for typed delegation."""
import os
import stat

import pytest

from gateway import hosted_rooms_common as common
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore


class _NonPosixOS:
    """Keep actual I/O but model Windows without a POSIX fd-permission API."""
    name = "nt"

    def __getattr__(self, key):
        if key in {"fchmod", "geteuid"}:
            raise AttributeError(key)
        return getattr(os, key)


def test_nonposix_private_check_preserves_generic_file(monkeypatch, tmp_path):
    path = tmp_path / "existing.db"
    path.write_bytes(b"existing-data")
    path.chmod(0o644)
    monkeypatch.setattr(common, "os", _NonPosixOS())
    assert common.secure_sqlite_files(path, create=True) is False
    assert path.read_bytes() == b"existing-data"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_nonposix_generic_store_remains_durable_but_not_private(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "os", _NonPosixOS())
    store = RunIdempotencyStore(str(tmp_path / "runs.db"))
    try:
        assert store.durable is True
        assert store.private_storage_verified is False
        outcome, record = store.reserve("scope", "key", "fingerprint", "run-1",
            {"run_id": "run-1", "status": "queued"})
        assert outcome == "created"
        assert record["run_id"] == "run-1"
        assert store.lookup("scope", "key", "fingerprint")[0] == "reused"
    finally:
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_posix_private_creation_and_sidecar_repair(tmp_path):
    path = tmp_path / "private" / "state.db"
    assert common.secure_sqlite_files(path, create=True) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        candidate.write_bytes(b"preserved")
        candidate.chmod(0o644)
    assert common.secure_sqlite_files(path) is True
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        assert candidate.read_bytes() == b"preserved"
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_posix_symlink_is_rejected_without_mutating_target(tmp_path, suffix):
    path = tmp_path / "state.db"
    target = tmp_path / "target"
    target.write_bytes(b"preserved")
    target.chmod(0o644)
    if suffix:
        common.secure_sqlite_files(path, create=True)
    candidate = path.with_name(path.name + suffix)
    candidate.symlink_to(target)
    with pytest.raises(OSError):
        common.secure_sqlite_files(path, create=True)
    assert target.read_bytes() == b"preserved"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_posix_chmod_failure_is_fail_closed(monkeypatch, tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"preserved")
    path.chmod(0o644)
    def deny_chmod(*args):
        raise PermissionError("permission repair denied")
    monkeypatch.setattr(common.os, "fchmod", deny_chmod)
    with pytest.raises(PermissionError, match="permission repair denied"):
        common.secure_sqlite_files(path, create=True)
    assert path.read_bytes() == b"preserved"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_private_storage_gate_rechecks_and_rejects_failed_repair(monkeypatch, tmp_path):
    path = tmp_path / "runs.db"
    store = RunIdempotencyStore(str(path))
    try:
        assert store.durable is True
        assert store.private_storage_verified is True
        path.chmod(0o644)
        def deny_chmod(*args):
            raise PermissionError("permission repair denied")
        monkeypatch.setattr(common.os, "fchmod", deny_chmod)
        assert store.private_storage_verified is False
        assert store.durable is True
    finally:
        store.close()


def test_missing_and_memory_state_are_not_verified_private(tmp_path):
    assert common.secure_sqlite_files(":memory:") is False
    assert common.secure_sqlite_files(tmp_path / "missing.db") is False
    assert not (tmp_path / "missing.db").exists()
    store = RunIdempotencyStore(":memory:")
    try:
        assert store.durable is False
        assert store.private_storage_verified is False
    finally:
        store.close()
