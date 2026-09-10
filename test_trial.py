#!/usr/bin/env python3
"""What the free trial must refuse.

Every assertion here is about somebody else's money. The interesting cases are
not "three runs works" but the ones where a limit has to hold under a race, a
restart, or a refresh -- so most of this is about the ledger rather than the
happy path.

Offline: no API key, no network, no model. Run it after touching trial.py.

    python test_trial.py
"""

import json
import os
import tempfile
import threading
from pathlib import Path

# app.py reads these at import time and never again: local mode builds a
# session from disk at module scope. Setting them inside a test would be too
# late for whichever test imported app first.
os.environ.update(RESUME_AGENT_HOSTED="1", RESUME_AGENT_INSECURE_COOKIES="1",
                  RESUME_AGENT_DEMO_KEY="sk-ant-fake")
os.environ.setdefault("SECRET_KEY", "test-salt")
os.environ.pop("ANTHROPIC_API_KEY", None)

import trial

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def section(name):
    print(f"\n{name}")


class FakeSession:
    """Duck-types the parts of sessions.Session the trial touches."""

    def __init__(self):
        self.trial_used = {}


def fresh_ledger(budget=5.00) -> trial.Ledger:
    tmp = Path(tempfile.mkdtemp()) / "trial.json"
    trial.DAILY_BUDGET = budget
    return trial.Ledger(path=tmp)


# ---------------------------------------------------------------------------

def test_session_quota():
    section("Per-session quota")
    trial.QUOTA["run"] = 3
    s = FakeSession()
    ok(trial.left(s, "run") == 3, "starts with the full allowance")
    s.trial_used["run"] = 3
    ok(trial.left(s, "run") == 0, "spent allowance is zero, not negative")
    s.trial_used["run"] = 9
    ok(trial.left(s, "run") == 0, "over-spend still reports zero, never negative")

    os.environ["RESUME_AGENT_DEMO_KEY"] = "sk-ant-test"
    s = FakeSession()
    ok(trial.check(s, "run") == "", "a fresh session may run")
    s.trial_used["run"] = 3
    ok("free runs" in trial.check(s, "run"), "the fourth run is refused")

    del os.environ["RESUME_AGENT_DEMO_KEY"]
    ok("isn't available" in trial.check(FakeSession(), "run"),
       "no demo key means no trial, whatever the session says")
    os.environ["RESUME_AGENT_DEMO_KEY"] = "sk-ant-test"


def test_budget_ceiling():
    section("Daily budget")
    led = fresh_ledger(budget=1.00)
    # $0.30 a run against a $1 budget: three fit, the fourth does not.
    ok(led.reserve("ip1", "run") == "", "first run inside budget")
    ok(led.reserve("ip2", "run") == "", "second run inside budget")
    ok(led.reserve("ip3", "run") == "", "third run inside budget")
    ok("budget" in led.reserve("ip4", "run"), "fourth run refused: budget gone")

    # Settling below the estimate gives the difference back to the day.
    led.settle("run", 0.05)
    led.settle("run", 0.05)
    led.settle("run", 0.05)
    ok(abs(led._state["spent"] - 0.15) < 1e-9, "settles at what was really spent")
    ok(led._state["reserved"] == 0, "no reservation left holding budget")
    ok(led.reserve("ip5", "run") == "",
       "runs that cost less than estimated free the budget back up")


def test_reservation_beats_the_race():
    section("Concurrency")
    led = fresh_ledger(budget=1.00)
    granted = []
    barrier = threading.Barrier(8)

    def go(n):
        barrier.wait()
        if led.reserve(f"ip{n}", "run") == "":
            granted.append(n)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Without reserve-before-work, all eight would read the same healthy
    # balance and every one of them would be admitted.
    ok(len(granted) == 3,
       f"eight simultaneous requests, three admitted (got {len(granted)})")
    ok(led.committed() <= 1.00 + 1e-9, "committed spend never exceeds the budget")


def test_per_ip_cap():
    section("Per-IP cap")
    trial.FREE_ACTIONS_PER_IP = 2
    led = fresh_ledger(budget=100.00)
    ok(led.reserve("same", "run") == "", "first from this address")
    ok(led.reserve("same", "run") == "", "second from this address")
    refusal = led.reserve("same", "run")
    ok("free trial" in refusal.lower(), "third from the same address refused")
    ok(led.reserve("other", "run") == "",
       "a different address is unaffected — the cap is per visitor, not global")
    trial.FREE_ACTIONS_PER_IP = 8


def test_new_session_does_not_reset_the_ip():
    section("Opening a new session to get around the quota")
    trial.FREE_ACTIONS_PER_IP = 2
    trial.QUOTA["run"] = 3
    led = fresh_ledger(budget=100.00)
    trial.LEDGER, saved = led, trial.LEDGER
    try:
        # Someone clears their cookie and starts over. The session counter
        # resets -- that is what a new session means -- and the address does
        # not, which is the whole point of the second limit.
        for _ in range(2):
            s = FakeSession()
            ok(trial.take(s, "1.2.3.4", "run") == "", "run allowed")
        s = FakeSession()
        ok(trial.take(s, "1.2.3.4", "run") != "",
           "a brand new session from the same address is still refused")
        ok(s.trial_used.get("run", 0) == 0,
           "a refused take does not spend the session's own allowance")
    finally:
        trial.LEDGER = saved
        trial.FREE_ACTIONS_PER_IP = 8


def test_restart_does_not_reset_the_day():
    section("Restart")
    tmp = Path(tempfile.mkdtemp()) / "trial.json"
    trial.DAILY_BUDGET = 1.00
    led = trial.Ledger(path=tmp)
    led.reserve("ip1", "run")
    led.settle("run", 0.30)
    led.reserve("ip2", "run")
    led.settle("run", 0.30)

    # Fly stops an idle machine and starts it again on the next request. If
    # the ledger lived only in memory, going quiet would reset the day's
    # budget -- which is to say, anyone could reset it by waiting.
    again = trial.Ledger(path=tmp)
    ok(abs(again._state["spent"] - 0.60) < 1e-9, "spend survives a restart")
    ok(again._state["ips"].get("ip1") == 1, "per-address counts survive a restart")

    # Reservations must not: whatever they were holding either settled before
    # the stop or died with the process.
    led.reserve("ip3", "run")
    third = trial.Ledger(path=tmp)
    ok(third._state["reserved"] == 0, "in-flight reservations are not carried over")


def test_day_rolls_over():
    section("Day rollover")
    tmp = Path(tempfile.mkdtemp()) / "trial.json"
    trial.DAILY_BUDGET = 1.00
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps({"day": "2020-01-01", "spent": 99.0, "reserved": 0.0,
                               "actions": 500, "ips": {"ip1": 50}}))
    led = trial.Ledger(path=tmp)
    ok(led._state["spent"] == 0, "yesterday's spend does not count against today")
    ok(led._state["ips"] == {}, "yesterday's addresses start over")
    ok(led.reserve("ip1", "run") == "", "and today's first run is allowed")


def test_refund_is_only_for_free_failures():
    section("Refunds")
    trial.QUOTA["run"] = 3
    led = fresh_ledger(budget=100.00)
    trial.LEDGER, saved = led, trial.LEDGER
    try:
        s = FakeSession()
        trial.take(s, "9.9.9.9", "run")
        ok(s.trial_used["run"] == 1, "taking a credit spends one")
        trial.give_back(s, "9.9.9.9", "run")
        ok(s.trial_used["run"] == 0, "a refund returns the session's credit")
        ok(led._state["ips"].get("9.9.9.9", 0) == 0,
           "and the address's count with it")
        ok(led.committed() == 0, "and releases the money it was holding")
    finally:
        trial.LEDGER = saved


def test_failed_run_after_a_model_call_still_costs():
    section("A run that fails after spending")
    led = fresh_ledger(budget=1.00)
    for _ in range(3):
        led.reserve("ip", "run")
        led.settle("run", None)          # what app.py does when job.billable
    ok(abs(led._state["spent"] - 0.90) < 1e-9,
       "a failed-but-billable run is charged the estimate")
    ok("budget" in led.reserve("ip2", "run"),
       "a loop of failing runs exhausts the budget rather than running forever")


def test_unwritable_state_dir_does_not_crash():
    section("Unwritable state")
    trial.DAILY_BUDGET = 1.00
    led = trial.Ledger(path=Path("/nonexistent-root-dir/nope/trial.json"))
    ok(led.reserve("ip", "run") == "",
       "a state dir it cannot write to must not take the site down")
    ok(led.committed() > 0, "limits still hold in memory for this process")


def test_ip_hash_is_not_the_address():
    section("Addresses")
    h = trial.hash_ip("203.0.113.7")
    ok("203.0.113.7" not in h, "the stored value is not the address")
    ok(h == trial.hash_ip("203.0.113.7"), "same address, same value")
    ok(h != trial.hash_ip("203.0.113.8"), "different address, different value")
    ok(len(h) == 16, "truncated")



# ---------------------------------------------------------------------------
# The routes that spend the money
# ---------------------------------------------------------------------------

def test_no_accidental_fallback():
    section("No path to the demo key by accident")
    import sessions
    os.environ["RESUME_AGENT_DEMO_KEY"] = "sk-ant-fake"

    s = sessions.Session(id="x")
    ok(s.key == "", "a session that never opted in has no key at all")
    ok(not s.ready, "and is not ready to run")
    s.on_trial = True
    ok(s.key == "sk-ant-fake", "opting into the trial reaches the demo key")
    s.api_key = "sk-ant-mine"
    ok(s.key == "sk-ant-mine", "their own key wins over the demo key")
    ok(s.own_key, "and is recognised as their own")

    # The SDK reads ANTHROPIC_API_KEY by itself. Hosted, that variable is the
    # one way a call could reach the model with nobody having paid for it.
    try:
        import app as web
    except ImportError:
        print("  skipped the startup check — flask not installed")
        return
    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-ambient"
    try:
        web.refuse_ambient_key()
        ok(False, "a hosted deployment refuses to start with ANTHROPIC_API_KEY set")
    except RuntimeError as e:
        ok("spend it silently" in str(e),
           "a hosted deployment refuses to start with ANTHROPIC_API_KEY set")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if saved:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_routes():
    section("Web flow")
    try:
        import app as web
    except ImportError as e:
        print(f"  skipped — {e}. pip install -r requirements.txt to run these.")
        return

    web.app.config["TESTING"] = True
    trial.QUOTA["run"] = 3
    trial.FREE_ACTIONS_PER_IP = 8
    led = fresh_ledger(budget=100.00)
    trial.LEDGER = led

    # Nothing here may touch the network or the model.
    calls = {"n": 0, "keys": []}

    class FakeResult:
        company, cost = "Acme", 0.11
        html, pdf, issues = "<html></html>", None, []

    def fake_run(**kw):
        calls["n"] += 1
        calls["keys"].append(getattr(kw["client"], "api_key", None))
        return FakeResult()

    web.pipeline.run = fake_run
    web.sessions.safe_url = lambda url: ""

    client = web.app.test_client()

    page = client.get("/start")
    ok(b"Free trial" in page.data, "the start page offers the trial")

    r = client.post("/start", data={"mode": "trial"})
    ok(r.status_code == 302, "choosing the trial starts a session")

    live = list(web.sessions.STORE._sessions.values())
    ok(len(live) == 1, "one session exists")
    s = live[0]
    ok(s.on_trial and not s.own_key, "session is on the trial with no key of its own")
    ok(s.key == "sk-ant-fake", "and draws on the demo key")

    # Skip the upload: this is about the run gate, not ingestion.
    s.resume = {"contact": {"name": "Test"}, "experience": [], "headlines": {},
                "skills": {}}

    for n in (1, 2, 3):
        r = client.post("/run", data={"url": "https://example.com/job"})
        ok(r.status_code == 302, f"free run {n} starts")

    r = client.post("/run", data={"url": "https://example.com/job"})
    ok(r.status_code == 200, "the fourth run does not start")
    ok(b"free runs" in r.data, "and says why")
    ok(b"Add your own Anthropic" in r.data, "and offers the way forward")

    # The gate must come before the work, not after it.
    import time
    time.sleep(0.3)
    ok(calls["n"] == 3, f"the model was reached three times, not four (got {calls['n']})")
    ok(set(calls["keys"]) == {"sk-ant-fake"}, "every run used the demo key")

    # Adding a key mid-session stops drawing on the host's account.
    r = client.post("/start", data={"api_key": "sk-ant-mine"})
    ok(r.status_code == 302, "a key can be added mid-session")
    ok(not s.on_trial and s.key == "sk-ant-mine",
       "their key takes over from the trial")

    spent_before = led.committed()
    r = client.post("/run", data={"url": "https://example.com/job"})
    ok(r.status_code == 302, "and the run that was refused a moment ago now works")
    time.sleep(0.3)
    ok(calls["keys"][-1] == "sk-ant-mine", "on their key")
    ok(abs(led.committed() - spent_before) < 1e-9,
       "a run on their own key does not touch the trial ledger")

    r = client.get("/admin/trial")
    ok(r.status_code == 404, "the admin view is off with no token set")
    os.environ["TRIAL_ADMIN_TOKEN"] = "letmein"
    ok(client.get("/admin/trial?token=wrong").status_code == 404,
       "and refuses a wrong token")
    r = client.get("/admin/trial?token=letmein")
    ok(r.status_code == 200 and "spent" in r.get_json(), "and reports with the right one")
    del os.environ["TRIAL_ADMIN_TOKEN"]

    body = client.get("/healthz").get_json()
    ok(body.get("trial") is True, "healthz says a trial is on offer")
    ok("spent" not in body and "budget" not in body,
       "but does not publish how much of it is left")


def main():
    os.environ["RESUME_AGENT_DEMO_KEY"] = "sk-ant-test"
    for fn in (test_session_quota, test_budget_ceiling,
               test_reservation_beats_the_race, test_per_ip_cap,
               test_new_session_does_not_reset_the_ip,
               test_restart_does_not_reset_the_day, test_day_rolls_over,
               test_refund_is_only_for_free_failures,
               test_failed_run_after_a_model_call_still_costs,
               test_unwritable_state_dir_does_not_crash,
               test_ip_hash_is_not_the_address,
               test_no_accidental_fallback, test_routes):
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
