"""Bounded typed operations on the existing RoomLink /v1/runs lifecycle.

No agent/session/model/tool initialization belongs in this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import suppress

from aiohttp import web

from gateway.platforms.api_server_room_grants import _json_error, _room_grant_error_response
from gateway.platforms import api_server_typed_tasks as tasks

TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "interrupted"})


def owner_scope(claims: dict) -> str:
    return hashlib.sha256(json.dumps(["typed-v1", *[claims[k] for k in tasks.IDENTITY_FIELDS]],
        separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _error(api, code: str, *, status: int = 403):
    return _json_error(api._openai_error, "Typed delegation was not admitted.", code=code, status=status)


def _finish(adapter, run_id: str, status: str, *, output=None, error_category=None) -> bool:
    current = adapter._run_statuses[run_id]
    if current["status"] in TERMINAL:
        return False
    provenance = dict(current["provenance"])
    provenance.update(terminal_at=time.time(), terminal_status=status, error_category=error_category)
    fields = {"provenance": provenance, "last_event": f"run.{status}",
        "remote_cancel_confirmed": status == "cancelled", "remote_effect_unknown": False}
    if output is not None:
        fields["output"] = tasks.validate_output(output)
    if error_category is not None:
        fields["error_category"] = error_category
    adapter._set_run_status(run_id, status, **fields)
    stream = adapter._run_streams.get(run_id)
    if stream is not None:
        from gateway.platforms.api_server_runs import _run_event
        stream.put_nowait(_run_event(run_id, f"run.{status}", **fields))
        stream.put_nowait(None)
    return True


def stop(adapter, run_id: str) -> dict:
    """Authoritative result cancellation; never claims physical worker termination."""
    _finish(adapter, run_id, "cancelled", error_category="cancelled")
    return adapter._run_statuses[run_id]


def _discard_late(adapter, run_id: str) -> None:
    current = adapter._run_statuses.get(run_id)
    if current is not None and current["status"] in TERMINAL:
        adapter._set_run_status(run_id, current["status"], late_result_discarded=True)


async def _execute(adapter, run_id: str, delegation: dict, admitted_monotonic: float) -> None:
    from gateway.platforms.api_server_runs import _retire_live_run
    handler = None
    try:
        if adapter._run_statuses[run_id]["status"] in TERMINAL:
            return
        provenance = dict(adapter._run_statuses[run_id]["provenance"])
        provenance["started_at"] = time.time()
        adapter._set_run_status(run_id, "running", provenance=provenance)
        remaining = delegation["max_runtime_ms"] / 1000 - (time.monotonic() - admitted_monotonic)
        if remaining <= 0:
            _finish(adapter, run_id, "timed_out", error_category="deadline_exceeded")
            return
        handler = asyncio.create_task(tasks.TASK_HANDLERS[delegation["task_type"]](delegation["payload"]))
        try:
            output = await asyncio.wait_for(asyncio.shield(handler), timeout=remaining)
        except asyncio.TimeoutError:
            _finish(adapter, run_id, "timed_out", error_category="deadline_exceeded")
            # The only production handler is bounded to 1s. Observe late completion,
            # retaining a boolean diagnostic, never its output or caller message.
            with suppress(Exception):
                await handler
            _discard_late(adapter, run_id)
        else:
            if time.monotonic() - admitted_monotonic >= delegation["max_runtime_ms"] / 1000:
                _finish(adapter, run_id, "timed_out", error_category="deadline_exceeded")
                _discard_late(adapter, run_id)
            elif not _finish(adapter, run_id, "completed", output=output):
                _discard_late(adapter, run_id)
    except asyncio.CancelledError:
        _finish(adapter, run_id, "cancelled", error_category="cancelled")
        if handler is not None:
            with suppress(Exception, asyncio.CancelledError):
                await handler
            _discard_late(adapter, run_id)
        raise
    except Exception:
        # Exception strings can contain payloads or credentials; never emit/log them.
        if not _finish(adapter, run_id, "failed", error_category="handler_failed"):
            _discard_late(adapter, run_id)
    finally:
        _retire_live_run(adapter, run_id)


async def handle(adapter, request, body, *, api):
    """Authenticate and admit typed work before all generic session/agent parsing."""
    from gateway.platforms.api_server_room_grants import _local_target
    from gateway.platforms.api_server_runs import (
        _accepted_response, _RunStream, _remember_room_retention, _room_retention_until,
        _replay_or_conflict)
    try:
        claims = adapter._room_grant_claims(request, permission="dispatch")
        authority = tasks.validate_authority(claims.get("typed_authority"))
        profile, installation_id = _local_target(claims, api._api_request_profile)
    except Exception as exc:
        return _room_grant_error_response(exc, _openai_error=api._openai_error)
    _remember_room_retention(request, claims)
    if not isinstance(body, dict) or set(body) != {"typed_delegation"}:
        return _error(api, "invalid_typed_delegation", status=400)
    if any(request.headers.get(k) for k in ("X-Hermes-Session-Key", "X-Hermes-Session-Id")):
        return _error(api, "typed_session_forbidden", status=400)
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 255 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        return _error(api, "invalid_idempotency_key", status=400)
    try:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False).encode("ascii")
        if len(encoded) > 16384:
            raise tasks.TypedDelegationError("Typed request too large.")
        fingerprint = tasks.digest({"body": body, "authority": authority,
            "owner": {k: claims[k] for k in tasks.IDENTITY_FIELDS},
            "execution_policy_digest": claims["execution_policy_digest"],
            "interface_version": tasks.INTERFACE_VERSION})
    except (ValueError, TypeError, RecursionError):
        return _error(api, "invalid_typed_delegation", status=400)
    if (not adapter._run_idempotency_store.durable
            or not adapter._run_idempotency_store.private_storage_verified):
        return _error(api, "typed_durable_storage_required", status=503)
    scope = owner_scope(claims)
    retention = _room_retention_until(request)
    # Compare the complete structural fingerprint before semantic admission:
    # same-key changed task/scope/capability is always a conflict, never dispatched.
    outcome, record = adapter._run_idempotency_store.lookup(
        scope, key, fingerprint, retention_until=retention)
    if outcome != "missing":
        return _replay_or_conflict(adapter, request, outcome, record, None, api._openai_error)
    try:
        delegation = tasks.validate_delegation(body["typed_delegation"], claims,
            target_profile=profile, installation_id=installation_id)
        if key != f'room:{delegation["task_id"]}:{delegation["execution_generation"]}':
            raise tasks.TypedDelegationError("Typed idempotency identity mismatch.")
    except (ValueError, TypeError, KeyError):
        return _error(api, "invalid_typed_delegation")
    for gate in (adapter._draining_response, adapter._concurrency_limited_response):
        error = gate()
        if error is not None:
            return error
    run_id = f"run_{uuid.uuid4().hex}"
    created = time.time()
    provenance = {k: delegation[k] for k in tasks.PROVENANCE_FIELDS if k in delegation}
    provenance.update(run_id=run_id, interface_version=tasks.INTERFACE_VERSION,
        input_digest=tasks.digest(delegation["payload"]), created_at=created,
        started_at=None, terminal_at=None, terminal_status=None, error_category=None)
    initial = {"object": "hermes.run", "run_id": run_id, "status": "queued",
        "created_at": created, "updated_at": created, "provenance": provenance,
        "typed_delegation": True}
    outcome, record = adapter._run_idempotency_store.reserve(scope, key, fingerprint, run_id, initial,
        owner_pid=adapter._run_owner_pid, owner_started=adapter._run_owner_started, retention_until=retention)
    if outcome != "created":
        return _replay_or_conflict(adapter, request, outcome, record, None, api._openai_error)
    adapter._run_idempotency_ids.add(run_id)
    adapter._run_owners[run_id] = scope
    adapter._run_statuses[run_id] = initial
    adapter._run_streams[run_id] = _RunStream()
    adapter._run_streams_created[run_id] = created
    adapter._activate_admitted_request()
    task = adapter._active_run_tasks[run_id] = asyncio.create_task(
        _execute(adapter, run_id, delegation, time.monotonic()))
    adapter._background_tasks.add(task)
    task.add_done_callback(adapter._background_tasks.discard)
    return _accepted_response(run_id, "started", None, replayed=False)


def check_query(status: dict, query) -> None:
    fields = {
        "expected_task_id": ("task_id", str),
        "expected_execution_generation": ("execution_generation", int),
        "expected_project_scope": ("project_scope", str),
        "expected_capability_digest": ("capability_digest", str),
        "expected_trace_id": ("trace_id", str),
        "not_before": ("not_before", float)}
    if any(key in query for key in fields):
        expected = {target: parse(query[key]) for key, (target, parse) in fields.items() if key in query}
        tasks.validate_result(status, expected)
