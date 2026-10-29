"""Durable, fail-closed admission for a single authorized supervision run.

This owns only its SQLite files, never a Codex database.  Callers supply a stable,
trusted registry directory and validate authorization, current snapshots, native
retry evidence and external side effects.  Admission is not an exactly-once
guarantee for an external API: only the first committed intent receives a permit.

Times are Unix seconds or timezone-aware ISO-8601 strings. Recovery policy keys
match plan-v4; notification limits must both be explicit to enable notifications.
All reservations, including cancelled and unknown ones, permanently consume quota.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid


class StoreError(RuntimeError):
    """The authority cannot safely be used."""


class StoreIntegrityError(StoreError):
    """Missing, conflicting, or incomplete persistent authority."""


class RevisionConflict(StoreError):
    """A CAS write lost to another writer; read the record again."""


_RECOVERY_DEFAULTS = {
    "maxAdditionalTurnsPerIncident": 3,
    "maxAdditionalTurnsPerMonitoringRun": 6,
    "maxFailureAttemptAllowance": 120,
    "maxFailureAttemptAllowancePerMonitoringRun": 120,
    "incidentDeadlineSeconds": 900,
}
_NOTIFICATION_KEYS = (
    "maxModelDispatchesPerMonitoringRun",
    "minModelDispatchIntervalPerRecipientSeconds",
)
_PENDING = {"reserved", "sending", "sent", "accepted", "awaiting_verification", "unknown"}
_TERMINAL = {
    "failed", "cancelled", "not_sent", "rejected", "recovered",
    "completed", "processed", "succeeded",
}
_END_INCIDENT = {"recovered", "completed", "processed", "succeeded"}
_MAX_JSON_BYTES = 65536


def _json(value):
    result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("JSON record exceeds 65536 bytes")
    return result


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value):
    if isinstance(value, bool):
        raise ValueError("time must be Unix seconds or timezone-aware ISO-8601")
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("ISO time requires a timezone")
        value = parsed.timestamp()
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time must be finite")
    return float(value)


def _id(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(field + " must be a nonempty string of at most 1024 characters")
    return value


def _integer(value, field, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(field + " must be an integer >= " + str(minimum))
    return value


def _path(value):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(value))))


def _policy(contract):
    policy = contract.get("policy", {})
    if not isinstance(policy, dict):
        raise ValueError("contract.policy must be an object")
    result = dict(_RECOVERY_DEFAULTS)
    result.update(policy)
    for key in _RECOVERY_DEFAULTS:
        _integer(result[key], key, 1 if key == "incidentDeadlineSeconds" else 0)
    for key in _NOTIFICATION_KEYS:
        if key in result:
            _integer(result[key], key)
    return result


def _validate_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError("contract must be an object")
    start, deadline = _time(contract["start"]), _time(contract["deadline"])
    if deadline <= start:
        raise ValueError("contract deadline must follow start")
    owner = contract["owner"]
    _id(owner.get("threadId") if isinstance(owner, dict) else owner, "owner")
    _integer(contract.get("ownerGeneration", 1), "ownerGeneration", 1)
    targets = contract["targets"]
    if not isinstance(targets, list):
        raise ValueError("targets must be a list")
    seen = set()
    for target in targets:
        _id(target["threadId"], "threadId")
        _id(target["taskBindingId"], "taskBindingId")
        if target["kind"] not in {"independent", "child"}:
            raise ValueError("target kind must be independent or child")
        if target["threadId"] in seen:
            raise ValueError("duplicate threadId in contract")
        seen.add(target["threadId"])
    recipients = contract.get("notificationRecipients", [])
    if not isinstance(recipients, list):
        raise ValueError("notificationRecipients must be a list")
    for recipient in recipients:
        _id(recipient, "notification recipient")
    _policy(contract)
    _json(contract)


_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL,
        contract_json TEXT NOT NULL, start REAL NOT NULL, deadline REAL NOT NULL,
        status TEXT NOT NULL, stop_reason TEXT, recovery_turns INTEGER NOT NULL DEFAULT 0,
        failure_attempts INTEGER NOT NULL DEFAULT 0, notifications INTEGER NOT NULL DEFAULT 0,
        last_now REAL NOT NULL)""",
    """CREATE TABLE incidents (
        run_id TEXT NOT NULL, incident_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        binding_id TEXT NOT NULL, root_turn TEXT NOT NULL, deadline REAL NOT NULL,
        requested_deadline REAL NOT NULL, initial_bound INTEGER NOT NULL,
        initial_evidence_json TEXT NOT NULL, recovery_turns INTEGER NOT NULL DEFAULT 0,
        failure_attempts INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        PRIMARY KEY(run_id, incident_id), UNIQUE(run_id, thread_id, root_turn))""",
    """CREATE TABLE intents (
        intent_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, incident_id TEXT, recipient TEXT,
        status TEXT NOT NULL, created REAL NOT NULL, sequence INTEGER NOT NULL,
        payload_json TEXT NOT NULL, evidence_json TEXT,
        UNIQUE(run_id, kind, idempotency_key))""",
    "CREATE INDEX intents_by_incident ON intents(run_id, incident_id, kind)",
    "CREATE INDEX intents_by_recipient ON intents(run_id, recipient, kind, created)",
    """CREATE TABLE records (
        run_id TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
        value_json TEXT NOT NULL, PRIMARY KEY(run_id, key))""",
)


class SupervisionStore:
    """Explicit initialization plus a canonical run-to-database registry.

    Reusing a run with a different contract/path or a lost database raises
    StoreIntegrityError. A failed initialization remains blocked for inspection;
    it is never silently replaced. Keep registry_dir fixed across all entrypoints.
    """

    def __init__(self, db_path, registry_dir):
        self.db_path = _path(db_path)
        self.registry_dir = _path(registry_dir)
        self.registry_path = str(Path(self.registry_dir) / "supervision-registry.sqlite3")
        if _path(self.registry_path) == self.db_path:
            raise ValueError("authority and registry databases must be separate")

    @staticmethod
    def _connect(path, create=False):
        if not create and not Path(path).is_file():
            raise StoreIntegrityError("authority database is missing")
        connection = None
        try:
            uri = Path(path).as_uri() + ("?mode=rwc" if create else "?mode=rw")
            connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise StoreIntegrityError("cannot open persistent authority") from exc

    def _registry(self, create=False):
        if create:
            Path(self.registry_dir).mkdir(parents=True, exist_ok=True)
        connection = self._connect(self.registry_path, create)
        try:
            if create:
                connection.execute("""CREATE TABLE IF NOT EXISTS bindings (
                    run_id TEXT PRIMARY KEY, db_path TEXT NOT NULL,
                    contract_hash TEXT NOT NULL, store_id TEXT NOT NULL,
                    state TEXT NOT NULL)""")
            return connection
        except sqlite3.Error as exc:
            connection.close()
            raise StoreIntegrityError("invalid canonical registry") from exc

    @staticmethod
    def _metadata(connection):
        metadata = dict(connection.execute("SELECT key,value FROM metadata").fetchall())
        if metadata.get("schema_version") != "1" or not metadata.get("store_id"):
            raise StoreIntegrityError("invalid authority metadata")
        return metadata

    def create_run(self, run_id, contract, now):
        """Initialize explicitly; identical retries preserve every counter and stop."""
        _id(run_id, "runId")
        _validate_contract(contract)
        now = _time(now)
        contract_hash = _hash(contract)
        if not Path(self.registry_path).exists() and Path(self.db_path).exists():
            raise StoreIntegrityError("registry missing for existing authority; refusing rebind")
        registry = self._registry(create=True)
        try:
            registry.execute("BEGIN IMMEDIATE")
            binding = registry.execute("SELECT * FROM bindings WHERE run_id=?", (run_id,)).fetchone()
            if binding:
                if binding["db_path"] != self.db_path or binding["contract_hash"] != contract_hash:
                    raise StoreIntegrityError("run path or contract differs from canonical binding")
                if binding["state"] != "ready":
                    raise StoreIntegrityError("run initialization incomplete; inspection required")
                registry.commit()
                return self.run_status(run_id)
            prior = registry.execute("SELECT * FROM bindings WHERE db_path=?", (self.db_path,)).fetchall()
            exists = Path(self.db_path).exists()
            if prior and (not exists or any(row["state"] != "ready" for row in prior)):
                raise StoreIntegrityError("registered authority missing or incomplete")
            if exists:
                if not prior:
                    raise StoreIntegrityError("unregistered existing authority; refusing adoption")
                connection = self._connect(self.db_path)
                try:
                    store_id = self._metadata(connection)["store_id"]
                    if any(row["store_id"] != store_id for row in prior):
                        raise StoreIntegrityError("authority identity differs from registry")
                finally:
                    connection.close()
            else:
                store_id = str(uuid.uuid4())
            registry.execute("INSERT INTO bindings VALUES (?,?,?,?, 'initializing')",
                             (run_id, self.db_path, contract_hash, store_id))
            registry.commit()
            # A crash after this point leaves an explicit incomplete binding.
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect(self.db_path, create=not exists)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not exists:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.executemany("INSERT INTO metadata VALUES (?,?)",
                                           [("schema_version", "1"), ("store_id", store_id)])
                elif self._metadata(connection)["store_id"] != store_id:
                    raise StoreIntegrityError("authority identity changed during initialization")
                connection.execute("""INSERT INTO runs
                    (run_id,contract_hash,contract_json,start,deadline,status,last_now)
                    VALUES (?,?,?,?,?,'active',?)""",
                                   (run_id, contract_hash, _json(contract), _time(contract["start"]),
                                    _time(contract["deadline"]), now))
                connection.commit()
            finally:
                connection.close()
            registry.execute("BEGIN IMMEDIATE")
            registry.execute("UPDATE bindings SET state='ready' WHERE run_id=?", (run_id,))
            registry.commit()
        except sqlite3.Error as exc:
            raise StoreIntegrityError("persistent initialization failed; no automatic reset") from exc
        finally:
            registry.close()
        return self.run_status(run_id)

    @contextmanager
    def _transaction(self, run_id):
        _id(run_id, "runId")
        registry = self._registry()
        connection = None
        try:
            binding = registry.execute("SELECT * FROM bindings WHERE run_id=?", (run_id,)).fetchone()
            if not binding or binding["state"] != "ready" or binding["db_path"] != self.db_path:
                raise StoreIntegrityError("run has no ready canonical binding at this path")
            connection = self._connect(self.db_path)
            connection.execute("BEGIN IMMEDIATE")
            if self._metadata(connection)["store_id"] != binding["store_id"]:
                raise StoreIntegrityError("authority identity differs from registry")
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise StoreIntegrityError("registered run is missing")
            contract = json.loads(run["contract_json"])
            if run["contract_hash"] != binding["contract_hash"] or _hash(contract) != binding["contract_hash"]:
                raise StoreIntegrityError("persisted contract differs from canonical binding")
            yield connection, run, contract
            connection.commit()
        except sqlite3.Error as exc:
            raise StoreIntegrityError("persistent authority transaction failed") from exc
        finally:
            if connection is not None:
                connection.close()
            registry.close()

    def run_status(self, run_id):
        with self._transaction(run_id) as (connection, run, contract):
            incidents = connection.execute("""SELECT incident_id,thread_id,binding_id,root_turn,
                deadline,recovery_turns,failure_attempts,status FROM incidents WHERE run_id=?
                ORDER BY incident_id""", (run_id,)).fetchall()
            intents = connection.execute("""SELECT intent_id,kind,idempotency_key,incident_id,
                recipient,status,created,sequence FROM intents WHERE run_id=? ORDER BY created,intent_id""",
                                         (run_id,)).fetchall()
            return {"runId": run_id, "contract": contract, "contractHash": run["contract_hash"],
                    "status": run["status"], "stopReason": run["stop_reason"],
                    "usedRecoveryTurns": run["recovery_turns"],
                    "usedFailureAttempts": run["failure_attempts"],
                    "usedNotifications": run["notifications"],
                    "lastNow": run["last_now"], "incidents": [dict(row) for row in incidents],
                    "intents": [dict(row) for row in intents]}

    def stop_run(self, run_id, reason):
        _id(reason, "stop reason")
        with self._transaction(run_id) as (connection, run, _):
            if run["status"] == "active":
                connection.execute("UPDATE runs SET status='stopped',stop_reason=? WHERE run_id=?",
                                   (reason, run_id))
        return self.run_status(run_id)

    @staticmethod
    def _guard(connection, run, now):
        if run["status"] != "active":
            return "run_stopped"
        if now < run["last_now"]:
            return "clock_regression"
        connection.execute("UPDATE runs SET last_now=? WHERE run_id=?", (now, run["run_id"]))
        if now < run["start"]:
            return "run_not_started"
        if now >= run["deadline"]:
            return "run_deadline"
        return None

    @staticmethod
    def _duplicate(connection, run_id, kind, key):
        row = connection.execute("""SELECT intent_id,status,sequence FROM intents
            WHERE run_id=? AND kind=? AND idempotency_key=?""", (run_id, kind, key)).fetchone()
        if row:
            return {"allowed": False, "reason": "duplicate_intent", "intentId": row["intent_id"],
                    "status": row["status"], "sequence": row["sequence"]}
        return None

    def reserve_recovery(self, run_id, request, now):
        """Charge the original chain once and atomically reserve one extra attempt."""
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        for key in ("incidentId", "attemptId", "threadId", "taskBindingId", "rootTurn", "snapshotFingerprint"):
            _id(request[key], key)
        executor = request["executor"]
        if not isinstance(executor, (str, dict)) or not executor:
            raise ValueError("executor must be a nonempty string or object")
        bound = _integer(request["nativeFailureAttemptUpperBound"], "nativeFailureAttemptUpperBound", 1)
        evidence = request["nativeRetryEvidence"]
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("nativeRetryEvidence must be a nonempty object")
        now, requested_deadline = _time(now), _time(request["incidentDeadline"])
        _integer(request.get("ownerGeneration", 1), "ownerGeneration", 1)
        payload = _json(request)
        with self._transaction(run_id) as (connection, run, contract):
            duplicate = self._duplicate(connection, run_id, "recovery", request["attemptId"])
            if duplicate:
                return duplicate
            reason = self._guard(connection, run, now)
            if reason:
                return {"allowed": False, "reason": reason}
            if request.get("ownerGeneration", 1) != contract.get("ownerGeneration", 1):
                return {"allowed": False, "reason": "owner_generation_mismatch"}
            target = next((target for target in contract["targets"]
                           if target["threadId"] == request["threadId"]), None)
            if not target or target["taskBindingId"] != request["taskBindingId"]:
                return {"allowed": False, "reason": "target_binding_mismatch"}
            if "kind" in request and request["kind"] != target["kind"]:
                return {"allowed": False, "reason": "target_kind_mismatch"}
            if "willRetry" in evidence and evidence["willRetry"] is not False:
                return {"allowed": False, "reason": "native_retry_pending"}
            if any(key in evidence and now < _time(evidence[key]) for key in ("retryAfter", "notBefore")):
                return {"allowed": False, "reason": "native_retry_wait"}
            if "snapshotExpiresAt" in request and now >= _time(request["snapshotExpiresAt"]):
                return {"allowed": False, "reason": "snapshot_expired"}
            # Changing an incident/root identifier must not bypass an unresolved
            # send on the same target. External effects still require inspection.
            target_pending = connection.execute("""SELECT i.status FROM intents i
                JOIN incidents c ON c.run_id=i.run_id AND c.incident_id=i.incident_id
                WHERE i.run_id=? AND i.kind='recovery' AND c.thread_id=?""",
                                                (run_id, request["threadId"])).fetchall()
            if any(row["status"] in _PENDING for row in target_pending):
                return {"allowed": False, "reason": "inflight_intent"}
            policy = _policy(contract)
            incident = connection.execute("SELECT * FROM incidents WHERE run_id=? AND incident_id=?",
                                          (run_id, request["incidentId"])).fetchone()
            if incident:
                if (incident["thread_id"], incident["binding_id"], incident["root_turn"], incident["requested_deadline"]) != (
                        request["threadId"], request["taskBindingId"], request["rootTurn"], requested_deadline):
                    return {"allowed": False, "reason": "incident_identity_mismatch"}
                if incident["status"] != "active":
                    return {"allowed": False, "reason": "incident_closed"}
                if now >= incident["deadline"]:
                    return {"allowed": False, "reason": "incident_deadline"}
            else:
                deadline = min(requested_deadline, run["deadline"], now + policy["incidentDeadlineSeconds"])
                if now >= deadline:
                    return {"allowed": False, "reason": "incident_deadline"}
                root = connection.execute("""SELECT incident_id FROM incidents
                    WHERE run_id=? AND thread_id=? AND root_turn=?""",
                                          (run_id, request["threadId"], request["rootTurn"])).fetchone()
                if root:
                    return {"allowed": False, "reason": "root_turn_already_registered",
                            "incidentId": root["incident_id"]}
                connection.execute("""INSERT INTO incidents
                    (run_id,incident_id,thread_id,binding_id,root_turn,deadline,requested_deadline,
                    initial_bound,initial_evidence_json,failure_attempts) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                   (run_id, request["incidentId"], request["threadId"], request["taskBindingId"],
                                    request["rootTurn"], deadline, requested_deadline, bound, _json(evidence), bound))
                connection.execute("UPDATE runs SET failure_attempts=failure_attempts+? WHERE run_id=?", (bound, run_id))
                incident = connection.execute("SELECT * FROM incidents WHERE run_id=? AND incident_id=?",
                                              (run_id, request["incidentId"])).fetchone()
                run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            checks = (
                (incident["recovery_turns"] + 1, policy["maxAdditionalTurnsPerIncident"], "incident_turn_budget"),
                (run["recovery_turns"] + 1, policy["maxAdditionalTurnsPerMonitoringRun"], "run_turn_budget"),
                (incident["failure_attempts"] + bound, policy["maxFailureAttemptAllowance"], "incident_failure_budget"),
                (run["failure_attempts"] + bound, policy["maxFailureAttemptAllowancePerMonitoringRun"], "run_failure_budget"),
            )
            for used, limit, reason in checks:
                if used > limit:
                    return {"allowed": False, "reason": reason}
            intent_id = str(uuid.uuid4())
            sequence = incident["recovery_turns"] + 1
            connection.execute("""INSERT INTO intents
                (intent_id,run_id,kind,idempotency_key,incident_id,status,created,sequence,payload_json)
                VALUES (?,?,'recovery',?,?,'reserved',?,?,?)""",
                               (intent_id, run_id, request["attemptId"], request["incidentId"], now, sequence, payload))
            connection.execute("""UPDATE incidents SET recovery_turns=recovery_turns+1,
                failure_attempts=failure_attempts+? WHERE run_id=? AND incident_id=?""",
                               (bound, run_id, request["incidentId"]))
            connection.execute("""UPDATE runs SET recovery_turns=recovery_turns+1,
                failure_attempts=failure_attempts+? WHERE run_id=?""", (bound, run_id))
            return {"allowed": True, "reason": "reserved", "intentId": intent_id,
                    "attemptSequence": sequence, "incidentDeadline": incident["deadline"],
                    "usedRecoveryTurns": run["recovery_turns"] + 1,
                    "usedFailureAttempts": run["failure_attempts"] + bound}

    def reserve_notification(self, run_id, notification_id, recipient, event_ids, now):
        _id(notification_id, "notificationId")
        _id(recipient, "recipient")
        if not isinstance(event_ids, list) or not event_ids:
            raise ValueError("event_ids must be a nonempty list")
        for event_id in event_ids:
            _id(event_id, "eventId")
        now = _time(now)
        payload = _json({"notificationId": notification_id, "recipient": recipient, "eventIds": event_ids})
        with self._transaction(run_id) as (connection, run, contract):
            duplicate = self._duplicate(connection, run_id, "notification", notification_id)
            if duplicate:
                return duplicate
            reason = self._guard(connection, run, now)
            if reason:
                return {"allowed": False, "reason": reason}
            owner = contract["owner"]
            owner_id = owner.get("threadId") if isinstance(owner, dict) else owner
            if recipient not in [owner_id, *contract.get("notificationRecipients", [])]:
                return {"allowed": False, "reason": "notification_recipient_mismatch"}
            policy = _policy(contract)
            if not all(key in policy for key in _NOTIFICATION_KEYS):
                return {"allowed": False, "reason": "notifications_disabled"}
            if run["notifications"] >= policy[_NOTIFICATION_KEYS[0]]:
                return {"allowed": False, "reason": "notification_budget"}
            prior_notices = connection.execute("""SELECT intent_id,status,created,payload_json FROM intents
                WHERE run_id=? AND kind='notification' AND recipient=?""", (run_id, recipient)).fetchall()
            unresolved = {"reserved", "sending", "sent", "unknown", "awaiting_verification"}
            for notice in prior_notices:
                if notice["status"] in unresolved:
                    return {"allowed": False, "reason": "notification_outcome_unresolved",
                            "intentId": notice["intent_id"]}
            requested_events = set(event_ids)
            for notice in prior_notices:
                # Only a definite nondelivery result permits a charged retry.
                # Cancellation/failed processing do not establish nondelivery.
                if notice["status"] in {"not_sent", "rejected"}:
                    continue
                previous_events = json.loads(notice["payload_json"])["eventIds"]
                if requested_events.intersection(previous_events):
                    return {"allowed": False, "reason": "notification_event_already_reserved",
                            "intentId": notice["intent_id"]}
            previous = max((notice["created"] for notice in prior_notices), default=None)
            if previous is not None:
                not_before = previous + policy[_NOTIFICATION_KEYS[1]]
                if now < not_before:
                    return {"allowed": False, "reason": "notification_interval", "notBefore": not_before}
            intent_id, sequence = str(uuid.uuid4()), run["notifications"] + 1
            connection.execute("""INSERT INTO intents
                (intent_id,run_id,kind,idempotency_key,recipient,status,created,sequence,payload_json)
                VALUES (?,?,'notification',?,?,'reserved',?,?,?)""",
                               (intent_id, run_id, notification_id, recipient, now, sequence, payload))
            connection.execute("UPDATE runs SET notifications=notifications+1 WHERE run_id=?", (run_id,))
            return {"allowed": True, "reason": "reserved", "intentId": intent_id, "sequence": sequence,
                    "usedNotifications": sequence, "notBefore": now + policy[_NOTIFICATION_KEYS[1]]}

    def complete_intent(self, run_id, intent_id, kind, status, evidence=None):
        """Record a caller-verified outcome without returning a dispatch permit.

        Evidence is audit metadata, not a delivery verifier. Before resolving an
        unknown notification to not_sent/rejected, the caller must independently
        inspect the actual host result/target history and establish nondelivery;
        merely supplying a nonempty object does not establish that fact. Unknown
        remains charged and blocked until an explicit, verified outcome is saved.
        """
        _id(intent_id, "intentId")
        if kind not in {"recovery", "notification"} or status not in _PENDING | _TERMINAL:
            raise ValueError("unknown intent kind or status")
        evidence_json = None if evidence is None else _json(evidence)
        with self._transaction(run_id) as (connection, _, __):
            row = connection.execute("SELECT * FROM intents WHERE run_id=? AND intent_id=? AND kind=?",
                                     (run_id, intent_id, kind)).fetchone()
            if row is None:
                raise StoreIntegrityError("intent does not belong to this run and kind")
            if row["status"] == status:
                return {"intentId": intent_id, "kind": kind, "status": status, "changed": False}
            if row["status"] in _TERMINAL:
                raise ValueError("terminal intent cannot be reopened or rewritten")
            if status == "reserved":
                raise ValueError("intent cannot return to reserved")
            if row["status"] == "unknown" and not evidence:
                raise ValueError("resolving unknown intent requires recorded evidence")
            connection.execute("UPDATE intents SET status=?,evidence_json=? WHERE intent_id=?",
                               (status, evidence_json, intent_id))
            if kind == "recovery" and status in _END_INCIDENT:
                connection.execute("UPDATE incidents SET status='closed' WHERE run_id=? AND incident_id=?",
                                   (run_id, row["incident_id"]))
            return {"intentId": intent_id, "kind": kind, "status": status, "changed": True}

    def get_record(self, run_id, key):
        _id(key, "record key")
        with self._transaction(run_id) as (connection, _, __):
            row = connection.execute("SELECT revision,value_json FROM records WHERE run_id=? AND key=?", (run_id, key)).fetchone()
            return {"revision": row["revision"], "value": json.loads(row["value_json"])} if row else {"revision": 0, "value": None}

    def put_record(self, run_id, key, value, expected_revision):
        _id(key, "record key")
        _integer(expected_revision, "expected_revision")
        payload = _json(value)
        with self._transaction(run_id) as (connection, _, __):
            row = connection.execute("SELECT revision FROM records WHERE run_id=? AND key=?", (run_id, key)).fetchone()
            actual = row["revision"] if row else 0
            if actual != expected_revision:
                raise RevisionConflict("record revision changed")
            revision = actual + 1
            connection.execute("""INSERT INTO records VALUES (?,?,?,?)
                ON CONFLICT(run_id,key) DO UPDATE SET revision=excluded.revision,value_json=excluded.value_json""",
                               (run_id, key, revision, payload))
            return revision
