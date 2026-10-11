"""Per-job review state machine. Every transition is explicit; anything else is a bug.

    READY
      → INPUT_PREPARED                input fingerprint + idempotency key computed
      → SUBMISSION_INTENT_RECORDED    journal fsynced BEFORE the provider is called
      → SUBMITTED                     provider returned a receipt
      → RESULT_DETECTED               a report was collected (stored in the journal)
      → RESULT_VALIDATED              schema + freshness + fingerprint checks passed
      → COMMITTED                     report persisted to the sink, then journal updated

    Exception branches:
      SAFE_TO_RETRY              provably nothing was submitted (rejected, or failed before submit)
      MANUAL_RECOVERY_REQUIRED   a submission may exist but cannot be proven, or its report failed
                                 validation; never resolved automatically, never resubmitted
      PROVIDER_UNAVAILABLE       quota exhausted / outage before submitting; the run stops
"""
from __future__ import annotations

READY = "READY"
INPUT_PREPARED = "INPUT_PREPARED"
SUBMISSION_INTENT_RECORDED = "SUBMISSION_INTENT_RECORDED"
SUBMITTED = "SUBMITTED"
RESULT_DETECTED = "RESULT_DETECTED"
RESULT_VALIDATED = "RESULT_VALIDATED"
COMMITTED = "COMMITTED"
SAFE_TO_RETRY = "SAFE_TO_RETRY"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

HAPPY_PATH = (READY, INPUT_PREPARED, SUBMISSION_INTENT_RECORDED, SUBMITTED,
              RESULT_DETECTED, RESULT_VALIDATED, COMMITTED)
STATES = HAPPY_PATH + (SAFE_TO_RETRY, MANUAL_RECOVERY_REQUIRED, PROVIDER_UNAVAILABLE)

TRANSITIONS: dict[str, frozenset[str]] = {
    READY: frozenset({INPUT_PREPARED, PROVIDER_UNAVAILABLE}),
    INPUT_PREPARED: frozenset({SUBMISSION_INTENT_RECORDED, READY, PROVIDER_UNAVAILABLE}),
    SUBMISSION_INTENT_RECORDED: frozenset({SUBMITTED, SAFE_TO_RETRY, PROVIDER_UNAVAILABLE,
                                           MANUAL_RECOVERY_REQUIRED}),
    SUBMITTED: frozenset({RESULT_DETECTED, MANUAL_RECOVERY_REQUIRED}),
    RESULT_DETECTED: frozenset({RESULT_VALIDATED, MANUAL_RECOVERY_REQUIRED}),
    RESULT_VALIDATED: frozenset({COMMITTED}),
    COMMITTED: frozenset(),
    SAFE_TO_RETRY: frozenset({INPUT_PREPARED}),
    PROVIDER_UNAVAILABLE: frozenset({INPUT_PREPARED}),
    # operator-only exits (scoring.engine.resolve_*): proven not submitted, or a report adopted by hand
    MANUAL_RECOVERY_REQUIRED: frozenset({SAFE_TO_RETRY, RESULT_DETECTED}),
}

# A journal in one of these states may hold a submission: a new submission is never allowed.
POST_INTENT = frozenset({SUBMISSION_INTENT_RECORDED, SUBMITTED, RESULT_DETECTED,
                         RESULT_VALIDATED, COMMITTED, MANUAL_RECOVERY_REQUIRED})
# A run may start a fresh submission from these.
STARTABLE = frozenset({READY, INPUT_PREPARED, SAFE_TO_RETRY, PROVIDER_UNAVAILABLE})

assert set(TRANSITIONS) == set(STATES)
assert all(t <= set(STATES) for t in TRANSITIONS.values())


class IllegalTransition(Exception):
    pass


def check_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, ()):
        raise IllegalTransition(f"{current} -> {target} is not allowed")
