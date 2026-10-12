import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from approval_gate import ApprovalGate, GateError, IntegrityError, InvalidTransition, NotFound
from approval_gate import cli


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 13, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "approvals.jsonl")
        self.clock = FakeClock()

    def gate(self, **kwargs):
        return ApprovalGate(self.path, clock=self.clock, **kwargs)

    def propose(self, gate, **kwargs):
        defaults = dict(action="send_email", payload={"to": "a@example.com", "body": "Hello"},
                        summary="Send the welcome email to a@example.com", proposed_by="agent")
        defaults.update(kwargs)
        return gate.propose(**defaults)


class ProposeAndDecide(Base):
    def test_a_new_proposal_waits_for_a_person(self):
        gate = self.gate()
        p = self.propose(gate)
        self.assertEqual(p.status, "pending")
        self.assertEqual([x.id for x in gate.pending()], [p.id])

    def test_approve_and_reject(self):
        gate = self.gate()
        a, r = self.propose(gate), self.propose(gate, summary="Another one")
        self.assertEqual(gate.approve(a.id, "marco", "looks right").status, "approved")
        self.assertEqual(gate.reject(r.id, "marco", "wrong recipient").status, "rejected")
        self.assertEqual(gate.get(r.id).reason, "wrong recipient")
        self.assertEqual(gate.pending(), [])

    def test_a_decision_cannot_be_changed(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        with self.assertRaises(InvalidTransition):
            gate.reject(p.id, "marco", "changed my mind")
        with self.assertRaises(InvalidTransition):
            gate.approve(p.id, "marco")

    def test_the_proposer_cannot_approve_their_own_proposal(self):
        gate = self.gate()
        p = self.propose(gate, proposed_by="Agent")
        with self.assertRaises(InvalidTransition):
            gate.approve(p.id, "agent")  # same person, case-insensitive
        gate_solo = self.gate(require_different_person=False)
        self.assertEqual(gate_solo.approve(p.id, "agent").status, "approved")

    def test_id_prefixes(self):
        gate = self.gate()
        p = self.propose(gate)
        self.assertEqual(gate.get(p.id[:5]).id, p.id)
        with self.assertRaises(NotFound):
            gate.get("zzzz")
        with self.assertRaises(NotFound):
            gate.get(p.id[:2])  # too short to be a prefix

    def test_input_validation(self):
        gate = self.gate()
        for bad in (
            dict(action=""), dict(payload="text"), dict(payload={"x": object()}),
            dict(summary=""), dict(summary="x" * 501), dict(risk="extreme"),
            dict(proposed_by=""), dict(ttl_seconds=0), dict(ttl_seconds=10 ** 9),
        ):
            with self.assertRaises(GateError, msg=str(bad)):
                self.propose(gate, **bad)


class Expiry(Base):
    def test_a_proposal_expires_and_can_no_longer_be_approved(self):
        gate = self.gate()
        p = self.propose(gate, ttl_seconds=3600)
        self.clock.advance(hours=2)
        self.assertEqual(gate.get(p.id).status, "expired")
        with self.assertRaises(InvalidTransition):
            gate.approve(p.id, "marco")

    def test_sweep_records_the_expiry_once(self):
        gate = self.gate()
        self.propose(gate, ttl_seconds=60)
        self.clock.advance(minutes=5)
        self.assertEqual(gate.sweep(), 1)
        self.assertEqual(gate.sweep(), 0)
        self.assertEqual([e["type"] for e in gate.log.read()].count("expired"), 1)


class Idempotency(Base):
    def test_the_same_key_returns_the_same_proposal(self):
        gate = self.gate()
        first = self.propose(gate, idempotency_key="welcome-42")
        second = self.propose(gate, idempotency_key="welcome-42")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(gate.list()), 1)

    def test_the_same_key_with_a_different_payload_is_refused(self):
        gate = self.gate()
        self.propose(gate, idempotency_key="k")
        with self.assertRaises(GateError):
            self.propose(gate, idempotency_key="k", payload={"to": "other@example.com"})


class AutoApprovalPolicy(Base):
    def test_only_low_risk_can_be_auto_approved(self):
        with self.assertRaises(GateError):
            self.gate(auto_approve=["medium"])
        with self.assertRaises(GateError):
            self.gate(auto_approve=["high"])

    def test_low_risk_is_auto_approved_only_when_configured(self):
        self.assertEqual(self.propose(self.gate(), risk="low").status, "pending")
        gate = self.gate(auto_approve=["low"])
        low = self.propose(gate, risk="low")
        self.assertEqual(low.status, "approved")
        self.assertEqual(low.decided_by, "policy:auto-low")
        self.assertEqual(self.propose(gate, risk="medium").status, "pending")
        self.assertEqual(self.propose(gate, risk="high").status, "pending")


class Running(Base):
    def test_only_approved_proposals_run_and_only_once(self):
        gate = self.gate()
        approved, waiting = self.propose(gate), self.propose(gate, summary="Not decided yet")
        gate.approve(approved.id, "marco")
        calls = []
        handlers = {"send_email": lambda payload: calls.append(payload) or {"sent": True}}
        done = gate.run_approved(handlers)
        self.assertEqual([p.id for p in done], [approved.id])
        self.assertEqual(gate.get(approved.id).status, "executed")
        self.assertEqual(gate.get(approved.id).result, {"sent": True})
        self.assertEqual(gate.get(waiting.id).status, "pending")
        self.assertEqual(gate.run_approved(handlers), [])  # nothing runs twice
        self.assertEqual(len(calls), 1)

    def test_a_handler_error_is_recorded_and_not_retried(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        attempts = []

        def broken(payload):
            attempts.append(1)
            raise RuntimeError("mail server down")

        gate.run_approved({"send_email": broken})
        self.assertEqual(gate.get(p.id).status, "failed")
        self.assertIn("mail server down", gate.get(p.id).error)
        gate.run_approved({"send_email": broken})
        self.assertEqual(len(attempts), 1)

    def test_a_crash_between_start_and_finish_is_never_retried_automatically(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        gate.log.append("2026-10-13T08:00:00Z", "started", p.id, {"by": "dead-runner"})  # simulates a crash
        calls = []
        self.assertEqual(gate.run_approved({"send_email": lambda payload: calls.append(1)}), [])
        self.assertEqual(calls, [])
        self.assertEqual(gate.get(p.id).status, "executing")
        self.assertEqual(gate.resolve(p.id, "marco", "executed", "checked: the email was sent").status, "executed")
        with self.assertRaises(InvalidTransition):
            gate.resolve(p.id, "marco", "failed")

    def test_the_handler_gets_a_copy_of_the_payload(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")

        def mutate(payload):
            payload["to"] = "changed@example.com"

        gate.run_approved({"send_email": mutate})
        self.assertEqual(gate.get(p.id).payload["to"], "a@example.com")

    def test_actions_without_a_handler_are_left_alone(self):
        gate = self.gate()
        p = self.propose(gate, action="publish_post")
        gate.approve(p.id, "marco")
        self.assertEqual(gate.run_approved({"send_email": lambda payload: None}), [])
        self.assertEqual(gate.get(p.id).status, "approved")


class Integrity(Base):
    def test_an_untouched_log_verifies(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        self.assertEqual(gate.verify(), 2)

    def test_editing_an_old_line_is_detected(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        with open(self.path, "r", encoding="utf-8") as f:
            text = f.read()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text.replace("a@example.com", "evil@example.com", 1))
        with self.assertRaises(IntegrityError):
            gate.verify()

    def test_removing_a_line_is_detected(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        gate.reject(self.propose(gate, summary="second").id, "marco", "no")
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(lines[:1] + lines[2:])
        with self.assertRaises(IntegrityError):
            gate.verify()

    def test_a_changed_payload_is_refused_at_run_time(self):
        gate = self.gate()
        p = self.propose(gate)
        gate.approve(p.id, "marco")
        with open(self.path, "r", encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        events[0]["data"]["payload"]["to"] = "evil@example.com"  # edit, keep everything else
        with open(self.path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        with self.assertRaises(IntegrityError):
            gate.run_approved({"send_email": lambda payload: None})

    def test_garbage_in_the_log_is_reported(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json\n")
        with self.assertRaises(IntegrityError):
            self.gate().verify()

    def test_simultaneous_writers_do_not_lose_or_corrupt_events(self):
        gate = ApprovalGate(self.path)  # real clock, real threads
        errors = []

        def worker(n):
            try:
                for i in range(8):
                    gate.propose("send_email", {"n": n, "i": i}, f"proposal {n}-{i}", proposed_by=f"agent{n}")
            except Exception as e:  # pragma: no cover - would be a failure
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(gate.verify(), 32)
        self.assertEqual(len(gate.list()), 32)

    def test_only_one_of_two_approvers_wins(self):
        gate = self.gate()
        p = self.propose(gate)
        results = []

        def decide(name):
            try:
                gate.approve(p.id, name)
                results.append("ok")
            except InvalidTransition:
                results.append("refused")

        threads = [threading.Thread(target=decide, args=(n,)) for n in ("marco", "ana")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertEqual(gate.verify(), 2)


class CommandLine(Base):
    def run_cli(self, *args, inputs=()):
        answers = iter(inputs)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--log", self.path, *args], input_fn=lambda prompt: next(answers))
        return code, out.getvalue(), err.getvalue()

    def test_full_flow(self):
        code, out, _ = self.run_cli("propose", "--action", "send_email", "--summary", "Send the welcome email",
                                    "--payload", '{"to": "a@example.com"}', "--risk", "low", "--by", "agent")
        self.assertEqual(code, 0)
        pid = out.split()[1]
        _, out, _ = self.run_cli("list")
        self.assertIn(pid, out)
        _, out, _ = self.run_cli("show", pid[:6])
        self.assertIn("a@example.com", out)
        self.assertEqual(self.run_cli("approve", pid, "--by", "marco")[0], 0)
        self.assertEqual(self.run_cli("approve", pid, "--by", "marco")[0], 1)  # already decided
        _, out, _ = self.run_cli("verify")
        self.assertIn("2 event(s)", out)
        _, out, _ = self.run_cli("list")
        self.assertIn("nothing pending", out)

    def test_review_walks_through_pending_items(self):
        gate = self.gate()
        ids = []
        for n in range(3):  # different timestamps, so the order is the order of proposing
            ids.append(self.propose(gate, summary=f"item {n}", ttl_seconds=30 * 24 * 3600).id)
            self.clock.advance(seconds=1)
        code, out, _ = self.run_cli("review", "--by", "marco", inputs=["a", "x", "r", "too risky", "s"])
        self.assertEqual(code, 0)
        statuses = {p.id: p.status for p in gate.list()}
        self.assertEqual(statuses[ids[0]], "approved")
        self.assertEqual(statuses[ids[1]], "rejected")
        self.assertEqual(statuses[ids[2]], "pending")

    def test_errors_give_exit_code_1_and_a_message(self):
        code, _, err = self.run_cli("show", "nonexistent")
        self.assertEqual((code, "no proposal" in err), (1, True))
        code, _, err = self.run_cli("propose", "--action", "a", "--summary", "s", "--payload", "{bad json")
        self.assertEqual(code, 1)

    def test_verify_fails_on_an_edited_log(self):
        self.run_cli("propose", "--action", "a", "--summary", "original")
        with open(self.path, "r", encoding="utf-8") as f:
            text = f.read()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text.replace("original", "changed"))
        code, _, err = self.run_cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("edited", err)


if __name__ == "__main__":
    unittest.main()
