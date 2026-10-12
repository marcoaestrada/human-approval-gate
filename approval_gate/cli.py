"""Command line for the person who decides.

    python -m approval_gate list
    python -m approval_gate show 3f9a
    python -m approval_gate approve 3f9a --by marco
    python -m approval_gate reject 3f9a --by marco --reason "wrong recipient"
    python -m approval_gate review --by marco        # go through every pending item
    python -m approval_gate verify                   # check the log was not edited
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, List, Optional

from . import __version__
from .core import ApprovalGate, GateError, Proposal, RISKS

EXIT_OK, EXIT_ERROR = 0, 1


def _show(p: Proposal, full: bool = True) -> str:
    lines = [
        f"id:         {p.id}",
        f"status:     {p.status}",
        f"risk:       {p.risk}",
        f"action:     {p.action}",
        f"summary:    {p.summary}",
        f"proposed:   {p.created_at} by {p.proposed_by}",
        f"expires:    {p.expires_at}",
    ]
    if p.decided_by:
        lines.append(f"decided:    {p.decided_at} by {p.decided_by}" + (f" ({p.reason})" if p.reason else ""))
    if p.error:
        lines.append(f"error:      {p.error}")
    if p.result is not None:
        lines.append(f"result:     {json.dumps(p.result)[:300]}")
    if full:
        payload = json.dumps(p.payload, indent=2, ensure_ascii=False)
        if len(payload) > 1500:
            payload = payload[:1500] + "\n  ... (truncated)"
        lines.append("payload:\n" + "\n".join("  " + line for line in payload.splitlines()))
    return "\n".join(lines)


def _row(p: Proposal) -> str:
    return f"{p.id}  {p.status:<9} {p.risk:<6} {p.action:<20} {p.summary[:70]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="approval-gate", description="An AI proposes, a person approves, everything is logged.")
    parser.add_argument("--log", default=os.environ.get("APPROVAL_GATE_LOG", "approvals.jsonl"),
                        help="path of the log file (default: approvals.jsonl, or $APPROVAL_GATE_LOG)")
    parser.add_argument("--version", action="version", version=f"approval-gate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="record a proposal (what an agent or script would do)")
    p.add_argument("--action", required=True)
    p.add_argument("--summary", required=True, help="one line the person reads before deciding")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--payload", help="JSON object with the details")
    group.add_argument("--payload-file", help="path of a JSON file with the details")
    p.add_argument("--risk", choices=RISKS, default="medium")
    p.add_argument("--by", default="agent", help="who proposes")
    p.add_argument("--ttl-hours", type=float, default=24.0)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("list", help="show pending proposals (or all with --all)")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("show", help="show one proposal in full")
    p.add_argument("id")

    p = sub.add_parser("approve", help="approve a proposal")
    p.add_argument("id")
    p.add_argument("--by", required=True, help="who approves")
    p.add_argument("--reason", default="")

    p = sub.add_parser("reject", help="reject a proposal")
    p.add_argument("id")
    p.add_argument("--by", required=True, help="who rejects")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("review", help="go through every pending proposal one by one")
    p.add_argument("--by", required=True)

    sub.add_parser("sweep", help="record the expiry of pending proposals past their deadline")
    sub.add_parser("verify", help="check that the log has not been edited")
    return parser


def main(argv: Optional[List[str]] = None, input_fn: Callable[[str], str] = input) -> int:
    args = build_parser().parse_args(argv)
    gate = ApprovalGate(args.log)
    try:
        if args.command == "propose":
            if args.payload_file:
                with open(args.payload_file, "r", encoding="utf-8-sig") as f:
                    payload = json.load(f)
            elif args.payload:
                payload = json.loads(args.payload)
            else:
                payload = {}
            proposal = gate.propose(args.action, payload, args.summary, proposed_by=args.by, risk=args.risk,
                                    ttl_seconds=int(args.ttl_hours * 3600), idempotency_key=args.idempotency_key)
            print(f"proposed {proposal.id} ({proposal.status})")
        elif args.command == "list":
            items = gate.list() if args.all else gate.pending()
            if not items:
                print("nothing pending" if not args.all else "the log is empty")
            for item in items:
                print(_row(item))
        elif args.command == "show":
            print(_show(gate.get(args.id)))
        elif args.command == "approve":
            print(f"approved {gate.approve(args.id, args.by, args.reason).id}")
        elif args.command == "reject":
            print(f"rejected {gate.reject(args.id, args.by, args.reason).id}")
        elif args.command == "review":
            pending = gate.pending()
            if not pending:
                print("nothing pending")
            for number, item in enumerate(pending, 1):
                print(f"\n--- {number} of {len(pending)} ---\n{_show(item)}")
                while True:
                    answer = input_fn("[a]pprove  [r]eject  [s]kip  [q]uit > ").strip().lower()
                    if answer in ("a", "approve"):
                        gate.approve(item.id, args.by)
                        print("approved")
                        break
                    if answer in ("r", "reject"):
                        reason = input_fn("reason > ").strip() or "rejected in review"
                        gate.reject(item.id, args.by, reason)
                        print("rejected")
                        break
                    if answer in ("s", "skip"):
                        break
                    if answer in ("q", "quit"):
                        return EXIT_OK
        elif args.command == "sweep":
            print(f"{gate.sweep()} expiry(ies) recorded")
        elif args.command == "verify":
            print(f"OK: {gate.verify()} event(s), the chain is intact")
    except (GateError, json.JSONDecodeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
