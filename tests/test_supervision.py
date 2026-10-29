"""Behavior tests for supervision decisions; no real chats, models or devices."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/parallel-dev-coordination/scripts"))
from supervision import Supervisor, project_record, public_sample
from supervision_store import SupervisionStore


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000
        self.contract = {"owner": "lead", "start": 1000, "deadline": 8000, "hostId": "local",
            "targets": [{"threadId": "worker", "taskBindingId": "batch", "kind": "independent"}],
            "messageAuthority": {"authorized": True, "source": "fixture"},
            "policy": {"maxModelDispatchesPerMonitoringRun": 2,
                       "minModelDispatchIntervalPerRecipientSeconds": 300, "coalesceWindowSeconds": 60}}
        self.store = SupervisionStore(self.root / "state.db", self.root / "registry")
        self.store.create_run("run", self.contract, self.now)
        self.engine = Supervisor(self.store, "run", self.contract)
        self.sample = {"threadId": "worker", "hostId": "local", "runtimeInstanceId": "host-1",
                       "observedAt": self.now, "status": "idle", "turnId": "old-turn",
                       "turnStatus": "completed"}
        self.project = {"known": True, "version": "1", "record": {
            "taskBindingId": "batch", "taskState": "completed", "assignmentState": "standby",
            "decisionOwner": "lead", "reason": "frozen-inputs", "nextReviewAt": 1600,
            "dependencyVersion": "freeze-1", "decisionRef": "decision-1"}}

    def observe(self, now=None):
        self.now = self.now if now is None else now
        self.sample["observedAt"] = self.now
        return self.engine.observe([self.sample], self.now, {"worker": self.project})

    def event(self):
        self.project["record"]["nextReviewAt"] = 999
        result = self.observe()
        self.now += 61
        self.observe()
        proposal = self.engine.prepare_notification(self.now)
        self.assertTrue(proposal["allowed"], proposal)
        return result["pending"][0], proposal

    def proof(self, name, obj=None):
        path = self.root / (name + ".json")
        path.write_text(json.dumps(obj or {"fixture": name}), encoding="utf-8")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def receipt(self, event_id, stage, **extra):
        return self.engine.owner_receipt({"runId": "run", "owner": "lead", "eventId": event_id,
            "stage": stage, "evidence": self.proof(stage), **extra}, self.now)

    def test_unchanged_valid_standby_does_not_notify(self):
        self.assertEqual(self.observe()["pending"], [])
        for now in (1060, 1120, 1180):
            self.assertEqual(self.observe(now)["pending"], [])
            self.assertFalse(self.engine.prepare_notification(now)["allowed"])

    def delivery(self, sha="a" * 64):
        self.project["record"].update(taskState="delivered_waiting_acceptance",
                                      assignmentState="assigned",
                                      deliveryRef={"path": "/delivery.json", "sha256": sha})

    def test_active_delivery_remains_actionable_across_waiting_turns(self):
        self.delivery()
        self.sample["status"] = "active"
        first = self.observe()
        self.assertEqual(first["targets"]["worker"]["state"], "delivery_needs_decision")
        self.sample.update(turnId="waiting-turn-2", status="idle")
        self.project["version"] = "owner-progress-edit"
        self.project["record"]["updatedAt"] = 1061
        second = self.observe(1061)
        self.assertEqual(first["pending"], second["pending"])
        proposal = self.engine.prepare_notification(1061)
        self.assertTrue(proposal["allowed"])
        self.engine.notification_result(proposal["notificationId"], "delivered", 1062)
        restarted = Supervisor(self.store, "run", self.contract)
        self.sample.update(turnId="waiting-turn-3", observedAt=1500, status="active")
        restarted.observe([self.sample], 1500, {"worker": self.project})
        self.assertFalse(restarted.prepare_notification(1500)["allowed"])
        self.assertEqual(len(restarted._load()[1]["events"]), 1)

    def test_new_delivery_hash_supersedes_only_old_delivery_event(self):
        self.delivery()
        old = self.observe()["pending"][0]
        self.delivery("b" * 64)
        result = self.observe(1010)
        self.assertNotEqual(result["pending"], [old])
        events = self.engine._load()[1]["events"]
        self.assertEqual(events[old]["stage"], "closed")
        self.assertEqual(len(result["pending"]), 1)

    def test_unversioned_delivery_does_not_reopen_on_progress_edit(self):
        self.delivery()
        del self.project["record"]["deliveryRef"]
        old = self.observe()["pending"]
        self.sample["turnId"] = "another-turn"
        self.project["version"] = "edited"
        self.assertEqual(self.observe(1010)["pending"], old)

    def test_delivery_does_not_override_pause_stale_terminal_or_failed_capacity(self):
        self.delivery()
        self.sample.update(status="active", activeFlags=["waitingOnUserInput"])
        self.assertEqual(self.observe()["targets"]["worker"]["state"], "paused_or_needs_human")
        self.sample.update(activeFlags=[], observedAt=1)
        result = self.engine.classify(self.contract["targets"][0], self.sample, self.project, {}, 1000)
        self.assertEqual(result["state"], "unknown")
        self.sample.update(status="systemError", turnStatus="failed", capacityFailure=True)
        self.assertEqual(self.observe(1010)["targets"]["worker"]["state"], "capacity_failure")
        self.project["record"].update(taskState="completed", assignmentState="accepted")
        self.assertEqual(self.observe(1020)["targets"]["worker"]["state"], "completed")

    def test_owner_review_context_is_visible_without_rekeying_or_accepting(self):
        self.delivery()
        old = self.observe()["pending"]
        self.project["record"]["reviewProgress"] = {
            "deliverySha256": "a" * 64, "updatedAt": "2026-10-27T12:00:00+08:00",
            "summary": "hash review finished; phone scope still pending",
            "completedChecks": ["28 file refs"], "remainingChecks": ["phone behavior"],
            "waitingOn": ["signed APK with required fields and USB device ready"],
            "privateTranscript": "must not be copied"}
        result = self.observe(1061)
        self.assertEqual(result["pending"], old)
        target = result["targets"]["worker"]
        self.assertEqual(target["state"], "delivery_needs_decision")
        self.assertEqual(target["reviewProgress"]["completedChecks"], ["28 file refs"])
        self.assertNotIn("privateTranscript", target["reviewProgress"])
        proposal = self.engine.prepare_notification(1061)
        self.assertIn("signed APK", proposal["message"])
        self.assertEqual(proposal["events"][0]["reviewProgress"], target["reviewProgress"])

    def test_old_review_progress_is_not_attached_to_new_delivery(self):
        self.delivery("b" * 64)
        self.project["record"]["reviewProgress"] = {"deliverySha256": "a" * 64, "summary": "reviewed old"}
        self.assertIsNone(self.observe()["targets"]["worker"]["reviewProgress"])

    def test_record_adapter_preserves_review_fields_and_ignores_transcript(self):
        self.delivery()
        self.project["record"]["reviewProgress"] = {"deliverySha256": "a" * 64, "waitingOn": ["device"]}
        self.project["record"]["transcript"] = "not allowed"
        proof = self.proof("record", {"task": self.project["record"]})
        record = project_record({"path": proof["path"], "pointer": "/task"})["record"]
        self.assertEqual(record["deliveryRef"]["sha256"], "a" * 64)
        self.assertEqual(record["reviewProgress"]["waitingOn"], ["device"])
        self.assertNotIn("transcript", record)

    def test_api_constructor_rejects_registered_contract_override(self):
        registered = copy.deepcopy(self.contract)
        registered["messageAuthority"]["authorized"] = False
        self.store.create_run("unauthorized", registered, self.now)
        altered = copy.deepcopy(registered)
        altered["messageAuthority"]["authorized"] = True
        with self.assertRaises(ValueError):
            Supervisor(self.store, "unauthorized", altered)

    def test_api_contract_input_mutation_cannot_enable_notifications(self):
        registered = copy.deepcopy(self.contract)
        registered["messageAuthority"]["authorized"] = False
        self.store.create_run("unauthorized", registered, self.now)
        engine = Supervisor(self.store, "unauthorized", registered)
        registered["messageAuthority"]["authorized"] = True
        self.project["record"]["nextReviewAt"] = 999
        engine.observe([self.sample], 1000, {"worker": self.project})
        self.assertEqual(engine.prepare_notification(1061)["reason"], "message_authority_missing")

    def test_batch_change_detected_while_thread_status_unchanged(self):
        self.observe()
        self.project["record"]["dependencyVersion"] = "unfrozen"
        changed = self.observe(1010)
        self.assertEqual(changed["targets"]["worker"]["reviewReason"], "standby_dependency_changed")
        self.assertEqual(len(changed["pending"]), 1)

    def test_dependency_event_survives_coalescing_polls_and_restart(self):
        self.observe()
        self.project["record"]["dependencyVersion"] = "unfrozen"
        changed = self.observe(1010)
        eid = changed["pending"][0]
        self.engine = Supervisor(self.store, "run", self.contract)
        repeated = self.observe(1071)
        self.assertEqual(repeated["targets"]["worker"]["reviewReason"], "standby_dependency_changed")
        proposal = self.engine.prepare_notification(1071)
        self.assertTrue(proposal["allowed"], proposal)
        self.assertEqual([e["eventId"] for e in proposal["events"]], [eid])

    def test_older_observation_cannot_replace_newer_pause(self):
        self.project["record"].update(taskState="in_progress", assignmentState="none")
        self.sample.update(paused=True, cursor="host-1:2")
        self.observe(1000)
        older = {**self.sample, "observedAt": 990, "cursor": "host-1:1", "paused": False,
                 "status": "systemError", "turnStatus": "failed", "capacityFailure": True}
        result = self.engine.observe([older], 1001, {"worker": self.project})
        self.assertEqual(result["targets"]["worker"]["state"], "paused_or_needs_human")
        self.assertFalse(self.engine.prepare_notification(1062)["allowed"])

    def test_same_source_cursor_cannot_regress_with_newer_sample_time(self):
        self.project["record"].update(taskState="in_progress", assignmentState="none")
        self.sample.update(paused=True, sourceInstanceId="stream", cursor="stream:10")
        self.observe(1000)
        self.sample.update(paused=False, cursor="stream:9", status="systemError",
                           turnStatus="failed", capacityFailure=True)
        result = self.observe(1001)
        self.assertEqual(result["targets"]["worker"]["state"], "paused_or_needs_human")
        self.assertFalse(self.engine.prepare_notification(1062)["allowed"])

    def test_prepare_checks_original_sample_time(self):
        self.project["record"]["nextReviewAt"] = 999
        self.sample["observedAt"] = 881
        self.engine.observe([self.sample], 1000, {"worker": self.project})
        self.assertEqual(self.engine.prepare_notification(1061)["reason"], "no_fresh_actionable_events")

    def test_invalid_future_or_wrong_host_sample_does_not_poison_watermark(self):
        self.sample["cursor"] = "host-1:10"
        self.observe(1000)
        invalid = {**self.sample, "observedAt": 9000, "cursor": "host-1:999"}
        self.engine.observe([invalid], 1001, {"worker": self.project})
        invalid.update(observedAt=1002, hostId="wrong-host")
        self.engine.observe([invalid], 1002, {"worker": self.project})
        self.sample["cursor"] = "host-1:11"
        result = self.observe(1003)
        self.assertEqual(result["targets"]["worker"]["state"], "standby_with_reason")

    def test_failed_read_and_restart_preserve_cursor_high_watermark(self):
        self.sample["cursor"] = "host-1:10"
        self.observe(1000)
        self.sample["readError"] = True
        self.observe(1001)
        self.engine = Supervisor(self.store, "run", self.contract)
        self.sample.update(readError=False, cursor="host-1:9", paused=True)
        self.assertEqual(self.observe(1002)["targets"]["worker"]["state"], "unknown")
        self.sample["cursor"] = "host-1:10"
        self.assertEqual(self.observe(1003)["targets"]["worker"]["state"], "paused_or_needs_human")

    def test_dependency_event_is_retired_by_new_standby_decision(self):
        self.observe()
        self.project["record"]["dependencyVersion"] = "unfrozen"
        eid = self.observe(1010)["pending"][0]
        self.project["record"].update(decisionRef="updated-decision", nextReviewAt=2000)
        self.assertEqual(self.observe(1071)["pending"], [])
        event = self.store.get_record("run", "supervision")["value"]["events"][eid]
        self.assertEqual(event["closedReason"], "decision_or_dependency_superseded")

    def test_dependency_event_due_can_change_after_verified_consumption(self):
        self.observe()
        self.project["record"]["dependencyVersion"] = "unfrozen"
        eid = self.observe(1010)["pending"][0]
        self.observe(1071)
        proposal = self.engine.prepare_notification(1071)
        self.engine.notification_result(proposal["notificationId"], "delivered", 1071)
        self.receipt(eid, "acknowledged")
        self.receipt(eid, "decision_recorded", decision="wait", reason="dependency", nextReviewAt=1600)
        self.receipt(eid, "action_verified", dependencyVersion="unfrozen")
        self.assertIsNone(self.observe(1100)["targets"]["worker"]["reviewReason"])
        self.assertEqual(self.observe(1601)["targets"]["worker"]["reviewReason"], "standby_review_due")

    def test_late_delivery_result_preserves_terminal_events(self):
        eid, proposal = self.event()
        self.project["record"]["taskBindingId"] = "superseding-task"
        self.observe(1062)
        self.engine.notification_result(proposal["notificationId"], "delivered", 1063)
        state = self.store.get_record("run", "supervision")["value"]
        self.assertEqual(state["events"][eid]["stage"], "closed")
        self.assertEqual(state["notifications"][proposal["notificationId"]]["status"], "delivered")
        self.assertEqual(self.engine.overdue(1364), [])

    def test_ack_replay_is_idempotent_across_restart_and_later_stages(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", 1061)
        receipt = {"runId": "run", "owner": "lead", "eventId": eid, "stage": "acknowledged",
                   "evidence": self.proof("stable-ack")}
        first = self.engine.owner_receipt(receipt, 1062)
        self.engine = Supervisor(self.store, "run", self.contract)
        replay = self.engine.owner_receipt(receipt, 1361)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(first["receiptId"], replay["receiptId"])
        self.assertEqual(self.store.get_record("run", "supervision")["value"]["events"][eid]["nextReviewAt"], 1362)
        self.assertEqual(len(self.engine.overdue(1363)), 1)
        self.engine.owner_receipt({"runId": "run", "owner": "lead", "eventId": eid,
            "stage": "decision_recorded", "evidence": self.proof("decision"),
            "decision": "wait", "reason": "dependency", "nextReviewAt": 1700}, 1364)
        self.assertTrue(self.engine.owner_receipt(receipt, 1365)["duplicate"])
        self.assertEqual(self.store.get_record("run", "supervision")["value"]["events"][eid]["stage"], "decision_recorded")

    def test_reused_receipt_proof_cannot_change_decision_payload(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", self.now)
        self.receipt(eid, "acknowledged")
        receipt = {"runId": "run", "owner": "lead", "eventId": eid, "stage": "decision_recorded",
                   "evidence": self.proof("stable-decision"), "decision": "wait",
                   "reason": "dependency", "nextReviewAt": 1600}
        self.engine.owner_receipt(receipt, self.now)
        receipt["nextReviewAt"] = 1700
        with self.assertRaises(ValueError):
            self.engine.owner_receipt(receipt, self.now + 1)
        self.assertEqual(self.store.get_record("run", "supervision")["value"]["events"][eid]["nextReviewAt"], 1600)

    def test_same_stage_receipt_update_requires_explicit_predecessor(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", 1061)
        first = self.receipt(eid, "acknowledged")
        receipt = {"runId": "run", "owner": "lead", "eventId": eid, "stage": "acknowledged",
                   "evidence": self.proof("ack-update", {"version": 2})}
        with self.assertRaises(ValueError):
            self.engine.owner_receipt(receipt, 1100)
        receipt["supersedesReceiptId"] = first["receiptId"]
        updated = self.engine.owner_receipt(receipt, 1100)
        self.assertFalse(updated["duplicate"])
        self.assertEqual(self.store.get_record("run", "supervision")["value"]["events"][eid]["nextReviewAt"], 1361)

    def test_first_failed_read_cannot_use_old_capacity_snapshot(self):
        self.project["record"].update(taskState="in_progress", assignmentState="none")
        self.sample.update(status="systemError", turnStatus="failed", capacityFailure=True)
        self.observe()
        self.sample["readError"] = True
        first = self.observe(1001)["targets"]["worker"]
        self.assertEqual(first["state"], "unknown")
        self.assertFalse(first["recoveryEligible"])
        self.assertEqual(first["displayState"], "stale")
        self.assertFalse(self.engine.prepare_notification(1061)["allowed"])
        second = self.observe(1061)["targets"]["worker"]
        self.assertEqual(second["displayState"], "unknown")

    def test_completed_without_assignment_and_late_capacity_are_quiet(self):
        self.project["record"].pop("assignmentState")
        self.sample.update(capacityFailure=True, turnStatus="failed")
        self.assertEqual(self.observe()["targets"]["worker"]["state"], "completed")
        self.assertFalse(self.engine.prepare_notification(1061)["allowed"])

    def test_superseded_binding_never_reopens_old_task(self):
        self.event()
        self.project["record"]["taskBindingId"] = "new-batch"
        result = self.observe(1100)
        self.assertEqual(result["targets"]["worker"]["state"], "superseded")
        self.assertEqual(result["pending"], [])

    def test_pause_approval_missing_or_wrong_host_cannot_dispatch(self):
        for change in ({"paused": True}, {"activeFlags": ["waitingOnApproval"]}, {"hostId": "other"}):
            original = copy.deepcopy(self.sample)
            self.sample.update(change)
            self.project["record"]["nextReviewAt"] = 900
            self.observe()
            self.assertFalse(self.engine.prepare_notification(1061)["allowed"])
            self.sample = original

    def test_notification_is_coalesced_once_and_unknown_stops_resend(self):
        self.project["record"]["nextReviewAt"] = 999
        self.observe()
        self.assertEqual(self.engine.prepare_notification(1010)["reason"], "coalescing")
        self.observe(1061)
        proposal = self.engine.prepare_notification(1061)
        self.engine.notification_result(proposal["notificationId"], "unknown", 1062)
        self.engine = Supervisor(SupervisionStore(self.root / "state.db", self.root / "registry"), "run", self.contract)
        self.assertEqual(self.engine.prepare_notification(1500)["reason"], "prior_notification_outcome_unknown")

    def test_ack_without_decision_escalates_only_once(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", self.now)
        self.receipt(eid, "acknowledged")
        self.assertEqual(self.engine.overdue(self.now + 299), [])
        alerts = self.engine.overdue(self.now + 301)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["requiresNonModelNotifier"])
        self.assertEqual(self.engine.overdue(self.now + 1000), [])

    def test_decision_without_action_is_not_success(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", self.now)
        self.receipt(eid, "acknowledged")
        self.receipt(eid, "decision_recorded", decision="dispatch", reason="separate files", nextReviewAt=self.now + 100)
        with self.assertRaises(ValueError):
            self.receipt(eid, "action_verified", newTaskBindingId="next")
        self.assertEqual(self.engine.overdue(self.now + 101)[0]["stage"], "decision_recorded")

    def test_actual_handoff_has_separate_decision_dispatch_progress_evidence(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", self.now)
        self.receipt(eid, "acknowledged")
        self.receipt(eid, "decision_recorded", decision="dispatch", reason="separate files", nextReviewAt=self.now + 300)
        result = self.receipt(eid, "action_verified", newTaskBindingId="next",
                              dispatchEvidence=self.proof("dispatch"), progressEvidence=self.proof("progress"))
        self.assertEqual(result["stage"], "action_verified")
        self.receipt(eid, "closed")
        self.assertEqual(self.engine.overdue(3000), [])

    def test_bad_evidence_or_skipping_ack_rejected(self):
        eid, proposal = self.event()
        self.engine.notification_result(proposal["notificationId"], "delivered", self.now)
        with self.assertRaises(ValueError):
            self.receipt(eid, "action_verified")
        with self.assertRaises(ValueError):
            self.engine.owner_receipt({"runId": "run", "owner": "lead", "eventId": eid,
                "stage": "acknowledged", "evidence": {"path": str(self.root / "missing"), "sha256": "x"}}, self.now)

    def test_selected_project_hash_ignores_unrelated_file_changes(self):
        path = self.root / "project.json"
        content = {"target": self.project["record"], "unrelated": 1}
        path.write_text(json.dumps(content), encoding="utf-8")
        mapping = {"path": str(path), "pointer": "/target"}
        first = project_record(mapping)
        content["unrelated"] = 2
        path.write_text(json.dumps(content), encoding="utf-8")
        second = project_record(mapping)
        self.assertEqual(first["version"], second["version"])
        self.assertNotEqual(first["source"]["sha256"], second["source"]["sha256"])
        content["target"] = "unstructured prose is not a valid task decision"
        path.write_text(json.dumps(content), encoding="utf-8")
        self.assertFalse(project_record(mapping)["known"])

    def test_public_sample_does_not_copy_messages_or_tools(self):
        value = public_sample({"thread": {"id": "worker", "status": {"type": "idle"}},
            "cursor": "runtime:3", "latestTurn": {"id": "t", "status": "completed"},
            "latestAssistantMessage": {"text": "secret body"}, "latestToolMarker": {"command": "private command"}}, 1000)
        self.assertNotIn("secret", json.dumps(value))
        self.assertNotIn("private", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
