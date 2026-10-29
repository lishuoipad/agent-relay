"""Shared, fail-closed recovery permits. This module never sends a message.

Both native independent recovery and a real parent-tool integration reserve through
the same SupervisionStore. A child permit is a short-lived, single-consumer intent,
not evidence that the host can send or switch a child's model (C4).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def denied(reason):
    return {"allowed": False, "reason": reason}


def timestamp(value):
    return float(value) if type(value) in (int, float) else datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def safety_digest(snapshot):
    return digest({key: value for key, value in snapshot.items() if key not in ("observedAt", "fingerprint")})


class RecoveryAdmission:
    """Authorization is frozen in contract.targets[].recoveryAuthorization.

    Target authorization includes executor, model, effort, modelLocked,
    fallbackModels, batchId, handoffSha256, and capability. Capability requires
    authorizationRef, preflightEvidence, state, and trialMaxDispatches (default 1).
    verified_for_scope additionally needs resultEvidence. A preflight receipt is
    not a result receipt; the first authorized trial needs no historical success.
    """

    MAX_SNAPSHOT_AGE = 15

    def __init__(self, store, run_id, contract=None):
        self.store, self.run_id = store, run_id
        current = store.run_status(run_id)
        if contract is not None and current["contract"] != contract:
            raise ValueError("existing supervision contract mismatch")
        self.contract = current["contract"]

    @classmethod
    def from_config(cls, config):
        from supervision_store import SupervisionStore
        if config.get("version") != 2:
            raise ValueError("execution requires a v2 shared supervision contract")
        spec = config["supervision"]
        for name in ("dbPath", "registryDir", "contractPath"):
            if not Path(spec[name]).is_absolute():
                raise ValueError("supervision paths must be absolute")
        raw = Path(spec["contractPath"]).read_bytes()
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != spec["contractSha256"]:
            raise ValueError("supervision contract hash mismatch")
        # Opening an authority does not authorize creating or resetting its run.
        admission = cls(SupervisionStore(spec["dbPath"], spec["registryDir"]),
                        spec["runId"], json.loads(raw))
        for target in config["threads"]:
            admission.validate_target(target, "independent")
        return admission

    def validate_target(self, target, kind):
        matches = [row for row in self.contract["targets"]
                   if row["threadId"] == target["threadId"] and
                   row["taskBindingId"] == target.get("taskBindingId") and row["kind"] == kind]
        if len(matches) != 1:
            raise ValueError("target task binding not in supervision contract")
        auth = matches[0].get("recoveryAuthorization")
        if not isinstance(auth, dict):
            raise ValueError("contract has no recovery authorization for target")
        for key in ("executor", "model", "effort", "modelLocked", "fallbackModels", "batchId",
                    "handoffSha256", "capability", "nativeFailureAttemptUpperBound",
                    "nativeRetryEvidence"):
            if key not in auth or auth[key] != target.get(key):
                raise ValueError("recovery authorization differs from contract: " + key)
        if auth.get("nativeRetryEvidenceByModel", {}) != target.get("nativeRetryEvidenceByModel", {}):
            raise ValueError("native retry routes differ from contract")
        for key in ("handoffPath", "reconcileCommand", "recoveryContextCommand", "parentThreadId", "parentTaskBindingId"):
            if auth.get(key) != target.get(key):
                raise ValueError("recovery evidence route differs from contract: " + key)
        if kind == "child" and (not target.get("parentThreadId") or not target.get("parentTaskBindingId")):
            raise ValueError("child authorization must bind its real parent and parent task")
        if target.get("authorized") is not True or target.get("exclusiveRecoveryOwner") is not True:
            raise ValueError("explicit authorization and dispatch ownership required")
        return matches[0]

    def bind_local_state(self, state):
        """Never silently migrate old budgets/intents or reset by changing directories."""
        record = self.store.get_record(self.run_id, "recovery.local-state")
        identity = str(state.path.resolve())
        expected = {"runId": self.run_id, "path": identity}
        if record["value"] is not None:
            if record["value"] != expected or not state.path.exists():
                raise ValueError("canonical recovery state changed or missing")
            if state.data.get("supervision") != expected:
                raise ValueError("recovery state is not bound to existing supervision run")
            return
        if state.data.get("threads") or state.data.get("signals") or state.data.get("supervision"):
            raise ValueError("legacy recovery state requires explicit budget and intent reconciliation")
        # A crash between this claim and the local save deliberately requires review.
        self.store.put_record(self.run_id, "recovery.local-state", expected, record["revision"])
        state.data["supervision"] = expected
        state.save()

    def _fresh(self, snapshot, now):
        stamp = snapshot.get("observedAt")
        return (snapshot.get("readSucceeded") is True and type(stamp) in (int, float)
                and 0 <= now-stamp <= self.MAX_SNAPSHOT_AGE
                and bool(snapshot.get("runtimeInstanceId"))
                and bool(snapshot.get("fingerprint")))

    def _safety(self, target, safety, now, kind):
        if not self._fresh(safety, now):
            return "required_snapshot_unknown_or_stale"
        if safety.get("threadId") != target["threadId"] or safety.get("taskBindingId") != target["taskBindingId"]:
            return "snapshot_binding_mismatch"
        for name in ("latestCapacityFailure", "nativeRetryEnded", "taskBindingValid", "unfinishedWork",
                     "ownershipValid", "sideEffectsReconciled", "handoffValid", "settingsVerified"):
            if safety.get(name) is not True:
                return "safety_precondition_missing:" + name
        for name in ("queued", "paused", "approvalPending", "budgetStopped"):
            if safety.get(name) is not False:
                return "unsafe_or_unknown:" + name
        if safety.get("runningOperations") != []:
            return "unsafe_or_unknown:runningOperations"
        if safety.get("taskState") not in ("assigned", "running", "blocked_capacity", "in_progress"):
            return "task_terminal_or_unknown"
        if safety.get("goalStatus") not in ("active", "no_goal"):
            return "goal_not_active"
        if type(safety.get("notBefore")) not in (int, float) or now < safety["notBefore"]:
            return "native_retry_delay"
        if not safety.get("failedTurn") or not safety.get("rootTurn"):
            return "failure_identity_missing"
        if kind == "independent" and safety.get("parentThreadId"):
            return "not_independent"
        return None

    @staticmethod
    def _native_evidence(target, safety, model, now):
        evidence = target.get("nativeRetryEvidenceByModel", {}).get(model)
        if evidence is None and model == target.get("model"):
            evidence = target.get("nativeRetryEvidence")
        if not isinstance(evidence, dict) or not evidence:
            return None
        required = ("runtimeVersion", "provider", "route", "model", "upperBound", "evidenceSha256", "validUntil")
        if any(key not in evidence for key in required):
            return None
        if (evidence["model"] != model or type(evidence["upperBound"]) is not int
                or not 1 <= evidence["upperBound"] <= 1000
                or type(evidence["validUntil"]) not in (int, float) or now >= evidence["validUntil"]
                or not isinstance(evidence["evidenceSha256"], str)
                or len(evidence["evidenceSha256"]) != 64
                or any(c not in "0123456789abcdef" for c in evidence["evidenceSha256"])):
            return None
        runtime = safety.get("nativeRuntime")
        if not isinstance(runtime, dict) or any(not evidence[k] or evidence[k] != runtime.get(k)
                                               for k in ("runtimeVersion", "provider", "route")):
            return None
        return evidence

    def reserve(self, target, safety, request, now):
        """Independent-thread entry. Children must use child_permit."""
        return self._reserve(target, safety, request, now, "independent")

    def _reserve(self, target, safety, request, now, kind):
        """Shared independent/child admission. Returned permit must be claimed once."""
        self.validate_target(target, kind)
        failure = self._safety(target, safety, now, kind)
        if failure:
            return denied(failure)
        model = request.get("selectedModel")
        choices = [target["model"]] + ([] if target["modelLocked"] else target.get("fallbackModels", []))
        if model not in choices or request.get("effort") != target["effort"]:
            return denied("model_or_effort_not_authorized")
        if safety.get("observedEffort") != target["effort"] or safety.get("selectedModelSupported") is not True:
            return denied("runtime_settings_unverified")
        status = self.store.run_status(self.run_id)
        if status["status"] != "active" or now < status["lastNow"] or now >= timestamp(self.contract["deadline"]):
            return denied("run_stopped_expired_or_clock_reversed")
        previous = [row for row in status["intents"] if row["kind"] == "recovery"
                    and row["incident_id"] == request["incidentId"]]
        previous.sort(key=lambda row: row["sequence"])
        sequence = previous[-1]["sequence"]+1 if previous else 1
        if request.get("attemptSequence") != sequence:
            return denied("attempt_sequence_mismatch")
        if not previous and model != target["model"]:
            return denied("first_attempt_requires_original_model")
        observed_model = target["model"]
        if previous:
            prior = self.store.get_record(self.run_id, "recovery.permit."+previous[-1]["intent_id"])["value"]
            if (previous[-1]["status"] != "failed" or not prior
                    or prior.get("acceptedTurn") != safety["failedTurn"]):
                return denied("previous_attempt_not_verified_capacity_failure")
            observed_model = prior["selectedModel"]
        if safety.get("observedModel") != observed_model:
            return denied("runtime_model_changed")
        evidence = self._native_evidence(target, safety, model, now)
        if evidence is None:
            return denied("native_retry_evidence_unknown_stale_or_route_mismatch")
        if target.get("nativeFailureAttemptUpperBound") != target["nativeRetryEvidence"].get("upperBound"):
            return denied("initial_native_bound_mismatch")
        # This binding cannot manufacture another incident after a timeout or an
        # unverified retry. New legitimate work needs its separately bound task.
        binding_key = "recovery.binding."+digest([target["threadId"], target["taskBindingId"]])
        binding = self.store.get_record(self.run_id, binding_key)
        identity = {"incidentId": request["incidentId"], "rootTurn": request["rootTurn"]}
        if binding["value"] is not None and binding["value"] != identity:
            return denied("task_binding_has_unresolved_incident")
        if binding["value"] is None:
            if any(row["thread_id"] == target["threadId"] and row["binding_id"] == target["taskBindingId"]
                   for row in status["incidents"]):
                return denied("existing_recovery_state_requires_reconciliation")
            try:
                self.store.put_record(self.run_id, binding_key, identity, binding["revision"])
            except Exception:
                return denied("incident_binding_conflict_or_unavailable")
        cap = target["capability"]
        if not cap.get("authorizationRef") or not cap.get("preflightEvidence"):
            return denied("capability_preflight_or_authorization_missing")
        state = cap.get("state")
        if state == "verified_for_scope":
            if not isinstance(cap.get("resultEvidence"), dict) or not cap["resultEvidence"]:
                return denied("verified_scope_has_no_result_evidence")
        elif state == "trial_authorized":
            maximum = cap.get("trialMaxDispatches", 1)
            if type(maximum) is not int or not 1 <= maximum <= 3:
                return denied("invalid_trial_limit")
            # One claim per authorized path across all its targets; unknown/no-send
            # results consume the trial too. Failed claims cannot increase a limit.
            key = "recovery.trial." + kind
            trial = self.store.get_record(self.run_id, key)
            data = trial["value"] or {"used": 0, "limit": maximum}
            if data["limit"] != maximum or data["used"] >= maximum:
                return denied("trial_limit_exhausted_or_changed")
            data = dict(data, used=data["used"]+1)
            try:
                self.store.put_record(self.run_id, key, data, trial["revision"])
            except Exception:
                return denied("trial_claim_conflict_or_unavailable")
        else:
            return denied("capability_disabled")
        if (request.get("rootTurn") != safety["rootTurn"] or
                request.get("failedTurn") != safety["failedTurn"]):
            return denied("failure_binding_mismatch")
        full = dict(request, threadId=target["threadId"], taskBindingId=target["taskBindingId"],
                    executor=target["executor"], snapshotFingerprint=safety["fingerprint"],
                    snapshotExpiresAt=safety["observedAt"]+self.MAX_SNAPSHOT_AGE, kind=kind,
                    nativeFailureAttemptUpperBound=evidence["upperBound"],
                    nativeRetryEvidence=dict(evidence, willRetry=False, notBefore=safety["notBefore"]))
        permit = self.store.reserve_recovery(self.run_id, full, now)
        if permit.get("allowed"):
            entry = {"executor": target["executor"], "threadId": target["threadId"],
                     "taskBindingId": target["taskBindingId"], "fingerprint": safety["fingerprint"],
                     "safetyDigest": safety_digest(safety),
                     "runtimeInstanceId": safety["runtimeInstanceId"],
                     "expiresAt": min(safety["observedAt"]+self.MAX_SNAPSHOT_AGE,
                                      timestamp(permit["incidentDeadline"]), timestamp(self.contract["deadline"])),
                     "selectedModel": model, "effort": target["effort"], "kind": kind,
                     "claimed": False}
            self.store.put_record(self.run_id, "recovery.permit."+permit["intentId"], entry, 0)
            permit.update(expiresAt=entry["expiresAt"], c4="disabled_pending_real_host_trial" if kind == "child" else None)
            permit["reservedNativeFailureAttemptUpperBound"] = evidence["upperBound"]
        return permit

    def claim_for_send(self, intent_id, target, safety, now):
        row = self.store.get_record(self.run_id, "recovery.permit."+intent_id)
        if not row["value"] or row["value"].get("kind") != "independent":
            return denied("child_requires_fresh_parent_claim")
        return self._claim_for_send(intent_id, target, safety, now)

    def _claim_for_send(self, intent_id, target, safety, now):
        """Persist a one-time claim immediately before the real host call.

        A crash after claim is ambiguous and can never reissue this permit.
        The host remains responsible for its final fresh snapshot and direct call.
        """
        row = self.store.get_record(self.run_id, "recovery.permit."+intent_id)
        value = row["value"]
        if not value or value["claimed"]:
            return denied("permit_missing_or_already_claimed")
        self.validate_target(target, value["kind"])
        status = self.store.run_status(self.run_id)
        if status["status"] != "active" or now < status["lastNow"] or now >= timestamp(status["contract"]["deadline"]):
            return self._cancel_permit(intent_id, row, "run_not_active")
        failure = self._safety(target, safety, now, value["kind"])
        if (failure or now >= value["expiresAt"] or value["executor"] != target["executor"]
                or value["threadId"] != target["threadId"] or value["taskBindingId"] != target["taskBindingId"]
                or value["fingerprint"] != safety["fingerprint"]
                or value["safetyDigest"] != safety_digest(safety)
                or value["runtimeInstanceId"] != safety["runtimeInstanceId"]):
            return self._cancel_permit(intent_id, row, failure or "permit_expired_or_changed")
        try:
            self.store.put_record(self.run_id, "recovery.permit."+intent_id,
                                  dict(value, claimed=True), row["revision"])
        except Exception:
            return denied("permit_claim_conflict_or_unavailable")
        self.store.complete_intent(self.run_id, intent_id, "recovery", "sending")
        return {"allowed": True, "intentId": intent_id}

    def _cancel_permit(self, intent_id, row, reason):
        if not row["value"] or row["value"].get("claimed"):
            return denied("permit_missing_or_already_claimed")
        try:
            self.store.put_record(self.run_id, "recovery.permit."+intent_id,
                                  dict(row["value"], claimed=True, cancellation=reason), row["revision"])
        except Exception:
            return denied("permit_claim_conflict_or_unavailable")
        self.store.complete_intent(self.run_id, intent_id, "recovery", "not_sent", {"reason": reason})
        return denied(reason)

    def record_outcome(self, intent_id, status, evidence=None):
        # Completed model turns/accepted messages are never recovery verification.
        if status in ("completed", "recovered", "verified"):
            raise ValueError("use independent task progress verification; a turn is not recovery")
        if status not in ("accepted", "unknown", "failed", "cancelled", "not_sent", "awaiting_verification"):
            raise ValueError("unsupported recovery outcome")
        row = self.store.get_record(self.run_id, "recovery.permit."+intent_id)
        value = row["value"]
        if not value:
            raise ValueError("unknown recovery permit")
        if status in ("accepted", "unknown", "failed", "awaiting_verification") and value.get("claimed") is not True:
            raise ValueError("host result requires claimed permit")
        if status == "accepted":
            if not isinstance(evidence, dict) or not evidence.get("turnId"):
                raise ValueError("accepted result requires a new turn identity")
            self.store.put_record(self.run_id, "recovery.permit."+intent_id,
                                  dict(value, acceptedTurn=evidence["turnId"]), row["revision"])
        if status == "failed" and (not evidence or evidence.get("capacityFailure") is not True
                                    or evidence.get("turnId") != value.get("acceptedTurn")):
            raise ValueError("next attempt requires evidence of the accepted turn's capacity failure")
        return self.store.complete_intent(self.run_id, intent_id, "recovery", status, evidence)

    def child_permit(self, target, child, parent, request, now):
        """Limited permit API only: cannot call collaboration tools or switch models."""
        if not self._fresh(parent, now):
            return denied("parent_snapshot_unknown_or_stale")
        if (parent.get("threadId") != child.get("parentThreadId") or
                parent.get("threadId") != target.get("parentThreadId") or
                parent.get("taskBindingId") != target.get("parentTaskBindingId") or parent.get("taskBindingValid") is not True or
                parent.get("executor") != target.get("executor") or
                parent.get("safeBoundary") is not True or parent.get("authorizedToolAvailable") is not True or
                parent.get("queued") is not False or parent.get("paused") is not False or
                parent.get("approvalPending") is not False or parent.get("budgetStopped") is not False or
                parent.get("runningOperations") != [] or
                parent.get("goalStatus") not in ("active", "no_goal")):
            return denied("parent_preflight_incomplete")
        if request.get("selectedModel") != target.get("model") or child.get("observedModel") != target.get("model"):
            return denied("child_model_switch_disabled")
        result = self._reserve(target, child, request, now, "child")
        if result.get("allowed"):
            row = self.store.get_record(self.run_id, "recovery.permit."+result["intentId"])
            self.store.put_record(self.run_id, "recovery.permit."+result["intentId"],
                                  dict(row["value"], parentFingerprint=parent["fingerprint"],
                                       parentSafetyDigest=safety_digest(parent),
                                       parentRuntimeInstanceId=parent["runtimeInstanceId"]), row["revision"])
        return result

    def claim_child_for_send(self, intent_id, target, child, parent, now):
        row = self.store.get_record(self.run_id, "recovery.permit."+intent_id)
        value = row["value"] or {}
        if (not self._fresh(parent, now) or value.get("kind") != "child"
                or value.get("parentFingerprint") != parent.get("fingerprint")
                or value.get("parentSafetyDigest") != safety_digest(parent)
                or value.get("parentRuntimeInstanceId") != parent.get("runtimeInstanceId")
                or parent.get("safeBoundary") is not True or parent.get("runningOperations") != []):
            return self._cancel_permit(intent_id, row, "parent_changed_before_send")
        if (parent.get("threadId") != child.get("parentThreadId") or
                parent.get("threadId") != target.get("parentThreadId") or
                parent.get("taskBindingId") != target.get("parentTaskBindingId") or parent.get("taskBindingValid") is not True or
                parent.get("executor") != target.get("executor") or parent.get("authorizedToolAvailable") is not True or
                parent.get("queued") is not False or parent.get("paused") is not False or
                parent.get("approvalPending") is not False or parent.get("budgetStopped") is not False or
                parent.get("goalStatus") not in ("active", "no_goal")):
            return self._cancel_permit(intent_id, row, "parent_changed_before_send")
        return self._claim_for_send(intent_id, target, child, now)
