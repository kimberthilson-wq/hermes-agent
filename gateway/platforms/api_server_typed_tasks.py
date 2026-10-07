"""Closed, no-tool RoomLink typed-operation contracts.

This registry admits only reviewed deterministic handlers. It is not an agent or
an OS isolation boundary and never interprets payload text as instructions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from typing import Any

from gateway.hosted_rooms_common import bounded_int, identifier

INTERFACE_VERSION = "hermes.typed.v1"
TASK_TYPE = "hermes.acceptance.echo"
PROJECT_SCOPE = "hermes.selftest"
MAX_RUNTIME_MS = 5000
MAX_MESSAGE_BYTES = 1024
IDENTITY_FIELDS = (
    "room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
    "member_id", "target_install_id", "target_profile")
DELEGATION_FIELDS = frozenset((*IDENTITY_FIELDS, "task_id", "execution_generation", "trace_id",
    "protocol_version", "task_type", "project_scope", "capability_ceiling",
    "execution_policy_digest", "capability_digest", "payload", "max_runtime_ms"))
AUTHORITY_FIELDS = frozenset(("version", "task_types", "project_scopes", "capability_ceiling", "max_runtime_ms"))


class TypedDelegationError(ValueError):
    """Malformed or unauthorized typed delegation (safe fixed messages only)."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def validate_authority(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != AUTHORITY_FIELDS:
        raise TypedDelegationError("Invalid typed authority fields.")
    if (type(value["version"]) is not int or value["version"] != 1
            or value["task_types"] != [TASK_TYPE]
            or value["project_scopes"] != [PROJECT_SCOPE]
            or value["capability_ceiling"] != []):
        raise TypedDelegationError("Typed authority is outside the supported task boundary.")
    runtime = bounded_int(value["max_runtime_ms"], low=1, high=MAX_RUNTIME_MS,
        error=TypedDelegationError, message="Invalid typed authority runtime.")
    return {"version": 1, "task_types": [TASK_TYPE], "project_scopes": [PROJECT_SCOPE],
        "capability_ceiling": [], "max_runtime_ms": runtime}


def typed_catalog(target_profile: str, installation_id: str) -> dict:
    policy = {"version": 1, "target_profile": target_profile,
        "typed_only": True, "capability_ceiling": []}
    policy["policy_digest"] = digest(policy)
    catalog = {"installation_id": installation_id, "target_profile": target_profile,
        "protocol_versions": [2], "interface_version": INTERFACE_VERSION,
        "task_types": [TASK_TYPE], "project_scopes": [PROJECT_SCOPE], "capability_ceiling": [],
        "max_runtime_ms": MAX_RUNTIME_MS, "max_message_bytes": MAX_MESSAGE_BYTES,
        "execution_policy": policy}
    catalog["catalog_digest"] = digest(catalog)
    return catalog


def validate_delegation(value: Any, claims: dict, *, target_profile: str, installation_id: str) -> dict:
    if not isinstance(value, dict) or set(value) != DELEGATION_FIELDS:
        raise TypedDelegationError("Invalid typed delegation fields.")
    authority = validate_authority(claims.get("typed_authority"))
    catalog = typed_catalog(target_profile, installation_id)
    for field in IDENTITY_FIELDS:
        if value[field] != claims.get(field) or type(value[field]) is not type(claims.get(field)):
            raise TypedDelegationError("Typed delegation identity does not match its grant.")
    if value["target_profile"] != target_profile or value["target_install_id"] != installation_id:
        raise TypedDelegationError("Typed delegation targets another installation or profile.")
    if type(value["protocol_version"]) is not int or value["protocol_version"] != 2:
        raise TypedDelegationError("Unsupported typed protocol.")
    if value["task_type"] not in authority["task_types"] or value["task_type"] not in TASK_HANDLERS:
        raise TypedDelegationError("Unknown typed task.")
    if value["project_scope"] not in authority["project_scopes"] or value["project_scope"] != PROJECT_SCOPE:
        raise TypedDelegationError("Typed project scope is not admitted.")
    if value["capability_ceiling"] != [] or not isinstance(value["capability_ceiling"], list):
        raise TypedDelegationError("Typed task capabilities exceed the empty ceiling.")
    if (value["execution_policy_digest"] != catalog["execution_policy"]["policy_digest"]
            or claims["execution_policy_digest"] != value["execution_policy_digest"]
            or value["capability_digest"] != catalog["catalog_digest"]):
        raise TypedDelegationError("Typed policy or capability catalog changed.")
    for field in ("task_id", "trace_id"):
        if identifier(value[field], label=field, error=TypedDelegationError) != value[field]:
            raise TypedDelegationError("Typed identifiers must be canonical.")
    bounded_int(value["execution_generation"], low=1, high=2147483647,
        error=TypedDelegationError, message="Invalid typed execution generation.")
    bounded_int(value["max_runtime_ms"], low=1, high=authority["max_runtime_ms"],
        error=TypedDelegationError, message="Typed deadline exceeds its grant.")
    payload = value["payload"]
    if not isinstance(payload, dict) or set(payload) - {"message", "scheduling_delay_ms"} or "message" not in payload:
        raise TypedDelegationError("Invalid echo payload.")
    message = payload["message"]
    if not isinstance(message, str):
        raise TypedDelegationError("Echo message must be UTF-8 text.")
    try:
        size = len(message.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise TypedDelegationError("Echo message must be valid UTF-8.") from None
    if size > MAX_MESSAGE_BYTES:
        raise TypedDelegationError("Echo message is too large.")
    bounded_int(payload.get("scheduling_delay_ms", 0), low=0, high=1000,
        error=TypedDelegationError, message="Invalid bounded scheduling delay.")
    # Fresh object: handlers never receive the caller's mutable authority envelope.
    return json.loads(json.dumps(value, ensure_ascii=True))


async def echo(payload: dict) -> dict:
    """Hash a bounded UTF-8 message; optional bounded scheduling permits lifecycle fences.

    Only these explicit data enter the handler. No profile, context, approval or
    infrastructure is available here. Raw text is not returned or persisted.
    """
    await asyncio.sleep(payload.get("scheduling_delay_ms", 0) / 1000)
    encoded = payload["message"].encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "utf8_bytes": len(encoded)}


TASK_HANDLERS = {TASK_TYPE: echo}
PROVENANCE_FIELDS = frozenset((*IDENTITY_FIELDS, "run_id", "task_id", "execution_generation",
    "trace_id", "task_type", "project_scope", "capability_ceiling", "capability_digest",
    "execution_policy_digest", "protocol_version", "interface_version", "input_digest",
    "max_runtime_ms", "created_at", "started_at", "terminal_at", "terminal_status", "error_category"))


def validate_output(value: Any) -> dict:
    if (not isinstance(value, dict) or set(value) != {"sha256", "utf8_bytes"}
            or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64
            or any(ch not in "0123456789abcdef" for ch in value["sha256"])):
        raise TypedDelegationError("Malformed typed output.")
    bounded_int(value["utf8_bytes"], low=0, high=MAX_MESSAGE_BYTES,
        error=TypedDelegationError, message="Malformed typed output length.")
    return dict(value)


def validate_result(status: Any, expected: dict, now: float | None = None) -> None:
    """Strict correlation assertion for callers; raises on malformed, stale or mismatched data.

    expected supplies delegation fields plus optional run_id, input_digest and
    not_before (minimum creation timestamp). Validate against the original request,
    not values copied from the received result.
    """
    if not isinstance(status, dict) or status.get("object") != "hermes.run":
        raise TypedDelegationError("Malformed typed result.")
    if not isinstance(status.get("status"), str):
        raise TypedDelegationError("Malformed typed lifecycle status.")
    if not isinstance(status.get("run_id"), str) or not re.fullmatch(r"run_[0-9a-f]{32}", status["run_id"]):
        raise TypedDelegationError("Malformed typed run identifier.")
    if not isinstance(expected, dict):
        raise TypedDelegationError("Malformed expected typed correlation.")
    provenance = status.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
        raise TypedDelegationError("Malformed typed provenance.")
    if provenance["run_id"] != status.get("run_id") or provenance["interface_version"] != INTERFACE_VERSION:
        raise TypedDelegationError("Typed result identity mismatch.")
    if (provenance["task_type"] != TASK_TYPE or provenance["project_scope"] != PROJECT_SCOPE
            or provenance["capability_ceiling"] != [] or type(provenance["protocol_version"]) is not int
            or provenance["protocol_version"] != 2):
        raise TypedDelegationError("Typed result boundary mismatch.")
    for field, value in expected.items():
        if field == "not_before":
            continue
        if field not in PROVENANCE_FIELDS or provenance[field] != value or type(provenance[field]) is not type(value):
            raise TypedDelegationError("Typed result correlation mismatch.")
    for field in (*IDENTITY_FIELDS, "task_id", "trace_id"):
        if field == "authority_epoch":
            bounded_int(provenance[field], low=1, error=TypedDelegationError,
                message="Malformed typed authority epoch.")
        else:
            identifier(provenance[field], label=field, error=TypedDelegationError,
                max_chars=256, pattern=None)
    bounded_int(provenance["execution_generation"], low=1, high=2147483647,
        error=TypedDelegationError, message="Malformed typed generation.")
    bounded_int(provenance["max_runtime_ms"], low=1, high=MAX_RUNTIME_MS,
        error=TypedDelegationError, message="Malformed typed deadline.")
    for field in ("capability_digest", "execution_policy_digest", "input_digest"):
        value = provenance[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise TypedDelegationError("Malformed typed provenance digest.")
    clock = now if now is not None else time.time()
    for field in ("created_at", "started_at", "terminal_at"):
        value = provenance[field]
        if value is None and field != "created_at":
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value > clock + 30:
            raise TypedDelegationError("Malformed typed result timestamp.")
    not_before = expected.get("not_before", 0)
    if type(not_before) not in (int, float) or not math.isfinite(not_before):
        raise TypedDelegationError("Invalid typed freshness assertion.")
    if not_before > provenance["created_at"]:
        raise TypedDelegationError("Stale typed result.")
    if provenance["started_at"] is not None and provenance["started_at"] < provenance["created_at"]:
        raise TypedDelegationError("Malformed typed result timestamp order.")
    terminal = status.get("status") in {"completed", "failed", "cancelled", "timed_out", "interrupted"}
    if provenance["error_category"] is not None and not isinstance(provenance["error_category"], str):
        raise TypedDelegationError("Malformed typed error category.")
    if terminal:
        if provenance["terminal_status"] != status["status"] or provenance["terminal_at"] is None:
            raise TypedDelegationError("Typed terminal provenance mismatch.")
        if provenance["terminal_at"] < (provenance["started_at"] or provenance["created_at"]):
            raise TypedDelegationError("Malformed typed terminal timestamp.")
    elif status.get("status") not in {"queued", "running"} or provenance["terminal_status"] is not None:
        raise TypedDelegationError("Malformed typed lifecycle status.")
    if status.get("status") == "completed":
        if provenance["started_at"] is None:
            raise TypedDelegationError("Completed typed task has no start timestamp.")
        validate_output(status.get("output"))
        if provenance["error_category"] is not None:
            raise TypedDelegationError("Malformed completed typed result.")
    elif terminal and provenance["error_category"] not in {
            "cancelled", "deadline_exceeded", "handler_failed", "owner_restarted", "gateway_shutdown"}:
        raise TypedDelegationError("Malformed typed failure category.")
    if status.get("status") != "completed" and "output" in status:
        raise TypedDelegationError("Unaccepted typed output.")
