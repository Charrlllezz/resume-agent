#!/usr/bin/env python3
"""Token accounting, in its own module on purpose.

Two separate problems put this here rather than in tailor_resume.

**Double import.** `python tailor_resume.py` runs that file as `__main__`.
fit.py then reaches back for it by name, which loads a *second, independent*
copy of the module -- so fit's tokens landed in one accumulator and the CLI
printed the other. Every per-role cost the CLI has ever reported was missing
the fit call. Shared mutable state cannot live in a module that can also be
`__main__`; this module can only ever be imported, so there is one of it.

**Concurrency.** A CLI runs once per process, so a plain dict was fine. A
server runs several at once, and a process-wide dict would report one user's
spend to another. A ContextVar gives each thread its own accumulator while
leaving single-run behaviour identical -- a lone thread just gets the one it
lazily creates.
"""

import contextvars
from contextlib import contextmanager

# Opus 5, $ per million tokens.
PRICE_IN, PRICE_OUT = 5.00, 25.00
PRICE_CACHE_WRITE, PRICE_CACHE_READ = 6.25, 0.50

_var = contextvars.ContextVar("resume_agent_usage", default=None)


def new() -> dict:
    return {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0, "calls": 0}


def now() -> dict:
    """This context's accumulator, created on first use."""
    current = _var.get()
    if current is None:
        current = new()
        _var.set(current)
    return current


@contextmanager
def scope():
    """Isolate token accounting to one run. Yields that run's own totals."""
    token = _var.set(new())
    try:
        yield _var.get()
    finally:
        _var.reset(token)


def record(response):
    """Accumulate token usage from a response. Costs nothing extra."""
    u = getattr(response, "usage", None)
    if not u:
        return response
    acc = now()
    acc["calls"] += 1
    acc["in"] += getattr(u, "input_tokens", 0) or 0
    acc["out"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    acc["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    return response


def cost(acc: dict) -> float:
    return (acc["in"] * PRICE_IN
            + acc["out"] * PRICE_OUT
            + acc["cache_write"] * PRICE_CACHE_WRITE
            + acc["cache_read"] * PRICE_CACHE_READ) / 1_000_000


def line(acc: dict) -> str:
    parts = [f"{acc['calls']} API call(s)", f"{acc['in']:,} in", f"{acc['out']:,} out"]
    if acc["cache_read"] or acc["cache_write"]:
        parts.append(f"{acc['cache_read']:,} cached read")
        parts.append(f"{acc['cache_write']:,} cache write")
    out = f"  {' | '.join(parts)}  ~${cost(acc):.3f}"
    # A zero cache read across a run means the master-resume breakpoint is not
    # hitting and every call is paying full price for it.
    if acc["calls"] > 1 and acc["cache_read"] == 0 and acc["cache_write"]:
        out += ("\n  ⚠  cache written but never read — "
                "the prompt prefix is changing between calls")
    return out
