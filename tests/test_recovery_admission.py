"""Recovery/parent-tool safety integration; no host tools or real messages."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/parallel-dev-coordination/scripts"))
from recovery_admission import RecoveryAdmission
from supervision_store import SupervisionStore


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        native = {"runtimeVersion": "fixture-v1", "provider": "fixture-provider", "route": "fixture-route",
                  "model": "primary", "upperBound": 30, "evidenceSha256": "a"*64, "validUntil": 2000}
        self.targets = [{"threadId": name, "taskBindingId": name+"-binding", "executor": "owner",
                         "model": "primary", "effort": "high", "modelLocked": False,
                         "fallbackModels": ["gpt-6.1-sol"], "authorized": True, "exclusiveRecoveryOwner": True,
                         "batchId": "batch", "handoffSha256": "b"*64,
                         "capability": {"state": "trial_authorized", "trialMaxDispatches": 1,
                                        "authorizationRef": "fixture-human-authorization",
                                        "preflightEvidence": {"fixture": True}},
                         "nativeFailureAttemptUpperBound": 30, "nativeRetryEvidence": native,
                         "nativeRetryEvidenceByModel": {"gpt-6.1-sol": dict(native, model="gpt-6.1-sol")}}
                        for name in ("independent", "child")]
        self.targets[1].update(parentThreadId="parent", parentTaskBindingId="parent-binding")
        self.contract = {"start": 1000, "deadline": 1900, "owner": "owner",
                         "targets": [{"threadId": t["threadId"], "taskBindingId": t["taskBindingId"],
                                      "kind": t["threadId"], "recoveryAuthorization": t} for t in self.targets],
                         "policy": {"maxAdditionalTurnsPerIncident": 3, "maxAdditionalTurnsPerMonitoringRun": 6,
                                    "maxFailureAttemptAllowance": 120, "maxFailureAttemptAllowancePerMonitoringRun": 120,
                                    "maxModelDispatchesPerMonitoringRun": 12,
                                    "minModelDispatchIntervalPerRecipientSeconds": 300}}
        self.shared = SupervisionStore(self.root / "state.db", self.root / "registry")

    def start(self):
        self.shared.create_run("run", self.contract, 1000)
        return RecoveryAdmission(self.shared, "run", self.contract)

    def snapshot(self, target):
        return {"readSucceeded": True, "observedAt": 1000, "runtimeInstanceId": "host-1",
                "fingerprint": "fresh-"+target["threadId"], "threadId": target["threadId"],
                "taskBindingId": target["taskBindingId"], "latestCapacityFailure": True,
                "nativeRetryEnded": True, "taskBindingValid": True, "unfinishedWork": True,
                "ownershipValid": True, "sideEffectsReconciled": True, "handoffValid": True,
                "settingsVerified": True, "queued": False, "paused": False, "approvalPending": False,
                "budgetStopped": False, "runningOperations": [], "taskState": "blocked_capacity",
                "goalStatus": "active", "notBefore": 999, "failedTurn": "failure-"+target["threadId"],
                "rootTurn": "failure-"+target["threadId"], "observedModel": "primary", "observedEffort": "high",
                "selectedModelSupported": True, "parentThreadId": "parent" if target["threadId"] == "child" else None,
                "nativeRuntime": {"runtimeVersion": "fixture-v1", "provider": "fixture-provider", "route": "fixture-route"}}

    def parent(self):
        return {"threadId": "parent", "executor": "owner", "readSucceeded": True, "observedAt": 1000,
                "taskBindingId": "parent-binding", "taskBindingValid": True, "budgetStopped": False,
                "runtimeInstanceId": "host-1", "fingerprint": "parent-fresh", "safeBoundary": True,
                "authorizedToolAvailable": True, "queued": False, "paused": False, "approvalPending": False,
                "runningOperations": [], "goalStatus": "active"}

    def request(self, target):
        return {"incidentId": "incident-"+target["threadId"], "attemptId": "attempt-"+target["threadId"]+"-1",
                "rootTurn": "failure-"+target["threadId"], "failedTurn": "failure-"+target["threadId"],
                "incidentDeadline": 1800, "attemptSequence": 1, "selectedModel": "primary", "effort": "high"}

    def test_first_child_trial_needs_no_historical_success(self):
        admission = self.start()
        child = self.targets[1]
        permit = admission.child_permit(child, self.snapshot(child), self.parent(), self.request(child), 1000)
        self.assertTrue(permit["allowed"])
        self.assertEqual(permit["c4"], "disabled_pending_real_host_trial")
        self.assertNotIn("resultEvidence", child["capability"])
        self.assertTrue(admission.claim_child_for_send(permit["intentId"], child, self.snapshot(child), self.parent(), 1000)["allowed"])
        admission.record_outcome(permit["intentId"], "accepted", {"turnId": "new-child-turn"})
        admission.record_outcome(permit["intentId"], "awaiting_verification", {"turnId": "new-child-turn"})
        self.assertEqual(self.shared.run_status("run")["intents"][0]["status"], "awaiting_verification")

    def test_independent_and_child_compete_for_last_run_turn(self):
        self.contract["policy"]["maxAdditionalTurnsPerMonitoringRun"] = 1
        admission = self.start()
        barrier = threading.Barrier(2)
        def reserve(index):
            local = RecoveryAdmission(SupervisionStore(self.root / "state.db", self.root / "registry"), "run")
            target = self.targets[index]
            barrier.wait()
            if index:
                return local.child_permit(target, self.snapshot(target), self.parent(), self.request(target), 1000)
            return local.reserve(target, self.snapshot(target), self.request(target), 1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (0, 1)))
        self.assertEqual(sum(bool(r["allowed"]) for r in results), 1)
        status = self.shared.run_status("run")
        self.assertEqual(status["usedRecoveryTurns"], 1)
        self.assertEqual(status["usedFailureAttempts"], 90)
        self.assertEqual(len(status["intents"]), 1)
        self.assertEqual(len(admission.store.run_status("run")["intents"]), 1)

    def test_native_failure_budget_stops_with_turns_remaining(self):
        self.contract["policy"]["maxFailureAttemptAllowancePerMonitoringRun"] = 30
        admission = self.start()
        target = self.targets[0]
        result = admission.reserve(target, self.snapshot(target), self.request(target), 1000)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "run_failure_budget")
        status = self.shared.run_status("run")
        self.assertEqual((status["usedRecoveryTurns"], status["usedFailureAttempts"]), (0, 30))

    def test_unknown_trial_survives_restart_no_second_permit(self):
        admission = self.start()
        target = self.targets[1]
        snapshot, parent, request = self.snapshot(target), self.parent(), self.request(target)
        result = admission.child_permit(target, snapshot, parent, request, 1000)
        admission.claim_child_for_send(result["intentId"], target, snapshot, parent, 1000)
        admission.record_outcome(result["intentId"], "unknown", {"hostResult": "timeout"})
        recovered = RecoveryAdmission(SupervisionStore(self.root / "state.db", self.root / "registry"), "run")
        self.assertFalse(recovered.child_permit(target, snapshot, parent, request, 1000)["allowed"])
        self.assertFalse(recovered.claim_child_for_send(result["intentId"], target, snapshot, parent, 1000)["allowed"])
        self.assertEqual(recovered.store.run_status("run")["usedRecoveryTurns"], 1)

    def test_child_requires_fresh_parent_before_reservation_and_send(self):
        admission = self.start()
        target, parent = self.targets[1], self.parent()
        parent["readSucceeded"] = False
        self.assertFalse(admission.child_permit(target, self.snapshot(target), parent, self.request(target), 1000)["allowed"])
        parent["readSucceeded"] = True
        permit = admission.child_permit(target, self.snapshot(target), parent, self.request(target), 1000)
        self.assertTrue(permit["allowed"])
        self.assertFalse(admission.claim_for_send(permit["intentId"], target, self.snapshot(target), 1000)["allowed"])
        parent["queued"] = True
        self.assertFalse(admission.claim_child_for_send(permit["intentId"], target, self.snapshot(target), parent, 1000)["allowed"])

    def test_child_model_switch_is_disabled(self):
        admission = self.start()
        target = self.targets[1]
        request = dict(self.request(target), selectedModel="gpt-6.1-sol")
        result = admission.child_permit(target, self.snapshot(target), self.parent(), request, 1000)
        self.assertEqual(result["reason"], "child_model_switch_disabled")
        self.assertEqual(self.shared.run_status("run")["usedRecoveryTurns"], 0)

    def test_missing_preconditions_do_not_consume_or_send(self):
        admission = self.start()
        target = self.targets[0]
        for key in ("sideEffectsReconciled", "taskBindingValid", "nativeRuntime", "readSucceeded", "ownershipValid",
                    "queued", "nativeRetryEnded", "unfinishedWork"):
            with self.subTest(key=key):
                snapshot = self.snapshot(target)
                del snapshot[key]
                self.assertFalse(admission.reserve(target, snapshot, self.request(target), 1000)["allowed"])
        self.assertEqual(self.shared.run_status("run")["usedRecoveryTurns"], 0)

    def test_stale_native_version_or_upper_bound_rejected(self):
        admission = self.start()
        target = self.targets[0]
        snapshot = self.snapshot(target)
        snapshot["nativeRuntime"]["runtimeVersion"] = "new-runtime"
        self.assertFalse(admission.reserve(target, snapshot, self.request(target), 1000)["allowed"])
        snapshot = self.snapshot(target)
        snapshot["observedAt"] = 2001
        self.assertFalse(admission.reserve(target, snapshot, self.request(target), 2001)["allowed"])

    def test_receipt_acceptance_cannot_mark_recovered(self):
        admission = self.start()
        target = self.targets[0]
        result = admission.reserve(target, self.snapshot(target), self.request(target), 1000)
        admission.claim_for_send(result["intentId"], target, self.snapshot(target), 1000)
        admission.record_outcome(result["intentId"], "accepted", {"turnId": "next"})
        for state in ("completed", "recovered", "verified"):
            with self.assertRaises(ValueError):
                admission.record_outcome(result["intentId"], state, {"turnId": "next"})
        self.assertEqual(self.shared.run_status("run")["intents"][0]["status"], "accepted")

    def test_contract_cannot_be_replaced_by_config(self):
        self.start()
        raw = json.dumps(self.contract).encode()
        path = self.root / "contract.json"
        path.write_bytes(raw)
        config = {"version": 2, "supervision": {"runId": "run", "dbPath": str(self.root / "state.db"),
                  "registryDir": str(self.root / "registry"), "contractPath": str(path),
                  "contractSha256": hashlib.sha256(raw).hexdigest()}, "threads": [self.targets[0]]}
        self.assertEqual(RecoveryAdmission.from_config(config).run_id, "run")
        config["threads"] = [copy.deepcopy(self.targets[0])]
        config["threads"][0]["capability"]["trialMaxDispatches"] = 2
        with self.assertRaises(ValueError):
            RecoveryAdmission.from_config(config)
        config["threads"] = [self.targets[0]]
        config["supervision"]["runId"] = "new-unapproved-run"
        with self.assertRaises(Exception):
            RecoveryAdmission.from_config(config)

    def test_expired_permit_is_not_sent_or_refunded(self):
        admission = self.start()
        target = self.targets[0]
        result = admission.reserve(target, self.snapshot(target), self.request(target), 1000)
        snapshot = self.snapshot(target)
        snapshot["observedAt"] = 1016
        self.assertFalse(admission.claim_for_send(result["intentId"], target, snapshot, 1016)["allowed"])
        self.assertEqual(self.shared.run_status("run")["usedRecoveryTurns"], 1)

    def test_self_asserted_attempt_number_cannot_skip_original_model(self):
        admission = self.start()
        target = self.targets[0]
        request = dict(self.request(target), attemptSequence=2, selectedModel="gpt-6.1-sol")
        self.assertEqual(admission.reserve(target, self.snapshot(target), request, 1000)["reason"], "attempt_sequence_mismatch")

    def test_new_incident_cannot_hide_unresolved_send(self):
        self.targets[0]["capability"].update(state="verified_for_scope", resultEvidence={"fixture": True})
        admission = self.start()
        target = self.targets[0]
        permit = admission.reserve(target, self.snapshot(target), self.request(target), 1000)
        admission.claim_for_send(permit["intentId"], target, self.snapshot(target), 1000)
        admission.record_outcome(permit["intentId"], "unknown", {"hostResult": "timeout"})
        request = dict(self.request(target), incidentId="new-incident", rootTurn="new-failure", failedTurn="new-failure")
        snapshot = dict(self.snapshot(target), rootTurn="new-failure", failedTurn="new-failure")
        self.assertEqual(admission.reserve(target, snapshot, request, 1000)["reason"], "task_binding_has_unresolved_incident")

    def test_independent_entry_cannot_issue_child_permit(self):
        admission = self.start()
        target = self.targets[1]
        with self.assertRaises(ValueError):
            admission.reserve(target, self.snapshot(target), self.request(target), 1000)

    def test_unrelated_parent_cannot_obtain_child_permit(self):
        admission = self.start()
        target = self.targets[1]
        child, parent = self.snapshot(target), self.parent()
        child["parentThreadId"] = parent["threadId"] = "unrelated-parent"
        self.assertFalse(admission.child_permit(target, child, parent, self.request(target), 1000)["allowed"])

    def test_changed_settings_with_unchanged_caller_fingerprint_cancel_once(self):
        admission = self.start()
        target = self.targets[0]
        snapshot = self.snapshot(target)
        permit = admission.reserve(target, snapshot, self.request(target), 1000)
        changed = dict(snapshot, observedEffort="low")
        self.assertFalse(admission.claim_for_send(permit["intentId"], target, changed, 1000)["allowed"])
        self.assertFalse(admission.claim_for_send(permit["intentId"], target, snapshot, 1000)["allowed"])
        self.assertEqual(self.shared.run_status("run")["intents"][0]["status"], "not_sent")

    def test_same_permit_can_only_be_claimed_once_concurrently(self):
        admission = self.start()
        target, snapshot = self.targets[0], self.snapshot(self.targets[0])
        permit = admission.reserve(target, snapshot, self.request(target), 1000)
        barrier = threading.Barrier(2)
        def claim(_):
            local = RecoveryAdmission(SupervisionStore(self.root / "state.db", self.root / "registry"), "run")
            barrier.wait()
            return local.claim_for_send(permit["intentId"], target, snapshot, 1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (0, 1)))
        self.assertEqual(sum(result["allowed"] for result in outcomes), 1)
        self.assertEqual(self.shared.run_status("run")["usedRecoveryTurns"], 1)

    def test_old_shared_intent_without_migration_metadata_blocks_fresh_entry(self):
        admission = self.start()
        target = self.targets[0]
        request = dict(self.request(target), threadId=target["threadId"], taskBindingId=target["taskBindingId"],
                       executor="owner", snapshotFingerprint="old-fingerprint", nativeFailureAttemptUpperBound=30,
                       nativeRetryEvidence={"legacyReceipt": True})
        self.shared.reserve_recovery("run", request, 1000)
        new_request = dict(self.request(target), incidentId="new-incident", rootTurn="new-root", failedTurn="new-root")
        new_snapshot = dict(self.snapshot(target), rootTurn="new-root", failedTurn="new-root")
        self.assertEqual(admission.reserve(target, new_snapshot, new_request, 1000)["reason"],
                         "existing_recovery_state_requires_reconciliation")


if __name__ == "__main__":
    unittest.main()
