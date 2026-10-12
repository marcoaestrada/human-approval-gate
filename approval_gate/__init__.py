"""human-approval-gate: an AI proposes, a person approves, everything is logged."""
from .core import (  # noqa: F401
    ApprovalGate, EventLog, GateError, IntegrityError, InvalidTransition, NotFound, Proposal, RISKS,
)

__version__ = "0.1.0"
