# human-approval-gate

**An AI proposes. A person decides. Everything is logged.**

A small Python library and command line (standard library only, no dependencies) for the moment between "an agent wants to do something" and "it actually happens". The agent can only *propose* an action. Nothing runs until a person approves it. Every step is one line in an append-only file, and each line carries the hash of the one before it, so an edit to the past can be detected.

It is the rule *"the AI does the repetitive work, a person keeps the judgment"* written as code.

## See it in 30 seconds

```bash
git clone https://github.com/marcoaestrada/human-approval-gate.git
cd human-approval-gate
python examples/demo.py
```

```
1. The agent proposes an action. Nothing happens yet.
   proposed 3f9a1c2b7d40, status: pending
2. Running the runner now does nothing, because nobody approved:
   executed: 0 action(s)
3. A person reads the summary and approves.
   status: approved
4. The runner executes it, once.
   [handler] pretending to send an email to customer@example.com: 'Your booking is confirmed'
   status: executed
   a second run executes 0 action(s)
5. The log proves nothing was edited afterwards:
   4 events, chain intact
```

## Use it from Python (the agent's side)

```python
from approval_gate import ApprovalGate

gate = ApprovalGate("approvals.jsonl")

proposal = gate.propose(
    action="send_email",
    payload={"to": "customer@example.com", "subject": "Your booking is confirmed"},
    summary="Confirm the booking of 4 people on Friday to customer@example.com",  # what the person reads
    proposed_by="agent:assistant",
    risk="medium",                       # low | medium | high
    idempotency_key="booking-1042",      # proposing the same thing twice returns the same proposal
)

# later, in a separate process that holds the real handlers:
gate.run_approved({"send_email": my_send_email_function})
```

## Use it from the command line (the person's side)

```bash
python -m approval_gate list                      # what is waiting
python -m approval_gate show 3f9a                 # the full details (an id prefix is enough)
python -m approval_gate approve 3f9a --by marco
python -m approval_gate reject  3f9a --by marco --reason "wrong recipient"
python -m approval_gate review  --by marco        # go through every pending item, one by one
python -m approval_gate verify                    # has the log been edited?
```

The log file is `approvals.jsonl` in the current folder, or the path in `$APPROVAL_GATE_LOG` or `--log`.

To record a proposal from the command line: `python -m approval_gate propose --action send_email --summary "Send the welcome email" --payload '{"to": "a@example.com"}' --risk low`. **Windows PowerShell tip:** quotes inside JSON are lost when passed on the command line; write the JSON to a file and use `--payload-file payload.json` instead (a file saved with a BOM is accepted).

## The rules it enforces

| Rule | Why |
|---|---|
| **Nothing runs without a recorded approval.** | The point of the whole thing. |
| **The proposer cannot approve their own proposal** (switch off with `require_different_person=False`, for a solo user). | A second pair of eyes, even if the first is an AI. |
| **Proposals expire** (default 24 h, at most 30 days). | An old approval request should not be answered by habit. |
| **An approval is bound to the exact payload** (its SHA-256 is stored). A changed payload is refused at run time. | What was approved is what runs. |
| **At most once.** A `started` event is written *before* the handler runs. A crash in between leaves the proposal `executing`; it is never retried automatically, and a person closes it with `resolve()`. | Sending twice is usually worse than sending zero times. |
| **Handler errors are recorded**, not retried, not hidden. | You can see exactly what failed. |
| **Auto-approval exists only for `low` risk, and only if you switch it on** (`auto_approve=["low"]`). Medium and high can never be auto-approved. | Convenience for harmless actions, never for the rest. |
| **A decision cannot be changed.** | The history means something. |
| **The log is append-only and hash-chained**; `verify` finds edited, removed or reordered lines. | An audit trail you can check. |

## What it is not

- **Not authentication.** `--by marco` is a label. Anyone who can run the command can type any name. If you need to know *who* really approved, put the gate behind your own login or review process.
- **Not tamper-proof against someone who controls the file.** The hash chain detects edits to an existing line. A person who can rewrite the whole file *and* recompute every hash can forge a new history. For stronger guarantees, copy the latest hash somewhere you trust.
- **Not a sandbox.** Your handlers are ordinary functions that run with your permissions. The gate only decides *whether* they run.
- **Not a queue server.** It is one file, built for small teams and single machines; every operation reads the whole file.
- **Not legal advice**, and not a substitute for your own review of what you let an agent do.

## Design notes

- Storage is JSON Lines, one event per line: `proposed`, `approved`, `rejected`, `expired`, `started`, `executed`, `failed`, `resolved`. The state of a proposal is calculated by replaying the events.
- Writers take a short lock (a lock file) and check the current state *inside* the lock, so two people approving at once cannot both win.
- Python 3.9+. Tests use a fake clock and a temporary folder: `python -m unittest discover -s tests -t . -v`.

## Support

Shared as-is. There is **no guaranteed support**; issues and pull requests may take a long time to be answered or may not be answered.

## License

MIT. See [LICENSE](LICENSE).
