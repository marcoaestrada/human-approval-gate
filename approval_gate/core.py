"""The approval gate: an append-only, hash-chained log of proposals and decisions.

An agent (or any script) *proposes* an action. Nothing runs until a person
approves it. Every step is one line in a JSON Lines file. Each line carries the
hash of the previous line, so editing an old line is detected by `verify()`.

Statuses of a proposal:
  pending    waiting for a decision
  approved   a person said yes; not run yet
  rejected   a person said no
  expired    nobody decided before `expires_at`
  executing  a runner started it and has not reported back (never retried automatically)
  executed   the handler finished
  failed     the handler raised an error (never retried automatically)
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

GENESIS = "0" * 64
RISKS = ("low", "medium", "high")
MAX_TTL_SECONDS = 30 * 24 * 3600


class GateError(Exception):
    """Base class for every error this module raises on purpose."""


class NotFound(GateError):
    pass


class InvalidTransition(GateError):
    pass


class IntegrityError(GateError):
    pass


# ---------------------------------------------------------------- small helpers
def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _FileLock:
    """A small cross-platform lock: the lock file exists while the lock is held."""

    def __init__(self, path: str, timeout: float = 10.0, stale_after: float = 30.0):
        self.path, self.timeout, self.stale_after = path, timeout, stale_after
        self._fd: Optional[int] = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:  # a crashed process may have left the lock behind
                    if time.time() - os.path.getmtime(self.path) > self.stale_after:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise GateError(f"could not get the lock on {self.path} (another process is writing)")
                time.sleep(0.01)

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- the log
class EventLog:
    def __init__(self, path: str, lock_timeout: float = 10.0):
        self.path = path
        self.lock_path = path + ".lock"
        self.lock_timeout = lock_timeout
        self._thread_lock = threading.Lock()

    def read(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        events: List[dict] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    raise IntegrityError(f"line {number} is not valid JSON")
        return events

    def append(self, ts: str, type_: str, id_: str, data: dict,
               guard: Optional[Callable[[List[dict]], None]] = None) -> dict:
        """Append one event. `guard(events)` runs inside the lock and may raise to abort."""
        with self._thread_lock, _FileLock(self.lock_path, self.lock_timeout):
            events = self.read()
            if guard is not None:
                guard(events)
            event = {
                "v": 1, "seq": len(events) + 1, "ts": ts, "type": type_, "id": id_,
                "data": data, "prev": events[-1]["hash"] if events else GENESIS,
            }
            event["hash"] = _sha(_canon(event))
            with open(self.path, "ab") as f:
                f.write(_canon(event).encode("utf-8") + b"\n")
                f.flush()
                os.fsync(f.fileno())
            return event

    def verify(self) -> int:
        """Return the number of events if the chain is intact; raise IntegrityError otherwise."""
        previous = GENESIS
        events = self.read()
        for position, event in enumerate(events, 1):
            body = {k: v for k, v in event.items() if k != "hash"}
            if event.get("seq") != position:
                raise IntegrityError(f"event {position}: wrong sequence number")
            if event.get("prev") != previous:
                raise IntegrityError(f"event {position}: the chain is broken (an earlier line was changed, removed or reordered)")
            if event.get("hash") != _sha(_canon(body)):
                raise IntegrityError(f"event {position}: the content does not match its hash (the line was edited)")
            previous = event["hash"]
        return len(events)


# ---------------------------------------------------------------- state
@dataclass
class Proposal:
    id: str
    action: str
    payload: dict
    summary: str
    proposed_by: str
    risk: str
    created_at: str
    expires_at: str
    payload_sha256: str
    idempotency_key: Optional[str] = None
    status: str = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    reason: str = ""
    approved_sha256: Optional[str] = None
    result: Any = None
    error: str = ""


def fold(events: Iterable[dict], now: datetime) -> Dict[str, Proposal]:
    proposals: Dict[str, Proposal] = {}
    for event in events:
        kind, id_, data = event["type"], event["id"], event["data"]
        if kind == "proposed":
            proposals[id_] = Proposal(
                id=id_, action=data["action"], payload=data["payload"], summary=data["summary"],
                proposed_by=data["proposed_by"], risk=data["risk"], created_at=event["ts"],
                expires_at=data["expires_at"], payload_sha256=data["payload_sha256"],
                idempotency_key=data.get("idempotency_key"),
            )
            continue
        p = proposals.get(id_)
        if p is None:
            continue
        if kind in ("approved", "rejected"):
            p.status, p.decided_by, p.decided_at, p.reason = kind, data["by"], event["ts"], data.get("reason", "")
            if kind == "approved":
                p.approved_sha256 = data.get("payload_sha256")
        elif kind == "expired":
            p.status = "expired"
        elif kind == "started":
            p.status = "executing"
        elif kind == "executed":
            p.status, p.result = "executed", data.get("result")
        elif kind == "failed":
            p.status, p.error = "failed", data.get("error", "")
        elif kind == "resolved":
            p.status, p.reason = data["outcome"], data.get("note", "")
    for p in proposals.values():
        if p.status == "pending" and now >= _parse(p.expires_at):
            p.status = "expired"
    return proposals


# ---------------------------------------------------------------- the gate
class ApprovalGate:
    def __init__(self, path: str, clock: Optional[Callable[[], datetime]] = None,
                 require_different_person: bool = True, auto_approve: Iterable[str] = ()):
        auto = tuple(auto_approve)
        if any(level != "low" for level in auto):
            raise GateError("only the 'low' risk level can ever be auto-approved")
        self.log = EventLog(path)
        self._clock = clock or _utcnow
        self.require_different_person = require_different_person
        self.auto_approve = auto

    # -- reading
    def now(self) -> datetime:
        return self._clock()

    def _state(self, events: Optional[List[dict]] = None) -> Dict[str, Proposal]:
        return fold(self.log.read() if events is None else events, self.now())

    def get(self, id_or_prefix: str) -> Proposal:
        state = self._state()
        if id_or_prefix in state:
            return state[id_or_prefix]
        matches = [p for key, p in state.items() if len(id_or_prefix) >= 4 and key.startswith(id_or_prefix)]
        if not matches:
            raise NotFound(f"no proposal with id {id_or_prefix!r}")
        if len(matches) > 1:
            raise GateError(f"{id_or_prefix!r} matches more than one proposal; type more characters")
        return matches[0]

    def list(self, status: Optional[str] = None) -> List[Proposal]:
        items = sorted(self._state().values(), key=lambda p: (p.created_at, p.id))
        return [p for p in items if status is None or p.status == status]

    def pending(self) -> List[Proposal]:
        return self.list("pending")

    def verify(self) -> int:
        return self.log.verify()

    # -- proposing
    def propose(self, action: str, payload: dict, summary: str, proposed_by: str = "agent",
                risk: str = "medium", ttl_seconds: int = 24 * 3600,
                idempotency_key: Optional[str] = None) -> Proposal:
        if not isinstance(action, str) or not action.strip():
            raise GateError("'action' must be a non-empty string")
        if not isinstance(payload, dict):
            raise GateError("'payload' must be a dict")
        try:
            canonical_payload = json.loads(json.dumps(payload))
        except (TypeError, ValueError) as e:
            raise GateError(f"'payload' must be JSON-serialisable: {e}")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            raise GateError("'summary' must be 1 to 500 characters: it is what the person reads before deciding")
        if risk not in RISKS:
            raise GateError(f"'risk' must be one of {', '.join(RISKS)}")
        if not isinstance(proposed_by, str) or not proposed_by.strip():
            raise GateError("'proposed_by' must be a non-empty string")
        if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise GateError(f"'ttl_seconds' must be between 1 and {MAX_TTL_SECONDS}")

        payload_sha = _sha(_canon(canonical_payload))
        now = self.now()
        id_ = uuid.uuid4().hex[:12]
        data = {
            "action": action.strip(), "payload": canonical_payload, "summary": summary.strip(),
            "proposed_by": proposed_by.strip(), "risk": risk,
            "expires_at": _iso(now + timedelta(seconds=ttl_seconds)), "payload_sha256": payload_sha,
        }
        if idempotency_key:
            data["idempotency_key"] = idempotency_key

        def guard(events: List[dict]) -> None:
            if idempotency_key:
                for existing in self._state(events).values():
                    if existing.idempotency_key == idempotency_key:
                        if existing.payload_sha256 != payload_sha or existing.action != data["action"]:
                            raise GateError("this idempotency key was already used for a different proposal")
                        raise _Existing(existing.id)

        try:
            self.log.append(_iso(now), "proposed", id_, data, guard=guard)
        except _Existing as same:
            return self.get(same.id)
        if risk == "low" and "low" in self.auto_approve:
            self._decide("approved", id_, "policy:auto-low", "auto-approved by policy (low risk)", system=True)
        return self.get(id_)

    # -- deciding
    def approve(self, id_or_prefix: str, by: str, reason: str = "") -> Proposal:
        return self._decide("approved", self.get(id_or_prefix).id, by, reason)

    def reject(self, id_or_prefix: str, by: str, reason: str = "") -> Proposal:
        return self._decide("rejected", self.get(id_or_prefix).id, by, reason)

    def _decide(self, kind: str, id_: str, by: str, reason: str, system: bool = False) -> Proposal:
        if not isinstance(by, str) or not by.strip():
            raise GateError("'by' must say who is deciding")
        by = by.strip()

        def guard(events: List[dict]) -> None:
            p = self._state(events).get(id_)
            if p is None:
                raise NotFound(f"no proposal with id {id_!r}")
            if p.status != "pending":
                raise InvalidTransition(f"{id_} is {p.status}, not pending")
            if not system and self.require_different_person and by.lower() == p.proposed_by.lower():
                raise InvalidTransition(f"{by!r} proposed this and cannot decide it too")

        data: Dict[str, Any] = {"by": by, "reason": reason}
        if kind == "approved":
            data["payload_sha256"] = self.get(id_).payload_sha256
        self.log.append(_iso(self.now()), kind, id_, data, guard=guard)
        return self.get(id_)

    def sweep(self) -> int:
        """Record an 'expired' event for every pending proposal past its deadline."""
        count = 0
        for p in self.list("expired"):
            try:
                self.log.append(_iso(self.now()), "expired", p.id, {},
                                guard=lambda events, pid=p.id: self._need_unrecorded_expiry(events, pid))
                count += 1
            except InvalidTransition:
                continue  # already recorded
        return count

    @staticmethod
    def _need_unrecorded_expiry(events: List[dict], id_: str) -> None:
        # the status without applying the clock: only a recorded 'expired' event changes it
        raw = fold(events, datetime(1970, 1, 1, tzinfo=timezone.utc)).get(id_)
        if raw is None or raw.status != "pending":
            raise InvalidTransition(f"{id_} has no unrecorded expiry")

    def _need(self, events: List[dict], id_: str, status: str) -> None:
        p = self._state(events).get(id_)
        if p is None or p.status != status:
            raise InvalidTransition(f"{id_} is no longer {status}")

    # -- running
    def run_approved(self, handlers: Dict[str, Callable[[dict], Any]], runner: str = "runner") -> List[Proposal]:
        """Run every approved proposal whose action has a handler, at most once each.

        A 'started' event is written BEFORE the handler runs. If the process dies in
        between, the proposal stays 'executing' and is never retried automatically:
        a person must look at it and call `resolve()`.
        """
        finished: List[Proposal] = []
        for p in self.list("approved"):
            handler = handlers.get(p.action)
            if handler is None:
                continue
            if _sha(_canon(p.payload)) != p.payload_sha256 or p.approved_sha256 != p.payload_sha256:
                raise IntegrityError(f"{p.id}: the payload no longer matches what was approved")
            try:
                self.log.append(_iso(self.now()), "started", p.id, {"by": runner},
                                guard=lambda events, pid=p.id: self._need(events, pid, "approved"))
            except InvalidTransition:
                continue  # another runner got there first
            try:
                result = handler(json.loads(json.dumps(p.payload)))
            except Exception as e:  # the handler's own errors must not stop the gate
                self.log.append(_iso(self.now()), "failed", p.id, {"error": f"{type(e).__name__}: {e}"})
            else:
                try:
                    json.dumps(result)
                except (TypeError, ValueError):
                    result = repr(result)[:200]
                self.log.append(_iso(self.now()), "executed", p.id, {"result": result})
            finished.append(self.get(p.id))
        return finished

    def resolve(self, id_or_prefix: str, by: str, outcome: str, note: str = "") -> Proposal:
        """A person closes a proposal stuck in 'executing' after checking what really happened."""
        if outcome not in ("executed", "failed"):
            raise GateError("'outcome' must be 'executed' or 'failed'")
        id_ = self.get(id_or_prefix).id
        self.log.append(_iso(self.now()), "resolved", id_, {"by": by, "outcome": outcome, "note": note},
                        guard=lambda events: self._need(events, id_, "executing"))
        return self.get(id_)


class _Existing(Exception):
    def __init__(self, id_: str):
        super().__init__(id_)
        self.id = id_
