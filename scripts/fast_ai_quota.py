"""Shared persistent Copilot quota circuit breaker; never a candidate failure."""
import datetime as dt
import json
import os
from pathlib import Path

STATE = Path(os.environ.get("FAST_AI_QUOTA_STATE", "state/ai_quota.json"))


class QuotaExhausted(RuntimeError):
    pass


def is_quota_error(message):
    message = str(message).lower()
    return any(text in message for text in (
        "exceeded your monthly quota", "monthly quota exceeded",
        "insufficient_quota", "no ai credits remaining", "exhausted your ai credits",
    ))


def paused():
    if not STATE.exists():
        return False
    state = json.loads(STATE.read_text())
    until = state.get("next_probe_at")
    return bool(until and dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(until))


def pause():
    now = dt.datetime.now(dt.timezone.utc)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = {"reason": "copilot_quota_exhausted", "paused_at": now.isoformat(),
             "next_probe_at": (now + dt.timedelta(hours=24)).isoformat()}
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(STATE)


def recovered():
    if STATE.exists():
        STATE.write_text('{}\n')
