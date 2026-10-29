"""Behavioral failure-injection tests: never connect to a real conversation."""
import copy
from contextlib import redirect_stderr
from email.utils import formatdate
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/parallel-dev-coordination/scripts"))
from capacity_recovery import (Lock, NativeHost, Recovery, Store, capacity_error,
                               file_hash, load_config, retry_after, main)
from recovery_rpc import Rpc, RpcError
from recovery_admission import RecoveryAdmission
from supervision_store import SupervisionStore


class Host:
    def __init__(self):
        self.data = {
            "thread": {"id": "test-thread", "status": {"type": "idle"}, "model": "primary",
                       "reasoningEffort": "high", "canAcceptDirectInput": True},
            "turn": {"id": "failed-1", "status": "failed", "completedAt": 1000,
                     "itemsView": "summary", "items": [{"type": "userMessage"}],
                     "error": {"codexErrorInfo": "serverOverloaded"}},
            "goal": {"status": "active", "objective": "Keep the current task"},
            "queued": False, "terminals": False,
            "models": {"primary": ["high"], "gpt-6.1-sol": ["high"]},
        }
        self.calls, self.fail, self.change, self.reads = [], False, None, 0
        self.context = None

    def snapshot(self, tid):
        self.reads += 1
        if self.change and self.reads == self.change[0]:
            self.change[1](self.data)
        result = copy.deepcopy(self.data)
        if self.context:
            result["recoveryContext"] = self.context(result)
        return result

    def send(self, tid, model, effort, prompt, message_id):
        self.calls.append((tid, model, effort, prompt, message_id))
        if self.fail:
            raise TimeoutError("server may have accepted before response was lost")
        self.data["thread"].update(model=model, reasoningEffort=effort)
        self.data["turn"] = {"id": "sent-"+str(len(self.calls)), "status": "inProgress",
                             "items": [], "itemsView": "summary"}
        self.data["thread"]["status"] = {"type": "active", "activeFlags": []}
        return {"turn": copy.deepcopy(self.data["turn"])}

    def fail_again(self, now):
        self.data["thread"]["status"] = {"type": "idle"}
        self.data["turn"].update(status="failed", completedAt=now,
                                 error={"codexErrorInfo": "serverOverloaded"})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        handoff = self.root / "handoff.md"
        handoff.write_text("Frozen current task; only remaining steps; one file owner.", encoding="utf-8")
        self.target = {"threadId": "test-thread", "authorized": True, "exclusiveRecoveryOwner": True,
                       "model": "primary", "effort": "high", "modelLocked": False,
                       "fallbackModels": ["gpt-6.1-sol"], "batchId": "batch-1",
                       "handoffPath": str(handoff), "handoffSha256": file_hash(handoff),
                       "nativeFailureAttemptUpperBound": 30, "taskBindingId": "binding-1", "executor": "fixture-owner",
                       "capability": {"state": "verified_for_scope", "authorizationRef": "fixture-human-authorization",
                                      "preflightEvidence": {"fixture": True}, "resultEvidence": {"fixture": True}},
                       "nativeRetryEvidence": {"runtimeVersion": "fixture-v1", "provider": "fixture-provider",
                                               "route": "fixture-route", "model": "primary", "upperBound": 30,
                                               "evidenceSha256": "a"*64, "validUntil": 100000}}
        self.target["nativeRetryEvidenceByModel"] = {"gpt-6.1-sol": dict(self.target["nativeRetryEvidence"], model="gpt-6.1-sol")}
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps({"version": 2, "threads": [self.target]}))
        self.config, _ = load_config(self.path)
        self.config["threads"] = [self.target]
        self.store = Store(self.root / "state.json")
        self.host, self.events, self.now = Host(), [], 1000.0
        self.shared = SupervisionStore(self.root / "shared.db", self.root / "registry")
        self.fixture_run = 0
        self.admission = self.make_admission()
        self.host.context = lambda s: {
            "threadId": self.target["threadId"], "taskBindingId": self.target["taskBindingId"],
            "turnId": s["turn"]["id"], "batchId": self.target["batchId"],
            "handoffSha256": self.target["handoffSha256"], "observedAt": self.now,
            "readSucceeded": True, "runtimeInstanceId": "fixture-instance", "taskState": "blocked_capacity",
            "taskBindingValid": True, "unfinishedWork": True, "ownershipValid": True,
            "approvalPending": False, "budgetStopped": False, "runningOperations": [],
            "nativeRuntime": {"runtimeVersion": "fixture-v1", "provider": "fixture-provider", "route": "fixture-route"}}
        self.engine = self.engine_for(self.store)

    def make_admission(self):
        self.fixture_run += 1
        run_id = "fixture-run-"+str(self.fixture_run)
        contract = {"start": 1000, "deadline": 10000, "owner": "fixture-owner",
                    "targets": [{"threadId": self.target["threadId"], "taskBindingId": self.target["taskBindingId"],
                                 "kind": "independent", "recoveryAuthorization": copy.deepcopy(self.target)}],
                    "policy": {"maxAdditionalTurnsPerIncident": 3, "maxAdditionalTurnsPerMonitoringRun": 6,
                               "maxFailureAttemptAllowance": 120, "maxFailureAttemptAllowancePerMonitoringRun": 120,
                               "maxModelDispatchesPerMonitoringRun": 12,
                               "minModelDispatchIntervalPerRecipientSeconds": 300}}
        self.shared.create_run(run_id, contract, 1000)
        return RecoveryAdmission(self.shared, run_id, contract)

    def engine_for(self, store, execute=True):
        return Recovery(self.host, store, self.config, self.events.append, execute,
                        clock=lambda: self.now, jitter=lambda a, b: 1.0, admission=self.admission)

    def authorize_fixture_changes(self):
        self.assertFalse(self.store.data["threads"])
        self.admission = self.make_admission()
        self.store = Store(self.root / ("state-"+str(self.fixture_run)+".json"))
        self.engine = self.engine_for(self.store)

    def step(self):
        return self.engine.step(self.target)

    def send_first(self):
        self.step()
        self.now += 61
        self.step()
        self.assertEqual(len(self.host.calls), 1)

    def test_backoff_native_retry_and_same_model_first(self):
        self.engine.event({"method": "error", "params": {"threadId": "test-thread", "turnId": "failed-1", "willRetry": True}})
        self.assertIsNone(self.step())
        self.engine.event({"method": "turn/completed", "params": {"threadId": "test-thread", "turn": {"id": "failed-1"}}})
        self.step()
        self.now += 59
        self.step()
        self.assertEqual(self.host.calls, [])
        self.now += 2
        self.step()
        self.assertEqual(self.host.calls[0][1:3], ("primary", "high"))

    def test_fallback_preserves_effort_and_runtime_verified(self):
        self.send_first()
        self.host.fail_again(self.now)
        self.step()
        self.now += 121
        self.step()
        self.assertEqual(self.host.calls[-1][1:3], ("gpt-6.1-sol", "high"))
        self.host.data["turn"]["status"] = "completed"
        self.host.data["thread"]["status"] = {"type": "idle"}
        self.assertEqual(self.step()["state"], "awaiting_verification")

    def test_ack_does_not_prove_effective_model(self):
        self.send_first()
        self.host.data["thread"]["reasoningEffort"] = "medium"
        self.assertEqual(self.step()["reason"], "effective_model_or_effort_mismatch")

    def test_write_ahead_intent_is_not_a_false_failure_notice(self):
        self.send_first()
        self.assertFalse(any(e["state"] == "uncertain" for e in self.events))

    def test_crash_after_intent_is_reported_once_without_resend(self):
        self.send_first()
        state = self.store.data["threads"]["test-thread"]
        state.update(state="uncertain", reason="dispatch_intent")
        self.store.save()
        self.engine = self.engine_for(Store(self.store.path))
        before = len(self.events)
        self.assertEqual(self.step()["reason"], "prior_dispatch_outcome_unknown")
        self.step()
        self.assertEqual(len(self.events), before+1)
        self.assertEqual(len(self.host.calls), 1)

    def test_locked_target_never_changes_model(self):
        self.target.update(modelLocked=True, fallbackModels=[])
        self.authorize_fixture_changes()
        self.send_first()
        self.host.fail_again(self.now)
        self.step(); self.now += 121; self.step()
        self.assertEqual([c[1] for c in self.host.calls], ["primary", "primary"])

    def test_retries_have_total_native_budget(self):
        self.config["policy"]["maxFailureAttemptAllowance"] = 60
        self.send_first()
        self.host.fail_again(self.now)
        self.assertEqual(self.step()["state"], "cooldown")
        self.now += 9999
        self.step()
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(self.store.data["threads"]["test-thread"]["reservedFailureAttempts"], 60)

    def test_max_turns_and_deadline(self):
        self.config["policy"]["maxRecoveryTurns"] = 1
        self.send_first(); self.host.fail_again(self.now)
        self.assertEqual(self.step()["reason"], "retry_budget_exhausted")

    def test_restart_does_not_reset_due_or_budget(self):
        self.step(); self.now += 20
        self.engine = self.engine_for(Store(self.store.path))
        self.step()
        self.assertEqual(self.host.calls, [])
        self.now += 41
        self.step()
        self.assertEqual(len(self.host.calls), 1)

    def test_uncertain_send_never_replayed_even_after_restart(self):
        self.host.fail = True
        self.send_first()
        self.assertEqual(self.step()["state"], "uncertain")
        self.engine = self.engine_for(Store(self.store.path))
        self.now += 10000
        self.step()
        self.assertEqual(len(self.host.calls), 1)

    def test_observe_mode_never_sends(self):
        self.engine.execute = False
        self.step(); self.now += 61
        self.assertEqual(self.step()["state"], "observed")
        self.assertEqual(self.host.calls, [])

    def test_pause_goal_limit_queue_and_background_block(self):
        for field, value, reason in (("goal", {"status": "paused"}, "goal_not_active"),
                                     ("goal", {"status": "usageLimited"}, "goal_not_active"),
                                     ("queued", True, "user_input_pending"),
                                     ("terminals", True, "background_operation_running")):
            with self.subTest(field=field, value=value):
                self.store.data["threads"] = {}
                original = self.host.data[field]
                self.host.data[field] = value
                self.assertEqual(self.step()["reason"], reason)
                self.host.data[field] = original
        self.assertEqual(self.host.calls, [])

    def test_newer_user_turn_cancels_scheduled_retry(self):
        self.step(); self.now += 61
        self.host.data["turn"]["id"] = "user-new-turn"
        self.assertEqual(self.step()["state"], "suppressed")
        self.assertEqual(self.host.calls, [])

    def test_changed_goal_and_handoff_block(self):
        self.step(); self.now += 61
        self.host.data["goal"]["objective"] = "another task"
        self.assertEqual(self.step()["reason"], "goal_changed")

    def test_last_moment_change_cancels(self):
        self.step(); self.now += 61
        self.host.change = (3, lambda d: d.update(queued=True))
        self.assertEqual(self.step()["reason"], "changed_during_preflight")
        self.assertEqual(self.host.calls, [])

    def test_stop_during_final_read_cancels(self):
        self.step(); self.now += 61
        flag = [False]
        self.engine.cancelled = lambda tid: flag[0]
        self.host.change = (3, lambda _: flag.__setitem__(0, True))
        self.assertEqual(self.step()["reason"], "user_stop_or_config_changed")
        self.assertEqual(self.host.calls, [])

    def test_side_effects_without_reconciliation_fail_closed(self):
        self.host.data["turn"]["items"].append({"id": "write-1", "type": "fileChange", "status": "completed"})
        self.step(); self.now += 61
        self.assertEqual(self.step()["reason"], "side_effects_need_reconciliation")
        self.assertEqual(self.host.calls, [])

    def test_project_gate_checks_real_receipt(self):
        gate = self.root / "gate.py"
        gate.write_text('import sys,json\nr=json.load(sys.stdin)\nr.update(safe=True,runningOperations=[],pendingApprovals=[],ownershipValid=True,changesReconciled=True)\nprint(json.dumps(r))\n')
        self.target["reconcileCommand"] = [sys.executable, str(gate)]
        self.authorize_fixture_changes()
        self.host.data["turn"]["items"].append({"id": "write-1", "type": "fileChange", "status": "completed"})
        self.send_first()

    def test_missing_summary_and_stale_failure_not_retried(self):
        self.host.data["turn"]["itemsView"] = "notLoaded"
        self.step(); self.now += 61
        self.assertEqual(self.step()["state"], "attention")
        self.store.data["threads"] = {}
        self.now += 1000
        self.assertEqual(self.step()["reason"], "stale_or_unknown_failure_time")

    def test_retry_after_too_long_enters_cooldown(self):
        self.host.data["turn"]["error"]["retryAfterSeconds"] = 1000
        self.assertEqual(self.step()["reason"], "retry_after_exceeds_deadline")

    def test_late_retry_after_survives_restart(self):
        self.step()
        self.engine.event({"method": "error", "params": {"threadId": "test-thread",
            "turnId": "failed-1", "willRetry": False,
            "error": {"codexErrorInfo": "serverOverloaded", "retryAfterSeconds": 300}}})
        self.engine = self.engine_for(Store(self.store.path))
        self.now += 100
        self.step()
        self.assertEqual(self.host.calls, [])
        self.now += 201
        self.step()
        self.assertEqual(len(self.host.calls), 1)

    def test_unrelated_events_do_not_affect_state(self):
        self.engine.event({"method": "error", "params": {"threadId": "unrelated",
            "turnId": "failed-1", "willRetry": True}})
        self.assertNotIn("signals", self.store.data)

    def test_handoff_mutation_blocks_dispatch(self):
        self.step()
        Path(self.target["handoffPath"]).write_text("new batch", encoding="utf-8")
        self.now += 61
        self.assertEqual(self.step()["reason"], "handoff_changed")
        self.assertEqual(self.host.calls, [])

    def test_interrupted_turn_never_resumed(self):
        self.send_first()
        self.host.data["turn"]["status"] = "interrupted"
        self.assertEqual(self.step()["reason"], "turn_interrupted")

    def test_gate_refusal_preserves_changes_without_dispatch(self):
        gate = self.root / "gate.py"
        gate.write_text('print("{\\"safe\\": false}")\n')
        self.target["reconcileCommand"] = [sys.executable, str(gate)]
        self.authorize_fixture_changes()
        self.host.data["turn"]["items"].append({"type": "commandExecution", "status": "completed"})
        self.step(); self.now += 61
        self.assertEqual(self.step()["state"], "attention")
        self.assertEqual(self.host.calls, [])

    def test_gate_flood_is_bounded(self):
        gate = self.root / "gate.py"
        gate.write_text('print("x"*1000000)\n')
        self.target["reconcileCommand"] = [sys.executable, str(gate)]
        self.authorize_fixture_changes()
        self.host.data["turn"]["items"].append({"type": "commandExecution", "status": "completed"})
        self.step(); self.now += 61
        self.assertEqual(self.step()["state"], "attention")

    def test_policy_change_does_not_reset_budget(self):
        self.step()
        self.config["policy"]["maxRecoveryTurns"] = 5
        self.assertEqual(self.step()["reason"], "contract_changed")

    def test_successive_failure_exhausts_approved_route(self):
        self.send_first()
        for wait in (121, 241):
            self.host.fail_again(self.now)
            self.step(); self.now += wait; self.step()
        self.host.fail_again(self.now)
        self.assertEqual(self.step()["state"], "cooldown")
        self.assertEqual([c[1] for c in self.host.calls], ["primary", "gpt-6.1-sol", "gpt-6.1-sol"])

    def test_unknown_thread_runtime_settings_fail_closed(self):
        del self.host.data["thread"]["reasoningEffort"]
        self.assertEqual(self.step()["reason"], "runtime_settings_changed")

    def test_unavailable_effort_not_downgraded(self):
        self.host.data["models"]["primary"] = ["low"]
        self.step(); self.now += 61
        self.assertEqual(self.step()["reason"], "approved_model_or_effort_unavailable")

    def test_no_subagent_recovery(self):
        self.host.data["thread"]["parentThreadId"] = "parent"
        self.assertEqual(self.step()["reason"], "not_independent_direct_input_thread")

    def test_notices_do_not_repeat_or_contain_transcript(self):
        self.host.data["turn"]["error"]["message"] = "PRIVATE_PAYLOAD"
        self.step()
        n = len(self.events)
        for _ in range(10): self.step()
        self.assertEqual(len(self.events), n)
        self.assertNotIn("PRIVATE_PAYLOAD", json.dumps(self.events)+self.store.path.read_text())

    def test_invalid_fallback_and_model_lock_config_rejected(self):
        for changes in ({"fallbackModels": ["unapproved"]}, {"modelLocked": True},
                        {"authorized": False}, {"nativeFailureAttemptUpperBound": 0}):
            data = {"version": 1, "threads": [dict(self.target, **changes)]}
            self.path.write_text(json.dumps(data))
            with self.assertRaises(ValueError): load_config(self.path)

    def test_duplicate_owner_lock(self):
        with Lock(self.root / "owner.lock"):
            with self.assertRaises(RuntimeError):
                with Lock(self.root / "owner.lock"): pass

    def test_execute_requires_shared_admission(self):
        with self.assertRaises(ValueError):
            Recovery(self.host, self.store, self.config, self.events.append, execute=True)

    def test_cli_v1_execute_cannot_open_host_or_bypass_shared_store(self):
        self.path.write_text(json.dumps({"version": 1, "threads": [self.target]}))
        with patch("capacity_recovery.Rpc") as rpc, redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as result:
                main(["--config", str(self.path), "--state-dir", str(self.root / "new-state"),
                      "--codex", str(self.root / "codex.exe"), "--sock", str(self.root / "sock"), "--execute"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("requires v2", error.getvalue())
        rpc.assert_not_called()

    def test_first_required_read_failure_sends_zero(self):
        self.step()
        self.now += 61
        self.host.snapshot = lambda tid: (_ for _ in ()).throw(ConnectionError("fixture"))
        self.assertEqual(self.step()["reason"], "required_read_or_authority_failed")
        self.assertEqual(self.host.calls, [])

    def test_final_required_read_failure_sends_zero(self):
        self.step()
        self.now += 61
        def fail(data):
            raise ConnectionError("fixture final read")
        self.host.change = (3, fail)
        self.assertEqual(self.step()["reason"], "required_read_or_authority_failed")
        self.assertEqual(self.host.calls, [])

    def test_missing_native_runtime_evidence_disables_execution(self):
        self.host.context = None
        self.step(); self.now += 61
        self.assertEqual(self.step()["reason"], "required_read_or_authority_failed")
        self.assertEqual(self.host.calls, [])

    def test_legacy_intent_cannot_be_silently_migrated(self):
        legacy = Store(self.root / "legacy.json")
        legacy.data["threads"]["test-thread"] = {"state": "uncertain", "attempts": 1,
                                                "reservedFailureAttempts": 60}
        legacy.save()
        with self.assertRaises(ValueError):
            self.make_admission().bind_local_state(legacy)

    def test_changing_state_directory_cannot_reset_run(self):
        with self.assertRaises(ValueError):
            self.admission.bind_local_state(Store(self.root / "different-state.json"))

    def test_completed_turn_and_acceptance_are_not_task_recovery(self):
        self.send_first()
        self.assertEqual(self.store.data["threads"]["test-thread"]["state"], "sent")
        self.host.data["turn"]["status"] = "completed"
        self.host.data["thread"]["status"] = {"type": "idle"}
        self.assertEqual(self.step()["state"], "awaiting_verification")
        self.assertNotIn("done", [event["state"] for event in self.events])


class ClassificationTests(unittest.TestCase):
    def test_only_explicit_capacity(self):
        for error in ({"codexErrorInfo": "serverOverloaded"}, {"code": "server_overloaded"},
                      {"message": "The model is temporarily overloaded. Retry later."},
                      {"message": "模型容量不足，请稍后重试"}):
            self.assertTrue(capacity_error(error), error)
        for error in ({"codexErrorInfo": "usageLimitExceeded"}, {"codexErrorInfo": "rateLimitExceeded"},
                      {"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}}},
                      {"message": "Server overloaded; retry later.", "code": -32001},
                      {"message": "model is busy but quota exceeded"}, {"message": "WebSocket disconnected"},
                      {"codexErrorInfo": "unauthorized", "message": "model is busy"}, None):
            self.assertFalse(capacity_error(error), error)

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(retry_after({"additionalDetails": "Retry-After: 180\r\n"}, 1000), 180)
        self.assertEqual(retry_after({"additionalDetails": "Retry-After: "+formatdate(1200, usegmt=True)}, 1000), 200)
        self.assertEqual(retry_after({"retryAfterSeconds": -1}, 1000), 0)
        self.assertTrue(retry_after({"retryAfterSeconds": "invalid"}, 1000) > 100000)


class RpcTests(unittest.TestCase):
    def test_real_stdio_roundtrip_and_error_event(self):
        code = '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if "id" not in m: continue
 if m["method"]=="fail": print(json.dumps({"id":m["id"],"error":{"code":-32001,"message":"private"}}),flush=True)
 else:
  print(json.dumps({"method":"error","params":{"willRetry":True,"threadId":"t","turnId":"x"}}),flush=True)
  print(json.dumps({"id":m["id"],"result":{"ok":True}}),flush=True)
'''
        rpc = Rpc([sys.executable, "-u", "-c", code], timeout=2)
        try:
            self.assertTrue(rpc.initialize()["ok"])
            self.assertEqual(rpc.wait(1)["method"], "error")
            with self.assertRaises(RpcError) as cm: rpc.call("fail", {})
            self.assertEqual(cm.exception.code, -32001)
            self.assertNotIn("private", str(cm.exception))
        finally: rpc.close()

    def test_oversized_response_disconnects(self):
        rpc = Rpc([sys.executable, "-u", "-c", 'import sys;sys.stdin.readline();print("x"*4194305,flush=True)'], timeout=3)
        try:
            with self.assertRaises(ConnectionError): rpc.call("test", {})
        finally: rpc.close()

    def test_approval_request_is_never_auto_approved(self):
        code = 'import sys,json;sys.stdin.readline();print(json.dumps({"id":5,"method":"item/commandExecution/requestApproval","params":{}}),flush=True)'
        rpc = Rpc([sys.executable, "-u", "-c", code], timeout=3)
        try:
            with self.assertRaises(ConnectionError): rpc.call("test", {})
        finally: rpc.close()


class NativeAdapterTests(unittest.TestCase):
    def test_bounded_metadata_and_preserved_permissions(self):
        calls = []
        class FakeRpc:
            def call(self, method, params):
                calls.append((method, params))
                return {"thread/read": {"thread": {}}, "thread/turns/list": {"data": []},
                        "thread/goal/get": {"goal": None}, "thread/queue/list": {"data": []},
                        "thread/backgroundTerminals/list": {"data": []},
                        "model/list": {"data": [{"model": "m", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]},
                        "turn/start": {"turn": {"id": "new"}}, "thread/resume": {}}[method]
        host = NativeHost(FakeRpc())
        host.snapshot("t")
        host.snapshot("t")
        self.assertEqual(sum(m == "model/list" for m, _ in calls), 1)
        self.assertIn(("thread/turns/list", {"threadId": "t", "limit": 1, "sortDirection": "desc", "itemsView": "summary"}), calls)
        host.attach("t")
        self.assertEqual(calls[-1], ("thread/resume", {"threadId": "t", "excludeTurns": True}))
        host.send("t", "m", "high", "bounded handoff", "msg")
        params = calls[-1][1]
        self.assertEqual(set(params), {"threadId", "model", "effort", "input", "clientUserMessageId"})


if __name__ == "__main__":
    unittest.main()
