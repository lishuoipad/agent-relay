"""Behavior tests for the owned authority only; never contact a model or host."""

import copy
from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "parallel-dev-coordination" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from supervision_store import RevisionConflict, StoreIntegrityError, SupervisionStore


def contract(**policy):
    return {
        "start": 900, "deadline": 10000, "owner": {"threadId": "owner"},
        "ownerGeneration": 1,
        "notificationRecipients": ["reviewer"],
        "targets": [
            {"threadId": "independent-thread", "taskBindingId": "independent-binding", "kind": "independent"},
            {"threadId": "child-thread", "taskBindingId": "child-binding", "kind": "child"},
        ],
        "policy": {"maxModelDispatchesPerMonitoringRun": 12,
                   "minModelDispatchIntervalPerRecipientSeconds": 300, **policy},
    }


def request(attempt="attempt-1", incident="incident-1", kind="independent", bound=10, **extra):
    return {
        "incidentId": incident, "attemptId": attempt,
        "threadId": kind + "-thread", "taskBindingId": kind + "-binding", "kind": kind,
        "rootTurn": "root-" + incident, "nativeFailureAttemptUpperBound": bound,
        "nativeRetryEvidence": {"evidenceHash": "verified-test-bound", "willRetry": False},
        "executor": {"id": kind + "-executor"}, "snapshotFingerprint": "test-snapshot",
        "incidentDeadline": 1800, **extra,
    }


def _reserve_worker(db, registry, payload, ready, start, results):
    try:
        store = SupervisionStore(db, registry)
        ready.put("ready")
        if not start.wait(20):
            results.put({"error": "start_timeout"})
            return
        results.put(store.reserve_recovery("run", payload, 1000))
    except Exception as error:
        results.put({"error": type(error).__name__})


def _cas_worker(db, registry, ready, start, results):
    try:
        store = SupervisionStore(db, registry)
        ready.put("ready")
        if not start.wait(20):
            results.put({"error": "start_timeout"})
            return
        try:
            revision = store.put_record("run", "shared", {"claimed": True}, 0)
            results.put({"revision": revision})
        except RevisionConflict:
            results.put({"conflict": True})
    except Exception as error:
        results.put({"error": type(error).__name__})


def _notification_worker(db, registry, payload, ready, start, results):
    try:
        store = SupervisionStore(db, registry)
        ready.put("ready")
        if not start.wait(20):
            results.put({"error": "start_timeout"})
            return
        results.put(store.reserve_notification("run", payload["notificationId"],
                                               "owner", payload["eventIds"], 1000))
    except Exception as error:
        results.put({"error": type(error).__name__})


class SupervisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supervision-store-")
        self.root = Path(self.temp.name)
        self.db = self.root / "authority.sqlite3"
        self.registry = self.root / "registry"
        self.store = SupervisionStore(self.db, self.registry)
        self.contract = contract()
        self.store.create_run("run", self.contract, 1000)

    def tearDown(self):
        self.temp.cleanup()

    def new_run(self, **policy):
        self.contract = contract(**policy)
        self.store = SupervisionStore(self.root / "custom.sqlite3", self.registry)
        self.store.create_run("custom", self.contract, 1000)
        return "custom"

    def finish_failed(self, result, run_id="run"):
        self.assertTrue(result["allowed"], result)
        self.store.complete_intent(run_id, result["intentId"], "recovery", "failed", {"observed": True})

    def concurrent(self, worker, payloads):
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = []
        try:
            for payload in payloads:
                arguments = (str(self.db), str(self.registry))
                if payload is not None:
                    arguments += (payload,)
                process = context.Process(target=worker, args=arguments + (ready, start, results))
                process.start()
                processes.append(process)
            for _ in processes:
                self.assertEqual("ready", ready.get(timeout=20))
            start.set()
            output = [results.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(20)
                self.assertEqual(0, process.exitcode)
            self.assertFalse(any("error" in result for result in output), output)
            return output
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            results.close()

    def test_independent_and_child_processes_compete_for_last_run_turn(self):
        for kind, count in (("independent", 3), ("child", 2)):
            for index in range(count):
                self.finish_failed(self.store.reserve_recovery(
                    "run", request(kind + str(index), kind, kind, bound=1), 1000))
        self.assertEqual(5, self.store.run_status("run")["usedRecoveryTurns"])
        results = self.concurrent(_reserve_worker, [
            request("independent-last", "independent-new", "independent", bound=1),
            request("child-last", "child", "child", bound=1),
        ])
        self.assertEqual(1, sum(result["allowed"] for result in results), results)
        self.assertEqual(["run_turn_budget"], [result["reason"] for result in results if not result["allowed"]])
        status = self.store.run_status("run")
        self.assertEqual(6, status["usedRecoveryTurns"])
        self.assertEqual(9, status["usedFailureAttempts"])

    def test_concurrent_duplicate_attempt_only_returns_one_permit(self):
        results = self.concurrent(_reserve_worker, [request(), request()])
        self.assertEqual(1, sum(result["allowed"] for result in results), results)
        self.assertEqual(1, len({result["intentId"] for result in results}))
        self.assertEqual(20, self.store.run_status("run")["usedFailureAttempts"])

    def test_failure_budget_exhausts_with_turns_remaining(self):
        first = self.store.reserve_recovery("run", request(bound=50), 1000)
        self.finish_failed(first)
        second = self.store.reserve_recovery("run", request("attempt-2", bound=50), 1001)
        self.assertEqual("incident_failure_budget", second["reason"])
        self.assertEqual(1, self.store.run_status("run")["usedRecoveryTurns"])
        self.assertEqual(100, self.store.run_status("run")["usedFailureAttempts"])

    def test_run_failure_budget_is_shared_across_targets(self):
        self.finish_failed(self.store.reserve_recovery("run", request(bound=40), 1000))
        denied = self.store.reserve_recovery("run", request("child-1", "child-incident", "child", bound=30), 1001)
        self.assertEqual("run_failure_budget", denied["reason"])
        self.assertEqual(1, self.store.run_status("run")["usedRecoveryTurns"])
        # The child's original failure already happened and remains charged.
        self.assertEqual(110, self.store.run_status("run")["usedFailureAttempts"])

    def test_original_chain_is_counted_once_even_when_initially_denied(self):
        payload = request(bound=121)
        self.assertEqual("incident_failure_budget", self.store.reserve_recovery("run", payload, 1000)["reason"])
        payload["attemptId"] = "another-attempt"
        self.assertFalse(self.store.reserve_recovery("run", payload, 1001)["allowed"])
        payload["incidentId"] = "invented-incident"
        self.assertEqual("root_turn_already_registered", self.store.reserve_recovery("run", payload, 1002)["reason"])
        status = self.store.run_status("run")
        self.assertEqual(121, status["usedFailureAttempts"])
        self.assertEqual(0, status["usedRecoveryTurns"])
        self.assertEqual(1, len(status["incidents"]))

    def test_unknown_restart_and_cancellation_do_not_refund(self):
        admitted = self.store.reserve_recovery("run", request(), 1000)
        self.store.complete_intent("run", admitted["intentId"], "recovery", "unknown")
        self.store = SupervisionStore(self.db, self.registry)
        self.store.create_run("run", copy.deepcopy(self.contract), 1001)
        self.assertEqual("duplicate_intent", self.store.reserve_recovery("run", request(), 1001)["reason"])
        self.assertEqual("inflight_intent", self.store.reserve_recovery("run", request("attempt-2"), 1001)["reason"])
        self.assertEqual("inflight_intent", self.store.reserve_recovery("run", request("attempt-2", "invented-incident"), 1001)["reason"])
        with self.assertRaises(ValueError):
            self.store.complete_intent("run", admitted["intentId"], "recovery", "not_sent")
        self.store.complete_intent("run", admitted["intentId"], "recovery", "not_sent", {"verifiedNoSend": True})
        second = self.store.reserve_recovery("run", request("attempt-2"), 1001)
        self.assertTrue(second["allowed"])
        self.store.complete_intent("run", second["intentId"], "recovery", "cancelled")
        status = self.store.run_status("run")
        self.assertEqual(2, status["usedRecoveryTurns"])
        self.assertEqual(30, status["usedFailureAttempts"])
        self.assertFalse(self.store.reserve_recovery("run", request("attempt-2"), 1002)["allowed"])

    def test_incident_turn_limit_is_independent_of_native_budget(self):
        for index in range(3):
            self.finish_failed(self.store.reserve_recovery("run", request("attempt-" + str(index), bound=1), 1000))
        denied = self.store.reserve_recovery("run", request("attempt-4", bound=1), 1000)
        self.assertEqual("incident_turn_budget", denied["reason"])
        self.assertEqual(3, self.store.run_status("run")["usedRecoveryTurns"])
        self.assertEqual(4, self.store.run_status("run")["usedFailureAttempts"])

    def test_inflight_and_awaiting_verification_prevent_next_attempt(self):
        first = self.store.reserve_recovery("run", request(), 1000)
        for state in ("sending", "accepted", "awaiting_verification"):
            self.store.complete_intent("run", first["intentId"], "recovery", state)
            self.assertEqual("inflight_intent", self.store.reserve_recovery("run", request("attempt-2"), 1000)["reason"])
        self.store.complete_intent("run", first["intentId"], "recovery", "recovered", {"progress": True})
        self.assertEqual("incident_closed", self.store.reserve_recovery("run", request("attempt-2"), 1000)["reason"])

    def test_directory_or_contract_changes_do_not_create_fresh_authority(self):
        self.finish_failed(self.store.reserve_recovery("run", request(), 1000))
        moved = SupervisionStore(self.root / "elsewhere" / "state.sqlite3", self.registry)
        with self.assertRaises(StoreIntegrityError):
            moved.create_run("run", self.contract, 1001)
        with self.assertRaises(StoreIntegrityError):
            moved.reserve_recovery("run", request("attempt-2"), 1001)
        changed = copy.deepcopy(self.contract)
        changed["policy"]["maxAdditionalTurnsPerMonitoringRun"] = 100
        with self.assertRaises(StoreIntegrityError):
            self.store.create_run("run", changed, 1001)
        self.assertEqual(1, self.store.run_status("run")["usedRecoveryTurns"])
        self.assertFalse(Path(moved.db_path).exists())

    def test_missing_database_and_missing_registry_never_auto_initialize(self):
        self.finish_failed(self.store.reserve_recovery("run", request(), 1000))
        backup = self.root / "saved-authority.sqlite3"
        self.db.rename(backup)
        for operation in (lambda: self.store.create_run("run", self.contract, 1001),
                          lambda: self.store.reserve_recovery("run", request("attempt-2"), 1001),
                          lambda: self.store.run_status("run")):
            with self.assertRaises(StoreIntegrityError):
                operation()
        self.assertFalse(self.db.exists())
        backup.rename(self.db)
        Path(self.store.registry_path).unlink()
        with self.assertRaises(StoreIntegrityError):
            self.store.create_run("run", self.contract, 1001)
        self.assertFalse(Path(self.store.registry_path).exists())

    def test_authority_replacement_is_detected(self):
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("UPDATE metadata SET value='replacement' WHERE key='store_id'")
        with self.assertRaises(StoreIntegrityError):
            self.store.reserve_recovery("run", request(), 1000)

    def test_unregistered_and_incomplete_authorities_are_not_adopted(self):
        with closing(sqlite3.connect(self.store.registry_path)) as connection:
            with connection:
                connection.execute("UPDATE bindings SET state='initializing' WHERE run_id='run'")
        with self.assertRaises(StoreIntegrityError):
            self.store.create_run("run", self.contract, 1000)
        with self.assertRaises(StoreIntegrityError):
            self.store.run_status("run")
        orphan = self.root / "orphan.sqlite3"
        orphan.touch()
        with self.assertRaises(StoreIntegrityError):
            SupervisionStore(orphan, self.registry).create_run("other", self.contract, 1000)

    def test_run_deadline_pause_and_target_binding_are_guards(self):
        self.assertEqual("target_binding_mismatch", self.store.reserve_recovery("run", request(taskBindingId="old-binding"), 1000)["reason"])
        self.assertEqual("target_kind_mismatch", self.store.reserve_recovery("run", request(kind="independent", **{"threadId": "child-thread", "taskBindingId": "child-binding"}), 1000)["reason"])
        self.assertEqual("owner_generation_mismatch", self.store.reserve_recovery("run", request(ownerGeneration=2), 1000)["reason"])
        self.assertEqual(0, self.store.run_status("run")["usedFailureAttempts"])
        stopped = self.store.stop_run("run", "user paused")
        self.assertEqual("stopped", stopped["status"])
        self.store.create_run("run", self.contract, 1001)
        self.assertEqual("run_stopped", self.store.reserve_recovery("run", request(), 1001)["reason"])
        self.assertEqual("run_stopped", self.store.reserve_notification("run", "n", "owner", ["e"], 1001)["reason"])
        other = self.new_run()
        self.assertEqual("run_deadline", self.store.reserve_recovery(other, request(), 10000)["reason"])
        self.assertEqual("run_deadline", self.store.reserve_notification(other, "n", "owner", ["e"], 10000)["reason"])

    def test_incident_identity_and_deadline_are_frozen(self):
        self.finish_failed(self.store.reserve_recovery("run", request(incidentDeadline=1010), 1000))
        self.assertEqual("incident_identity_mismatch", self.store.reserve_recovery("run", request("attempt-2", incidentDeadline=1100), 1001)["reason"])
        self.assertEqual("incident_deadline", self.store.reserve_recovery("run", request("attempt-2", incidentDeadline=1010), 1010)["reason"])
        self.assertEqual(20, self.store.run_status("run")["usedFailureAttempts"])

    def test_native_retry_snapshot_and_clock_guards(self):
        for extra, reason in (
            ({"nativeRetryEvidence": {"willRetry": True}}, "native_retry_pending"),
            ({"nativeRetryEvidence": {"notBefore": 1001}}, "native_retry_wait"),
            ({"snapshotExpiresAt": 1000}, "snapshot_expired"),
        ):
            self.assertEqual(reason, self.store.reserve_recovery("run", request(**extra), 1000)["reason"])
        self.assertEqual("clock_regression", self.store.reserve_recovery("run", request(), 999)["reason"])
        self.assertEqual(0, self.store.run_status("run")["usedFailureAttempts"])

    def test_notification_quota_dedup_interval_recipient_and_restart(self):
        admitted = self.store.reserve_notification("run", "n1", "owner", ["event-1"], 1000)
        self.assertTrue(admitted["allowed"])
        self.store.complete_intent("run", admitted["intentId"], "notification", "unknown")
        self.store = SupervisionStore(self.db, self.registry)
        self.assertEqual("duplicate_intent", self.store.reserve_notification("run", "n1", "reviewer", ["changed-event"], 1001)["reason"])
        self.store.complete_intent("run", admitted["intentId"], "notification", "accepted",
                                   {"testFixtureOnly": "host acceptance independently confirmed"})
        self.assertEqual("notification_interval", self.store.reserve_notification("run", "n2", "owner", ["event-2"], 1299)["reason"])
        self.assertEqual("notification_recipient_mismatch", self.store.reserve_notification("run", "n2", "arbitrary", ["event-2"], 1300)["reason"])
        for index in range(2, 13):
            admitted = self.store.reserve_notification("run", "n" + str(index), "owner", ["e" + str(index)], 1000 + (index - 1) * 300)
            self.assertTrue(admitted["allowed"])
            self.store.complete_intent("run", admitted["intentId"], "notification", "accepted")
        self.assertEqual("notification_budget", self.store.reserve_notification("run", "n13", "reviewer", ["event-13"], 4600)["reason"])
        self.assertEqual(12, self.store.run_status("run")["usedNotifications"])

    def test_notification_cancelled_is_charged_and_recipients_have_separate_intervals(self):
        run_id = self.new_run(maxModelDispatchesPerMonitoringRun=2)
        first = self.store.reserve_notification(run_id, "n1", "owner", ["e1"], 1000)
        self.store.complete_intent(run_id, first["intentId"], "notification", "cancelled")
        second = self.store.reserve_notification(run_id, "n2", "reviewer", ["e2"], 1000)
        self.assertTrue(second["allowed"])
        self.assertEqual("notification_budget", self.store.reserve_notification(run_id, "n3", "owner", ["e3"], 1300)["reason"])

    def test_unknown_notification_blocks_new_ids_and_events_after_restart(self):
        first = self.store.reserve_notification("run", "n1", "owner", ["e1"], 1000)
        self.store.complete_intent("run", first["intentId"], "notification", "unknown")
        self.store = SupervisionStore(self.db, self.registry)
        for notification_id, event_ids in (("n2", ["e1"]), ("n3", ["e2"])):
            denied = self.store.reserve_notification("run", notification_id, "owner", event_ids, 1300)
            self.assertEqual("notification_outcome_unresolved", denied["reason"])
        self.assertEqual(1, self.store.run_status("run")["usedNotifications"])

    def test_sent_event_cannot_be_relabelled_but_new_events_can_be_notified(self):
        first = self.store.reserve_notification("run", "n1", "owner", ["e1", "e2"], 1000)
        self.store.complete_intent("run", first["intentId"], "notification", "accepted")
        denied = self.store.reserve_notification("run", "n2", "owner", ["e2", "new-event"], 1300)
        self.assertEqual("notification_event_already_reserved", denied["reason"])
        fresh = self.store.reserve_notification("run", "n3", "owner", ["new-event"], 1300)
        self.assertTrue(fresh["allowed"])
        # Another explicitly authorized recipient has its own event delivery.
        self.assertTrue(self.store.reserve_notification("run", "n4", "reviewer", ["e2"], 1300)["allowed"])
        self.assertEqual(3, self.store.run_status("run")["usedNotifications"])

    def test_verified_not_sent_can_retry_with_a_new_id_without_refund(self):
        first = self.store.reserve_notification("run", "n1", "owner", ["e1"], 1000)
        self.store.complete_intent("run", first["intentId"], "notification", "unknown")
        with self.assertRaises(ValueError):
            self.store.complete_intent("run", first["intentId"], "notification", "not_sent")
        self.store.complete_intent("run", first["intentId"], "notification", "not_sent",
                                   {"testFixtureOnly": "independent host check confirmed no delivery"})
        retry = self.store.reserve_notification("run", "n2", "owner", ["e1"], 1300)
        self.assertTrue(retry["allowed"])
        self.assertEqual(2, self.store.run_status("run")["usedNotifications"])

    def test_cancelled_does_not_prove_event_was_not_sent(self):
        first = self.store.reserve_notification("run", "n1", "owner", ["e1"], 1000)
        self.store.complete_intent("run", first["intentId"], "notification", "cancelled")
        denied = self.store.reserve_notification("run", "n2", "owner", ["e1"], 1300)
        self.assertEqual("notification_event_already_reserved", denied["reason"])

    def test_concurrent_notification_ids_share_recipient_inflight_guard(self):
        results = self.concurrent(_notification_worker, [
            {"notificationId": "n1", "eventIds": ["e1"]},
            {"notificationId": "n2", "eventIds": ["e2"]},
        ])
        self.assertEqual(1, sum(result["allowed"] for result in results))
        self.assertEqual(["notification_outcome_unresolved"],
                         [result["reason"] for result in results if not result["allowed"]])

    def test_notification_disabled_without_both_explicit_limits(self):
        disabled_contract = contract()
        del disabled_contract["policy"]["minModelDispatchIntervalPerRecipientSeconds"]
        other = SupervisionStore(self.root / "disabled.sqlite3", self.registry)
        other.create_run("disabled", disabled_contract, 1000)
        self.assertEqual("notifications_disabled", other.reserve_notification("disabled", "n", "owner", ["e"], 1000)["reason"])

    def test_records_use_cas_and_survive_restart_and_stop(self):
        self.assertEqual({"revision": 0, "value": None}, self.store.get_record("run", "state"))
        self.assertEqual(1, self.store.put_record("run", "state", {"events": ["one"]}, 0))
        with self.assertRaises(RevisionConflict):
            self.store.put_record("run", "state", {"events": []}, 0)
        self.assertEqual(2, self.store.put_record("run", "state", {"events": ["one", "two"]}, 1))
        self.store.stop_run("run", "paused")
        reopened = SupervisionStore(self.db, self.registry)
        self.assertEqual({"revision": 2, "value": {"events": ["one", "two"]}}, reopened.get_record("run", "state"))
        self.assertEqual(3, reopened.put_record("run", "state", {"stopped": True}, 2))
        with self.assertRaises(ValueError):
            reopened.put_record("run", "large", "x" * 65536, 0)

    def test_concurrent_cas_has_one_winner(self):
        results = self.concurrent(_cas_worker, [None, None])
        self.assertEqual(1, sum("revision" in result for result in results))
        self.assertEqual(1, sum("conflict" in result for result in results))
        self.assertEqual(1, self.store.get_record("run", "shared")["revision"])

    def test_no_implicit_initialization_or_invalid_inputs(self):
        fresh = SupervisionStore(self.root / "uninitialized.sqlite3", self.root / "no-registry")
        with self.assertRaises(StoreIntegrityError):
            fresh.reserve_recovery("run", request(), 1000)
        self.assertFalse(Path(fresh.db_path).exists())
        for extra in ({"nativeFailureAttemptUpperBound": 0}, {"nativeFailureAttemptUpperBound": True},
                      {"nativeRetryEvidence": {}}, {"snapshotFingerprint": ""}, {"executor": ""}):
            with self.assertRaises(ValueError):
                self.store.reserve_recovery("run", request(**extra), 1000)
        self.assertEqual(0, self.store.run_status("run")["usedRecoveryTurns"])

    def test_iso_times_and_contract_extensions_are_preserved(self):
        custom = contract()
        custom["start"] = "1970-01-01T00:15:00Z"
        custom["deadline"] = "1970-01-01T02:46:40+00:00"
        custom["targets"][0]["recoveryAuthorization"] = {"verified": True}
        other = SupervisionStore(self.root / "iso.sqlite3", self.registry)
        other.create_run("iso", custom, "1970-01-01T00:16:40Z")
        self.assertEqual(custom, other.run_status("iso")["contract"])
        self.assertTrue(other.reserve_recovery("iso", request(), "1970-01-01T00:16:40Z")["allowed"])


if __name__ == "__main__":
    unittest.main()
