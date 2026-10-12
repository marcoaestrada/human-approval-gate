"""A complete round trip in one script. Nothing is sent: the handler only prints.

    python examples/demo.py

In real use, steps 1 and 3 run in different places and at different times:
the agent proposes during the day; a person decides with `python -m approval_gate review`;
a runner executes whatever was approved.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from approval_gate import ApprovalGate  # noqa: E402


def send_email(payload):
    print(f"   [handler] pretending to send an email to {payload['to']}: {payload['subject']!r}")
    return {"sent": False, "dry_run": True}


with tempfile.TemporaryDirectory() as folder:
    gate = ApprovalGate(os.path.join(folder, "approvals.jsonl"))

    print("1. The agent proposes an action. Nothing happens yet.")
    proposal = gate.propose(
        action="send_email",
        payload={"to": "customer@example.com", "subject": "Your booking is confirmed"},
        summary="Confirm the booking of 4 people on Friday to customer@example.com",
        proposed_by="agent:assistant",
        risk="medium",
    )
    print(f"   proposed {proposal.id}, status: {proposal.status}")

    print("2. Running the runner now does nothing, because nobody approved:")
    print(f"   executed: {len(gate.run_approved({'send_email': send_email}))} action(s)")

    print("3. A person reads the summary and approves.")
    gate.approve(proposal.id, by="marco", reason="Checked the guest list")
    print(f"   status: {gate.get(proposal.id).status}")

    print("4. The runner executes it, once.")
    gate.run_approved({"send_email": send_email})
    print(f"   status: {gate.get(proposal.id).status}")
    print(f"   a second run executes {len(gate.run_approved({'send_email': send_email}))} action(s)")

    print("5. The log proves nothing was edited afterwards:")
    print(f"   {gate.verify()} events, chain intact")
