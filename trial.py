#!/usr/bin/env python3
"""The free trial: a few runs on the host's key, then bring your own.

Asking a stranger for an Anthropic API key before showing them anything is a
wall most people will not climb. This lets them run it two or three times on
the host's account first, which costs the host real money -- so everything
here exists to bound that.

**The key is deliberately not `ANTHROPIC_API_KEY`.** The SDK falls back to that
variable on its own, so a hosted process holding it would spend the host's
money down any code path that forgot to pass a key -- silently, with no trial
accounting in front of it. Naming it `RESUME_AGENT_DEMO_KEY` means the SDK can
never pick it up, and every use has to come through this module on purpose.

**This is the second line of defence, not the first.** The first is a spend
limit on the Anthropic workspace the demo key belongs to, which is enforced by
Anthropic's billing rather than by this file. Guards written here can have bugs;
that one cannot. Set both.

Four independent limits, because they fail differently:

  per session   a person cannot sit and run twenty
  per IP/day    ...nor open twenty sessions to get around that
  per day ($)   the whole trial stops when the day's budget is gone
  reservation   concurrent runs cannot each see the last dollar and take it

The ledger is written to disk because Fly stops an idle machine and starts it
again on the next request. In memory, the daily budget would reset every time
the site went quiet -- which is to say, on demand. It holds counts, salted IP
hashes and dollars: no keys, no resumes, no raw addresses.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# What one action costs, charged up front and trued up afterwards. These are
# deliberately a little high: a reservation that is too small lets concurrent
# runs overshoot the budget, while one that is too large only makes the trial
# slightly more cautious than it needs to be.
ESTIMATE = {"run": 0.30, "ingest": 0.12}

FREE_RUNS_PER_SESSION = int(os.environ.get("FREE_RUNS_PER_SESSION", "3"))
FREE_INGESTS_PER_SESSION = int(os.environ.get("FREE_INGESTS_PER_SESSION", "3"))
FREE_ACTIONS_PER_IP = int(os.environ.get("FREE_ACTIONS_PER_IP", "8"))
DAILY_BUDGET = float(os.environ.get("TRIAL_DAILY_BUDGET", "5.00"))

STATE_DIR = Path(os.environ.get("RESUME_AGENT_STATE_DIR", "/app/.state"))


def demo_key() -> str:
    """The host's key, or '' if no trial is offered. Never ANTHROPIC_API_KEY."""
    return os.environ.get("RESUME_AGENT_DEMO_KEY", "").strip()


def enabled() -> bool:
    return bool(demo_key())


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def hash_ip(ip: str) -> str:
    """Salted, truncated, one-way. The ledger should not be a list of who
    visited: it only ever needs to know 'this one again' for a day."""
    salt = os.environ.get("SECRET_KEY", "resume-agent-dev-salt")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


class Ledger:
    """The day's trial spending. One per process; guarded by a lock.

    A single gunicorn worker means one of these, which is the same reason the
    session store can live in memory. More workers would need this in Redis --
    and would break sessions first, so the constraint is already there.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else STATE_DIR / "trial.json"
        self._lock = threading.Lock()
        self._state = self._load()

    # -- persistence --------------------------------------------------------

    def _blank(self) -> dict:
        return {"day": _today(), "spent": 0.0, "reserved": 0.0,
                "actions": 0, "ips": {}}

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._blank()
        # A ledger from a previous day is not an error, it is just spent.
        if state.get("day") != _today():
            return self._blank()
        # Reservations do not survive a restart: whatever they were holding
        # either completed (and was settled into `spent` before the stop) or
        # died with the process. Carrying them forward would leak budget.
        state["reserved"] = 0.0
        return state

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            # An unwritable state dir must not take the site down. The trial
            # then bounds itself per-session and per-IP within this process's
            # lifetime, and the workspace spend limit still holds the floor.
            pass

    def _roll(self):
        if self._state.get("day") != _today():
            self._state = self._blank()

    # -- accounting ---------------------------------------------------------

    def committed(self) -> float:
        """Spent plus in-flight. The number the budget is checked against."""
        return self._state["spent"] + self._state["reserved"]

    def remaining(self) -> float:
        with self._lock:
            self._roll()
            return max(0.0, DAILY_BUDGET - self.committed())

    def reserve(self, ip_hash: str, action: str) -> str:
        """Hold budget for one action. Returns '' to proceed, or why not.

        Reserving before the work rather than charging after it is what makes
        concurrency safe: eight simultaneous runs each take their $0.30 out of
        the budget at the start, so the eighth is refused if the money is gone
        instead of all eight reading the same healthy balance.
        """
        cost = ESTIMATE.get(action, 0.30)
        with self._lock:
            self._roll()
            if self.committed() + cost > DAILY_BUDGET:
                return ("The free trial has used up today's budget. Add your own "
                        "Anthropic key to keep going — it resets at midnight UTC.")
            if self._state["ips"].get(ip_hash, 0) >= FREE_ACTIONS_PER_IP:
                return ("That's the free trial used up for today. Add your own "
                        "Anthropic key to keep going.")
            self._state["reserved"] += cost
            self._state["ips"][ip_hash] = self._state["ips"].get(ip_hash, 0) + 1
            self._state["actions"] += 1
            self._save()
            return ""

    def settle(self, action: str, actual: float | None):
        """Release the reservation and record what was really spent.

        `actual` is None when the run failed after it had already called the
        model, where the tokens are gone with the exception. Charging the full
        estimate there is the conservative reading, and the right one: a loop
        of failing runs must still exhaust the budget.
        """
        held = ESTIMATE.get(action, 0.30)
        with self._lock:
            self._roll()
            self._state["reserved"] = max(0.0, self._state["reserved"] - held)
            self._state["spent"] += held if actual is None else max(0.0, actual)
            self._save()

    def refund(self, ip_hash: str, action: str):
        """Give it all back: this action never reached the model.

        A posting URL that does not resolve fails before the first API call.
        Charging someone a trial run for a typo teaches them the trial is
        broken.
        """
        held = ESTIMATE.get(action, 0.30)
        with self._lock:
            self._roll()
            self._state["reserved"] = max(0.0, self._state["reserved"] - held)
            if self._state["ips"].get(ip_hash):
                self._state["ips"][ip_hash] -= 1
            self._state["actions"] = max(0, self._state["actions"] - 1)
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._state["day"],
                    "spent": round(self._state["spent"], 4),
                    "reserved": round(self._state["reserved"], 4),
                    "actions": self._state["actions"],
                    "budget": DAILY_BUDGET,
                    "visitors": len(self._state["ips"])}


LEDGER = Ledger()


# ---------------------------------------------------------------------------
# Per-session quotas
# ---------------------------------------------------------------------------

QUOTA = {"run": FREE_RUNS_PER_SESSION, "ingest": FREE_INGESTS_PER_SESSION}


def used(s, action: str) -> int:
    return s.trial_used.get(action, 0)


def left(s, action: str) -> int:
    return max(0, QUOTA.get(action, 0) - used(s, action))


def check(s, action: str) -> str:
    """Can this session take this action on the trial? '' if yes, else why not.

    Session quota only -- the ledger's own limits are checked at reserve time,
    under its lock, where they cannot race.
    """
    if not enabled():
        return "The free trial isn't available on this deployment."
    if left(s, action) <= 0:
        if action == "run":
            return (f"That's the {FREE_RUNS_PER_SESSION} free runs. Add your own "
                    "Anthropic key to keep going — a run costs about 20¢.")
        return ("That's the free resume uploads for this session. Add your own "
                "Anthropic key to keep going.")
    return ""


def take(s, ip: str, action: str) -> str:
    """Claim one trial action for this session. '' if granted, else why not.

    The session counter goes up here, before the work: two requests racing
    would otherwise both read 'one left'.
    """
    refusal = check(s, action)
    if refusal:
        return refusal
    refusal = LEDGER.reserve(hash_ip(ip), action)
    if refusal:
        return refusal
    s.trial_used[action] = used(s, action) + 1
    return ""


def give_back(s, ip: str, action: str):
    """Undo a `take` for work that never reached the model."""
    s.trial_used[action] = max(0, used(s, action) - 1)
    LEDGER.refund(hash_ip(ip), action)


def client_ip(request) -> str:
    """The visitor's address, as far as it can be trusted.

    Fly's proxy sets Fly-Client-IP and it cannot be spoofed from outside;
    X-Forwarded-For can be, so its first entry is only a fallback, and
    remote_addr is the last resort. Getting this wrong makes the per-IP cap
    a formality, not a hole in anything else.
    """
    fly = request.headers.get("Fly-Client-IP")
    if fly:
        return fly.strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"
