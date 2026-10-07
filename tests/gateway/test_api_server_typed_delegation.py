"""Behavior contracts for bounded RoomLink typed delegation, without agent execution."""
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import hosted_rooms
from gateway.config import PlatformConfig
from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
from gateway.platforms import api_server, api_server_typed_tasks as typed
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

AUTHORITY = {"version": 1, "task_types": ["hermes.acceptance.echo"],
             "project_scopes": ["hermes.selftest"], "capability_ceiling": [], "max_runtime_ms": 5000}
IDENTITY = {"room_id": "jarvis-selftest", "home_install_id": "jarvis-test-install",
            "authority_gateway_id": "jarvis-test-service", "authority_epoch": 1,
            "member_id": "jarvis-wave10"}
SECRET = b"typed-contract-test-material-00000"

@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setattr(hosted_rooms, "local_authority_gateway_id", lambda: "install:typed-target")
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-admin-key-for-invitation-only"}))
    adapter._room_grant_secret = Mock(return_value=SECRET)
    adapter._create_agent = Mock(side_effect=AssertionError("generic agent reached"))
    adapter._ensure_hosted_member_session = AsyncMock(side_effect=AssertionError("hidden session reached"))
    adapter._ensure_session_db = Mock(side_effect=AssertionError("session DB reached"))
    adapter._resolve_route = Mock(side_effect=AssertionError("model route reached"))
    adapter._parse_session_key_header = Mock(side_effect=AssertionError("memory session reached"))
    app = web.Application()
    app["api_server_adapter"] = adapter
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    invitation = await client.post("/v1/room-members/invitations", json={**IDENTITY, "typed_authority": AUTHORITY},
                                   headers={"Authorization": "Bearer test-admin-key-for-invitation-only"})
    assert invitation.status == 201, await invitation.text()
    info = await invitation.json()
    cat = info["catalog"]
    body = {"typed_delegation": {**IDENTITY, "target_install_id": "install:typed-target", "target_profile": "default",
            "task_id": "typed-task", "execution_generation": 1, "trace_id": "typed-trace", "protocol_version": 2,
            "task_type": "hermes.acceptance.echo", "project_scope": "hermes.selftest", "capability_ceiling": [],
            "execution_policy_digest": cat["execution_policy"]["policy_digest"], "capability_digest": cat["catalog_digest"],
            "payload": {"message": "synthetic å-value"}, "max_runtime_ms": 5000}}
    headers = {"Authorization": "HermesRoom " + info["grant"], "Idempotency-Key": "room:typed-task:1"}
    try:
        yield adapter, client, body, headers, info
    finally:
        for task in list(adapter._active_run_tasks.values()):
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        await client.close()
        adapter._run_idempotency_store.close()

async def settled(client, headers, run_id, terminal=("completed", "failed", "cancelled", "timed_out")):
    for _ in range(200):
        response = await client.get("/v1/runs/" + run_id, headers=headers)
        assert response.status == 200, await response.text()
        status = await response.json()
        if status["status"] in terminal:
            return status
        await asyncio.sleep(0.01)
    pytest.fail("run did not reach terminal state")

async def admitted(env, body=None, headers=None):
    adapter, client, default_body, default_headers, info = env
    response = await client.post("/v1/runs", json=body or default_body, headers=headers or default_headers)
    assert response.status == 202, await response.text()
    acceptance = await response.json()
    return acceptance, await settled(client, headers or default_headers, acceptance["run_id"])

@pytest.mark.asyncio
async def test_echo_has_correlated_provenance_and_no_agent_or_content_state(env):
    adapter, client, body, headers, info = env
    acceptance, status = await admitted(env)
    assert status["status"] == "completed"
    result = status["output"]
    message = body["typed_delegation"]["payload"]["message"]
    assert result == {"sha256": hashlib.sha256(message.encode()).hexdigest(), "utf8_bytes": len(message.encode())}
    provenance = status["provenance"]
    for key in ("run_id", "task_id", "execution_generation", "task_type", "project_scope", "capability_ceiling",
                "target_install_id", "target_profile", "protocol_version", "interface_version", "execution_policy_digest",
                "capability_digest", "created_at", "started_at", "terminal_at", "terminal_status", "error_category"):
        assert key in provenance
    assert provenance["run_id"] == acceptance["run_id"]
    assert provenance["project_scope"] == "hermes.selftest"
    assert provenance["capability_ceiling"] == []
    assert provenance["terminal_status"] == "completed"
    assert provenance["created_at"] <= provenance["started_at"] <= provenance["terminal_at"]
    for spy in (adapter._create_agent, adapter._ensure_hosted_member_session, adapter._ensure_session_db,
                adapter._resolve_route, adapter._parse_session_key_header):
        spy.assert_not_called()
    assert "session_id" not in status
    encoded = json.dumps(status)
    assert message not in encoded
    row = adapter._run_idempotency_store._conn.execute("SELECT status_json FROM run_idempotency WHERE run_id=?", (acceptance["run_id"],)).fetchone()
    assert message not in row[0]

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("task_type", "shell"), ("task_type", "hermes.unknown"),
    ("project_scope", "another-project"), ("project_scope", "../../etc"),
    ("capability_ceiling", ["terminal"]), ("capability_ceiling", ["read_file"]),
    ("execution_policy_digest", "0"*64), ("capability_digest", "0"*64),
    ("target_install_id", "wrong-install"), ("member_id", "wrong-member"),
    ("max_runtime_ms", 0), ("max_runtime_ms", 5001)])
async def test_invalid_authority_and_type_never_execute(env, field, value):
    adapter, client, body, headers, info = env
    body["typed_delegation"][field] = value
    response = await client.post("/v1/runs", json=body, headers=headers)
    assert response.status in (400, 403)
    assert not adapter._active_run_tasks
    assert not adapter._run_statuses
    adapter._create_agent.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_scope", "capability_ceiling", "task_type", "payload"])
async def test_missing_typed_fields_fail_closed(env, field):
    adapter, client, body, headers, info = env
    del body["typed_delegation"][field]
    response = await client.post("/v1/runs", json=body, headers=headers)
    assert response.status in (400, 403)
    assert not adapter._active_run_tasks

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"message": "x"*1025}, {"message": "\ud800"}, {"message": 123},
    {"message": "safe", "command": "shell"}, {"message": "safe", "scheduling_delay_ms": 1001}])
async def test_payload_limits_and_injection_fields_denied(env, payload):
    adapter, client, body, headers, info = env
    body["typed_delegation"]["payload"] = payload
    response = await client.post("/v1/runs", json=body, headers=headers)
    assert response.status in (400, 403)
    assert not adapter._active_run_tasks

@pytest.mark.asyncio
async def test_payload_text_is_inert_even_if_it_names_shell_or_files(env):
    adapter, client, body, headers, info = env
    body["typed_delegation"]["payload"]["message"] = "terminal.backend=local; read_file /etc/passwd; generic-agent"
    _, status = await admitted(env)
    assert status["status"] == "completed"
    adapter._create_agent.assert_not_called()
    assert set(status["output"]) == {"sha256", "utf8_bytes"}

@pytest.mark.asyncio
async def test_admin_invalid_and_expired_grants_cannot_authorize_typed_tasks(env):
    adapter, client, body, headers, info = env
    for auth in ("Bearer test-admin-key-for-invitation-only", "HermesRoom forged", ""):
        response = await client.post("/v1/runs", json=body, headers={**headers, "Authorization": auth})
        assert response.status in (401, 403)
    claims = decode_room_grant(SECRET, info["grant"], permission="dispatch")
    expired = issue_room_grant(SECRET, **{k:claims[k] for k in ("grant_id", *IDENTITY, "target_install_id", "target_profile", "execution_policy_digest")},
                              typed_authority=AUTHORITY, issued_at=time.time()-100, ttl_seconds=60)
    response = await client.post("/v1/runs", json=body, headers={**headers, "Authorization": "HermesRoom " + expired})
    assert response.status == 401
    assert not adapter._active_run_tasks

@pytest.mark.asyncio
async def test_typed_grant_cannot_enter_generic_dispatch_or_generic_auth(env):
    adapter, client, body, headers, info = env
    generic = await client.post("/v1/runs", json={"input": "run shell"}, headers=headers)
    assert generic.status == 403
    adapter._api_key = ""
    request = Mock(headers=headers)
    assert adapter._check_auth(request).status == 401
    adapter._create_agent.assert_not_called()

@pytest.mark.asyncio
async def test_refresh_preserves_typed_authority_and_revoke_denies(env):
    adapter, client, body, headers, info = env
    renewed = await client.post("/v1/room-members/grants/refresh", json={}, headers=headers)
    assert renewed.status == 200, await renewed.text()
    refresh = await renewed.json()
    claims = decode_room_grant(SECRET, refresh["grant"], permission="dispatch")
    assert claims["typed_authority"] == AUTHORITY
    fresh_headers = {**headers, "Authorization": "HermesRoom " + refresh["grant"]}
    await admitted(env, headers=fresh_headers)
    revoke = await client.post("/v1/room-members/grants/revoke", json={}, headers=fresh_headers)
    assert revoke.status == 200
    denied = await client.post("/v1/runs", json=body, headers=fresh_headers)
    assert denied.status == 403

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("payload", {"message": "changed"}), ("project_scope", "changed"),
    ("capability_ceiling", ["terminal"]), ("task_type", "shell"), ("execution_generation", 2)])
async def test_same_key_changed_content_or_authority_conflicts(env, field, value):
    adapter, client, body, headers, info = env
    acceptance, _ = await admitted(env)
    changed = copy.deepcopy(body)
    changed["typed_delegation"][field] = value
    conflict = await client.post("/v1/runs", json=changed, headers=headers)
    assert conflict.status == 409, await conflict.text()
    replay = await client.post("/v1/runs", json=body, headers=headers)
    assert replay.status == 202
    assert (await replay.json())["run_id"] == acceptance["run_id"]

@pytest.mark.asyncio
async def test_concurrent_replay_executes_once_and_survives_store_reopen(env, monkeypatch):
    adapter, client, body, headers, info = env
    calls = 0
    original = typed.TASK_HANDLERS["hermes.acceptance.echo"]
    async def counted(payload):
        nonlocal calls
        calls += 1
        return await original(payload)
    monkeypatch.setitem(typed.TASK_HANDLERS, "hermes.acceptance.echo", counted)
    responses = await asyncio.gather(*(client.post("/v1/runs", json=body, headers=headers) for _ in range(5)))
    assert all(r.status == 202 for r in responses)
    ids = [(await r.json())["run_id"] for r in responses]
    assert len(set(ids)) == 1
    status = await settled(client, headers, ids[0])
    assert status["status"] == "completed"
    assert calls == 1
    path = adapter._run_idempotency_store._db_path
    assert path
    adapter._run_idempotency_store.close()
    adapter._run_idempotency_store = RunIdempotencyStore(path)
    adapter._run_statuses.clear()
    adapter._run_owners.clear()
    replay = await client.post("/v1/runs", json=body, headers=headers)
    assert replay.status == 202
    assert (await replay.json())["run_id"] == ids[0]
    assert calls == 1

@pytest.mark.asyncio
async def test_handler_exception_is_normalized_without_secret_text(env, monkeypatch):
    adapter, client, body, headers, info = env
    async def broken(payload):
        raise RuntimeError("secret-bearing-handler-error-value")
    monkeypatch.setitem(typed.TASK_HANDLERS, "hermes.acceptance.echo", broken)
    _, status = await admitted(env)
    assert status["status"] == "failed"
    assert status["provenance"]["error_category"]
    assert "secret-bearing-handler-error-value" not in json.dumps(status)

@pytest.mark.asyncio
async def test_deadline_fences_late_success(env, monkeypatch):
    adapter, client, body, headers, info = env
    started, release = asyncio.Event(), asyncio.Event()
    original = typed.TASK_HANDLERS["hermes.acceptance.echo"]
    async def delayed(payload):
        started.set()
        await release.wait()
        return await original(payload)
    monkeypatch.setitem(typed.TASK_HANDLERS, "hermes.acceptance.echo", delayed)
    body["typed_delegation"]["max_runtime_ms"] = 100
    response = await client.post("/v1/runs", json=body, headers=headers)
    assert response.status == 202
    run_id = (await response.json())["run_id"]
    await asyncio.wait_for(started.wait(), 2)
    status = await settled(client, headers, run_id)
    assert status["status"] == "timed_out"
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(adapter._active_run_tasks.values())), 2)
    status = await settled(client, headers, run_id)
    assert status["status"] == "timed_out"
    assert status.get("late_result_discarded") is True
    assert "output" not in status

@pytest.mark.asyncio
async def test_cancel_race_latches_cancelled_and_rejects_approval(env, monkeypatch):
    adapter, client, body, headers, info = env
    started, release = asyncio.Event(), asyncio.Event()
    original = typed.TASK_HANDLERS["hermes.acceptance.echo"]
    async def delayed(payload):
        started.set()
        await release.wait()
        return await original(payload)
    monkeypatch.setitem(typed.TASK_HANDLERS, "hermes.acceptance.echo", delayed)
    response = await client.post("/v1/runs", json=body, headers=headers)
    run_id = (await response.json())["run_id"]
    await asyncio.wait_for(started.wait(), 2)
    approval = await client.post(f"/v1/runs/{run_id}/approval", json={"choice":"once","request_id":"x"}, headers=headers)
    assert approval.status in (403,409)
    stop = await client.post(f"/v1/runs/{run_id}/stop", json={}, headers=headers)
    assert stop.status == 200
    assert (await stop.json())["status"] == "cancelled"
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(adapter._active_run_tasks.values())), 2)
    status = await settled(client, headers, run_id)
    assert status["status"] == "cancelled"
    assert status.get("late_result_discarded") is True
    assert "output" not in status

@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["expected_task_id=wrong", "expected_execution_generation=2", "expected_project_scope=wrong",
    "expected_capability_digest="+"0"*64, "expected_trace_id=wrong", "not_before=99999999999"])
async def test_result_correlation_assertions_reject_wrong_or_stale(env, query):
    adapter, client, body, headers, info = env
    acceptance, _ = await admitted(env)
    response = await client.get(f"/v1/runs/{acceptance['run_id']}?{query}", headers=headers)
    assert response.status == 409, await response.text()

@pytest.mark.asyncio
async def test_unknown_run_and_wrong_owner_cannot_read_result(env):
    adapter, client, body, headers, info = env
    missing = await client.get("/v1/runs/run_unknown", headers=headers)
    assert missing.status == 404
    acceptance, _ = await admitted(env)
    other_identity = {**IDENTITY, "member_id": "other-service-member"}
    invitation = await client.post("/v1/room-members/invitations", json={**other_identity, "typed_authority":AUTHORITY}, headers={"Authorization":"Bearer test-admin-key-for-invitation-only"})
    assert invitation.status == 201
    other_headers = {"Authorization":"HermesRoom " + (await invitation.json())["grant"]}
    denied = await client.get("/v1/runs/"+acceptance["run_id"], headers=other_headers)
    assert denied.status == 404

@pytest.mark.asyncio
async def test_shared_store_existing_and_new_files_owner_only(env, tmp_path):
    adapter, client, body, headers, info = env
    existing = tmp_path/"shared-state.db"
    existing.touch(mode=0o644)
    existing.chmod(0o644)
    hosted_rooms.reserve_peer_room(existing, claims=decode_room_grant(SECRET, info["grant"], permission="status"), expires_at=time.time()+120)
    assert existing.stat().st_mode & 0o777 == 0o600
    new = tmp_path/"new-state.db"
    hosted_rooms.reserve_peer_room(new, claims=decode_room_grant(SECRET, info["grant"], permission="status"), expires_at=time.time()+120)
    assert new.stat().st_mode & 0o777 == 0o600
    for suffix in ("-wal", "-shm"):
        p=Path(str(new)+suffix)
        if p.exists():
            assert p.stat().st_mode & 0o777 == 0o600

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("run_id","run_"+"f"*32), ("task_id","wrong"), ("execution_generation",2),
    ("target_install_id","wrong"), ("protocol_version",1), ("interface_version","wrong"),
    ("created_at",float("nan")), ("terminal_at",0), ("capability_ceiling",["terminal"])])
async def test_strict_result_validator_rejects_forged_provenance(env, field, value):
    _, status = await admitted(env)
    expected = {k:v for k,v in env[2]["typed_delegation"].items() if k != "payload"}
    expected["run_id"] = status["run_id"]
    typed.validate_result(status, expected)
    changed = copy.deepcopy(status)
    changed["provenance"][field] = value
    with pytest.raises(typed.TypedDelegationError):
        typed.validate_result(changed, expected)

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_identity","missing_version","nonobject_provenance","malformed_output","extra_output","unaccepted_output","stale"])
async def test_strict_result_validator_rejects_malformed_and_stale_data(env, kind):
    _, status = await admitted(env)
    expected = {k:v for k,v in env[2]["typed_delegation"].items() if k != "payload"}
    expected["run_id"] = status["run_id"]
    changed = copy.deepcopy(status)
    if kind == "missing_identity": del changed["provenance"]["member_id"]
    elif kind == "missing_version": del changed["provenance"]["protocol_version"]
    elif kind == "nonobject_provenance": changed["provenance"] = []
    elif kind == "malformed_output": changed["output"]["sha256"] = "malformed"
    elif kind == "extra_output": changed["output"]["raw_message"] = "not allowed"
    elif kind == "unaccepted_output": changed["status"] = "cancelled"
    elif kind == "stale": expected["not_before"] = time.time()+100
    with pytest.raises(typed.TypedDelegationError):
        typed.validate_result(changed, expected)

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("input","generic"),("model","provider"),("messages",[]),("session_id","hidden"),("approved",True)])
async def test_mixed_generic_envelope_cannot_fall_through(env, field, value):
    adapter, client, body, headers, _ = env
    body[field] = value
    response = await client.post("/v1/runs", json=body, headers=headers)
    assert response.status == 400
    assert not adapter._active_run_tasks
    adapter._create_agent.assert_not_called()

@pytest.mark.asyncio
async def test_legacy_grant_has_no_typed_authority_and_profile_mismatch_denies(env):
    adapter, client, body, headers, info = env
    claims = decode_room_grant(SECRET, info["grant"], permission="dispatch")
    kwargs={k:claims[k] for k in ("grant_id", *IDENTITY, "target_install_id", "target_profile", "execution_policy_digest")}
    legacy = issue_room_grant(SECRET, **kwargs)
    denied = await client.post("/v1/runs",json=body,headers={**headers,"Authorization":"HermesRoom "+legacy})
    assert denied.status in (401,403)
    body["typed_delegation"]["target_profile"]="other-profile"
    denied = await client.post("/v1/runs",json=body,headers=headers)
    assert denied.status == 403
    assert not adapter._active_run_tasks

@pytest.mark.asyncio
async def test_grant_runtime_ceiling_is_narrower_than_server_and_approval_cannot_widen(env):
    adapter, client, body, headers, _ = env
    authority={**AUTHORITY,"max_runtime_ms":100}
    response=await client.post("/v1/room-members/invitations",json={**IDENTITY,"typed_authority":authority},
        headers={"Authorization":"Bearer test-admin-key-for-invitation-only"})
    assert response.status == 201
    headers={**headers,"Authorization":"HermesRoom "+(await response.json())["grant"]}
    body["typed_delegation"]["max_runtime_ms"]=101
    denied=await client.post("/v1/runs",json=body,headers=headers)
    assert denied.status==403
    body["typed_delegation"]["max_runtime_ms"]=100
    accepted,status=await admitted(env,body=body,headers=headers)
    # This verifies grant admission; private state persistence can consume the
    # 100 ms budget, so a correctly fenced deadline is also a valid outcome.
    assert status["status"] in ("completed","timed_out")
    assert status["provenance"]["max_runtime_ms"]==100
    if status["status"]=="timed_out":
        assert status["error_category"]=="deadline_exceeded"
        assert status["provenance"]["error_category"]=="deadline_exceeded"
        assert "output" not in status
    approval=await client.post("/v1/runs/"+accepted["run_id"]+"/approval",json={"choice":"smart","approved":True},headers=headers)
    assert approval.status==403
    body["typed_delegation"]["task_id"]="over-ceiling"
    body["typed_delegation"]["capability_ceiling"]=["terminal"]
    denied=await client.post("/v1/runs",json=body,headers={**headers,"Idempotency-Key":"room:over-ceiling:1"})
    assert denied.status==403
    adapter._create_agent.assert_not_called()

@pytest.mark.asyncio
async def test_idempotency_files_private_and_confidential_values_not_logged(env, caplog):
    adapter, _, _, _, info=env
    await admitted(env)
    path=Path(adapter._run_idempotency_store._db_path)
    for suffix in ("","-wal","-shm"):
        candidate=Path(str(path)+suffix)
        if candidate.exists(): assert candidate.stat().st_mode & 0o777==0o600
    for value in (info["grant"],"test-admin-key-for-invitation-only","typed-contract-test-material-00000"):
        assert value not in caplog.text
    adapter._memory_sessions.checkout = Mock(side_effect=AssertionError("memory initialization reached"))
    env[2]["typed_delegation"]["task_id"]="isolated-second"
    await admitted(env,headers={**env[3],"Idempotency-Key":"room:isolated-second:1"})
    adapter._memory_sessions.checkout.assert_not_called()

@pytest.mark.asyncio
async def test_cancelled_live_handler_keeps_concurrency_slot(env, monkeypatch):
    adapter, client, body, headers, _=env
    adapter._max_concurrent_runs=1
    started,release=asyncio.Event(),asyncio.Event()
    original=typed.TASK_HANDLERS[typed.TASK_TYPE]
    async def delayed(payload):
        started.set()
        await release.wait()
        return await original(payload)
    monkeypatch.setitem(typed.TASK_HANDLERS,typed.TASK_TYPE,delayed)
    response=await client.post("/v1/runs",json=body,headers=headers)
    assert response.status==202
    run_id=(await response.json())["run_id"]
    try:
        await asyncio.wait_for(started.wait(),3)
        cancelled=await client.post("/v1/runs/"+run_id+"/stop",json={},headers=headers)
        assert cancelled.status==200
        another=copy.deepcopy(body)
        another["typed_delegation"]["task_id"]="second-slot"
        h={**headers,"Idempotency-Key":"room:second-slot:1"}
        denied=await client.post("/v1/runs",json=another,headers=h)
        assert denied.status==429
        assert adapter.active_agent_work_count()==1
    finally:
        release.set()
        await asyncio.gather(*list(adapter._active_run_tasks.values()))
    assert adapter.active_agent_work_count()==0
    accepted,status=await admitted(env,body=another,headers=h)
    assert status["status"]=="completed"

@pytest.mark.asyncio
async def test_typed_dispatch_denies_unverified_private_storage(env,monkeypatch):
    from gateway import hosted_rooms_common as common
    adapter,client,body,headers,_=env
    real=common.os
    class UnsupportedOS:
        name="nt"
        def __getattr__(self,key):
            if key=="fchmod": raise AttributeError(key)
            return getattr(real,key)
    monkeypatch.setattr(common,"os",UnsupportedOS())
    denied=await client.post("/v1/runs",json=body,headers=headers)
    assert denied.status==503
    assert not adapter._active_run_tasks
    adapter._create_agent.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("permission_error",[False,True])
async def test_typed_invitation_denies_private_storage_failure_before_secrets(env,monkeypatch,permission_error):
    from gateway import hosted_rooms_common as common
    adapter,client,body,headers,_=env
    secret=Mock(side_effect=AssertionError("signing secret reached"))
    monkeypatch.setattr(adapter,"_room_grant_secret",secret)
    if permission_error:
        def deny(*args,**kwargs): raise PermissionError("test")
    else:
        def deny(*args,**kwargs): return False
    monkeypatch.setattr(common,"secure_sqlite_files",deny)
    denied=await client.post("/v1/room-members/invitations",json={**IDENTITY,"typed_authority":AUTHORITY},
        headers={"Authorization":"Bearer test-admin-key-for-invitation-only"})
    assert denied.status==503
    secret.assert_not_called()
