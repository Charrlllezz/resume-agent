#!/usr/bin/env python3
"""Web UI for the resume agent.

Paste a job posting URL, get the tailored resume plus everything the pipeline
knows about the role -- the per-requirement fit evidence, the QA findings, the
tailoring decisions, the ATS gaps. A spreadsheet row holds about a tenth of
that; the rest was being generated and thrown away.

  python app.py                          local, one user, key from .env
  RESUME_AGENT_HOSTED=1 gunicorn ...     hosted, each visitor brings their own

One app rather than two, because two would drift. Local mode is a session
seeded from disk at startup; hosted mode makes every visitor supply their own
key and resume. See sessions.py for what is and is not kept.
"""

import json
import os
import re
import secrets
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   Response, session, url_for)

import ingest
import pipeline
import qa
import sessions
import tailor_resume as tr
import trial
import usage

app = Flask(__name__)

# Stable across restarts in production, or every session breaks on deploy.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure whenever hosted, because the cookie is the session. That also
    # means a browser will not send it back over plain HTTP, so exercising
    # hosted mode on localhost needs this switched off deliberately. It is
    # never right in production and is named to say so.
    SESSION_COOKIE_SECURE=(not sessions.single_user()
                           and os.environ.get("RESUME_AGENT_INSECURE_COOKIES") != "1"),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)

MAX_JOBS_KEPT = 25


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Raw exception text is accurate and useless. These are the failures a real
# posting URL actually produces, in the words of someone who wants the resume.
ERROR_HINTS = (
    ("nodename nor servname", "That address doesn't resolve — check the URL."),
    ("Name or service not known", "That address doesn't resolve — check the URL."),
    ("timed out", "The posting took too long to load. Paste the text instead."),
    ("403", "The board refused the request — it blocks automated fetches. "
            "Paste the posting text instead."),
    ("404", "That posting is gone. It may have been filled or renamed."),
    ("credit balance", "That Anthropic account is out of credit."),
    ("authentication", "The API key was rejected. Check that it's a working "
                       "Anthropic key."),
    ("invalid_api_key", "The API key was rejected. Check that it's a working "
                        "Anthropic key."),
    ("JSONDecode", "The model returned something unparseable. Try again — this "
                   "is usually transient."),
)


def friendly_error(e: Exception) -> str:
    # A ValueError raised in this codebase is already a sentence written for
    # the person reading it. Prefixing it with the class name is noise.
    if isinstance(e, ValueError) and str(e):
        return sessions.redact(str(e))
    raw = f"{type(e).__name__}: {e}"
    for needle, hint in ERROR_HINTS:
        if needle.lower() in raw.lower():
            return hint
    # An SDK may quote the request it failed on, so never render it unredacted.
    return sessions.redact(raw)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.before_request
def attach_session():
    g.session = sessions.STORE.get(session.get("sid"))


def current() -> sessions.Session:
    if not getattr(g, "session", None):
        abort(401)
    return g.session


def new_session() -> sessions.Session:
    s = sessions.STORE.create()
    session["sid"] = s.id
    session.permanent = False
    return s


def bootstrap_local():
    """The local build's one implicit user: key from .env, resume from disk."""
    tr.load_env()
    s = sessions.STORE.create()
    s.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    s.resume = json.loads(tr.MASTER_RESUME.read_text(encoding="utf-8"))
    return s


def refuse_ambient_key():
    """Hosted, ANTHROPIC_API_KEY in the environment is a standing invitation.

    The SDK reads that variable by itself. A hosted process holding it would
    bill the host for any call that reached the SDK without an explicit key --
    no trial accounting, no session, no limit, and no error to notice. The
    trial key is deliberately named something the SDK will never pick up, and
    this makes the mistake loud instead of expensive.
    """
    if not sessions.single_user() and os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set in a hosted deployment. The SDK falls "
            "back to it, so any code path that forgot to pass a key would "
            "spend it silently, for anyone. Unset it. To offer a free trial, "
            "use RESUME_AGENT_DEMO_KEY, which the SDK cannot pick up on its "
            "own and which trial.py meters.")


refuse_ambient_key()
LOCAL_SESSION = bootstrap_local() if sessions.single_user() else None


@app.before_request
def use_local_session():
    if LOCAL_SESSION is not None:
        g.session = LOCAL_SESSION
        g.session.touch()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    url: str = ""
    company: str = ""
    status: str = "queued"          # queued | running | done | error
    stage: str = ""
    events: list = field(default_factory=list)
    result: object = None
    error: str = ""
    # True once the run has reached the model. A trial credit is only kept for
    # work that actually cost the host something -- a URL that does not
    # resolve fails before the first call, and charging a free run for a typo
    # teaches people the trial is broken.
    billable: bool = False
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def note(self, stage: str, message: str):
        # 'warn' is an aside, not a step: letting it overwrite the stage would
        # lose track of how far the run actually got.
        if stage != "warn":
            self.stage = stage
        if stage == "analyze":
            self.billable = True
        if message:
            self.events.append({"stage": stage, "message": message})


def remember(s: sessions.Session, job: Job):
    s.jobs[job.id] = job
    for old in list(s.jobs)[:-MAX_JOBS_KEPT]:
        s.jobs.pop(old, None)


def _job(job_id: str) -> Job:
    """Only ever this session's. Jobs are per-session so a guessed id from
    somebody else's run cannot be read back."""
    job = current().jobs.get(job_id)
    if not job:
        abort(404)
    return job


def _run_job(s: sessions.Session, job: Job, url: str, text: str, ip: str = ""):
    """Run one job. `ip` is non-empty only when a trial credit is being held."""
    try:
        job.status = "queued"
        # Waits for a slot rather than launching a browser regardless. A few
        # simultaneous Chromiums will exhaust a small machine. Bounded rather
        # than a bare `with sessions.RUN_SLOTS:` -- every slot can end up held
        # by a run that is stuck rather than merely busy, and a queued run
        # should fail cleanly instead of waiting behind it forever.
        if not sessions.RUN_SLOTS.acquire(timeout=sessions.SLOT_WAIT_SECONDS):
            raise ValueError("The server is at capacity right now — please "
                             "try again in a few minutes.")
        try:
            job.status = "running"
            job.result = pipeline.run(
                resume=s.resume, client=tr.make_client(s.key),
                url=url, text=text, company=job.company,
                progress=lambda stage, message, result: job.note(stage, message),
            )
        finally:
            sessions.RUN_SLOTS.release()
        job.company = job.result.company or job.company
        job.status = "done"
        if ip:
            trial.LEDGER.settle("run", job.result.cost)
    except Exception as e:
        job.status = "error"
        job.error = friendly_error(e)
        traceback.print_exc()
        if ip:
            # The tokens are gone with the exception, so a run that reached the
            # model is charged the estimate. Anything else is given back.
            if job.billable:
                trial.LEDGER.settle("run", None)
            else:
                trial.give_back(s, ip, "run")


# ---------------------------------------------------------------------------
# Getting started (hosted only)
# ---------------------------------------------------------------------------

@app.get("/start")
def start_page():
    if sessions.single_user():
        return redirect(url_for("index"))
    return render_template("start.html", s=getattr(g, "session", None),
                           trial_on=trial.enabled(),
                           free_runs=trial.FREE_RUNS_PER_SESSION,
                           error=request.args.get("error", ""))


@app.post("/start")
def start_submit():
    if sessions.single_user():
        return redirect(url_for("index"))
    s = getattr(g, "session", None) or new_session()

    def again(message):
        return render_template("start.html", s=s, error=message,
                               trial_on=trial.enabled(),
                               free_runs=trial.FREE_RUNS_PER_SESSION)

    key = (request.form.get("api_key") or "").strip()
    if key:
        if not key.startswith("sk-"):
            return again("That doesn't look like an Anthropic API key — they "
                         "begin with sk-.")
        s.api_key = key
        # Their key now pays for everything, so stop drawing on the trial --
        # including for someone who started free and is topping up mid-session.
        s.on_trial = False
    elif request.form.get("mode") == "trial":
        if not trial.enabled():
            return again("The free trial isn't available right now. An API key "
                         "will still work.")
        if trial.LEDGER.remaining() <= 0:
            return again("The free trial has used up today's budget. An API key "
                         "will still work — it resets at midnight UTC.")
        s.on_trial = True

    if not s.key:
        return again("An API key is needed to run.")
    return redirect(url_for("resume_page"))


@app.post("/signout")
def signout():
    sid = session.pop("sid", None)
    if sid:
        sessions.STORE.drop(sid)
    return redirect(url_for("start_page"))


def needs_setup():
    """Where to send someone who cannot run yet, or None if they can."""
    if sessions.single_user():
        return None
    s = getattr(g, "session", None)
    if not s or not s.key:
        return redirect(url_for("start_page"))
    if not s.resume:
        return redirect(url_for("resume_page"))
    return None


# ---------------------------------------------------------------------------
# Tailoring
# ---------------------------------------------------------------------------

def _index(s: sessions.Session, error: str = "", offer_key: bool = False):
    """Re-render the form with a message on it.

    Every refusal path lands here. Assembling this at each call site is how one
    of them ended up passing roles=[] and tracks=[], which emptied the page the
    person was being sent back to.
    """
    return render_template(
        "index.html",
        name=(s.resume or {}).get("contact", {}).get("name", ""),
        roles=[r["company"] for r in (s.resume or {}).get("experience", [])],
        tracks=tr.tracks_for(s.resume) if s.resume else [],
        hosted=not sessions.single_user(),
        on_trial=s.on_trial and not s.own_key,
        runs_left=trial.left(s, "run") if s.on_trial and not s.own_key else None,
        offer_key=offer_key,
        using_sample=sessions.single_user()
        and tr.MASTER_RESUME.name.endswith("example.json"),
        error=error)


@app.get("/")
def index():
    detour = needs_setup()
    if detour:
        return detour
    return _index(current())


@app.post("/run")
def start():
    detour = needs_setup()
    if detour:
        return detour
    s = current()
    if s.runs_started >= sessions.MAX_RUNS_PER_SESSION:
        return _index(s, f"That's {sessions.MAX_RUNS_PER_SESSION} runs this "
                         "session, which is the cap. Sign out and back in to "
                         "reset it.")

    url = (request.form.get("url") or "").strip()
    text = (request.form.get("text") or "").strip()
    if not url and not text:
        return redirect(url_for("index"))
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Hosted, this URL comes from a stranger and this process will fetch it and
    # render the result. Locally it is your own machine and pointing it at
    # localhost is a legitimate thing to want.
    if url and not sessions.single_user():
        refusal = sessions.safe_url(url)
        if refusal:
            return _index(s, refusal)

    # Claimed after the URL is known to be worth fetching, so a refused address
    # or an empty form never costs a free run. Nothing below here can fail
    # before the thread starts.
    ip = ""
    if s.on_trial and not s.own_key:
        ip = trial.client_ip(request)
        refusal = trial.take(s, ip, "run")
        if refusal:
            return _index(s, refusal, offer_key=True)

    job = Job(id=secrets.token_urlsafe(8), url=url,
              company=(request.form.get("company") or "").strip())
    remember(s, job)
    s.runs_started += 1
    threading.Thread(target=_run_job, args=(s, job, url, text, ip),
                     daemon=True).start()
    return redirect(url_for("job_page", job_id=job.id))


@app.get("/job/<job_id>")
def job_page(job_id):
    job = _job(job_id)
    if job.status == "error":
        return render_template("running.html", job=job, failed=True)
    if job.status != "done":
        return render_template("running.html", job=job)
    r = job.result
    grouped = {level: [i for i in r.issues if i.level == level]
               for level in (qa.ERROR, qa.WARN, qa.INFO)}
    return render_template("result.html", job=job, r=r, row=r.row(),
                           grouped=grouped, qa_error=qa.ERROR, qa_warn=qa.WARN,
                           assessments=(r.assessment or {}).get("assessments", []))


@app.get("/job/<job_id>.json")
def job_status(job_id):
    job = _job(job_id)
    return jsonify(status=job.status, stage=job.stage, error=job.error,
                   events=job.events)


@app.get("/job/<job_id>/resume.html")
def job_html(job_id):
    job = _job(job_id)
    if not job.result:
        abort(404)
    return Response(job.result.html, mimetype="text/html")


@app.get("/job/<job_id>/resume.pdf")
def job_pdf(job_id):
    job = _job(job_id)
    if not job.result or not job.result.pdf:
        abort(404)
    who = current().resume["contact"].get("name", "Resume")
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"{who} Resume {job.company}").strip("_")
    return Response(job.result.pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})


def _apply_edits(tailored: dict, edits: dict) -> None:
    """Write hand-edited text back into the tailored structure, keyed by the
    same data-path the preview marks each field with.

    Anything malformed, out of range, or blank is skipped rather than raising
    -- a stray or empty field here must never cost someone the rest of an
    edit, and a title cleared by an accidental select-all should not silently
    blank a resume.
    """
    def text(key):
        v = edits.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    v = text("headline")
    if v is not None:
        tailored["headline"] = v

    for i, role in enumerate(tailored.get("experience", [])):
        for field in ("title", "company", "location", "dates"):
            v = text(f"experience.{i}.{field}")
            if v is not None:
                role[field] = v
        for j in range(len(role.get("bullets", []))):
            v = text(f"experience.{i}.bullets.{j}")
            if v is not None:
                role["bullets"][j] = v

    for gi, (_group, items) in enumerate(tailored.get("skills", {}).items()):
        for si in range(len(items)):
            v = text(f"skills.{gi}.{si}")
            if v is not None:
                items[si] = v


@app.post("/job/<job_id>/edit")
def job_edit(job_id):
    """Hand edits, saved and re-checked the same way a generated draft is.

    Free and fast: re-rendering and re-QAing are both local and deterministic,
    and the PDF is a Chromium print rather than a model call, so a save costs
    nothing and touches no API key. The fit verdict is not recomputed -- it
    judges whether the underlying experience satisfies the posting, which a
    wording tweak does not change, and re-running it would spend real money on
    every keystroke's save.
    """
    job = _job(job_id)
    if not job.result:
        abort(404)
    edits = request.get_json(silent=True)
    if not isinstance(edits, dict):
        abort(400)

    _apply_edits(job.result.tailored, edits)
    resume = current().resume
    job.result.html = tr.render_html(job.result.tailored, resume)
    try:
        job.result.pdf = pipeline.LocalRenderer().pdf(job.result.html)
    except Exception:
        pass  # the edit still saved; the PDF just lags until the next one
    job.result.issues = qa.verify(job.result.tailored, resume,
                                  job.result.analysis, job.result.html)
    return jsonify(ok=True, has_errors=job.result.has_errors)


# ---------------------------------------------------------------------------
# Ingestion: upload a resume, review what was read out of it, commit it
# ---------------------------------------------------------------------------

@app.get("/resume")
def resume_page():
    if not sessions.single_user():
        s = getattr(g, "session", None)
        if not s or not s.key:
            return redirect(url_for("start_page"))
    s = current()
    return render_template("resume.html", resume=s.resume,
                           drafts=s.drafts,
                           hosted=not sessions.single_user(),
                           using_sample=sessions.single_user()
                           and tr.MASTER_RESUME.name.endswith("example.json"),
                           bullets=sum(len(qa._flatten(r.get("bullets", {})))
                                       for r in (s.resume or {}).get("experience", [])))


@app.post("/resume/upload")
def resume_upload():
    s = current()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return redirect(url_for("resume_page"))
    data = upload.read()

    # Read off the request here, not inside the thread. The request context is
    # gone by the time the worker runs, and touching it there raises.
    filename = upload.filename
    track = (request.form.get("track") or "").strip() or "general"

    # Checked before anything is charged: a file we cannot read costs nothing
    # to refuse, and refusing it here means an unsupported format never eats a
    # free upload. ingest() reads it again, which is milliseconds.
    try:
        ingest.read_document(data, filename)
    except ValueError as e:
        return render_template("resume.html", resume=s.resume, bullets=0,
                               drafts=s.drafts,
                               hosted=not sessions.single_user(),
                               error=str(e)), 400

    ip = ""
    if s.on_trial and not s.own_key:
        ip = trial.client_ip(request)
        refusal = trial.take(s, ip, "ingest")
        if refusal:
            return render_template("resume.html", resume=s.resume, bullets=0,
                                   drafts=s.drafts,
                                   hosted=not sessions.single_user(),
                                   offer_key=True, error=refusal), 402

    draft_id = secrets.token_urlsafe(8)
    job = Job(id=draft_id)
    remember(s, job)

    def work():
        # Held outside the scope so the failure path can still settle on what
        # was really spent: an ingest that dies after the model call did cost
        # the host money, and the tokens are known either way.
        totals = usage.new()
        # Distinct from job.note("analyze", ...) below, which fires before the
        # slot wait purely to show "Reading the document" -- it cannot signal
        # whether the model was actually reached, so billing needs its own flag.
        started_ingest = False
        try:
            job.status = "running"
            job.note("analyze", "Reading the document")
            # See _run_job: a bare `with sessions.RUN_SLOTS:` waits forever if
            # every slot is held by a run that is stuck rather than busy.
            if not sessions.RUN_SLOTS.acquire(timeout=sessions.SLOT_WAIT_SECONDS):
                raise ValueError("The server is at capacity right now — "
                                 "please try again in a few minutes.")
            started_ingest = True
            try:
                with usage.scope() as acc:
                    totals = acc
                    draft, document, issues = ingest.ingest(
                        tr.make_client(s.key), data, filename, track=track)
            finally:
                sessions.RUN_SLOTS.release()
            s.drafts[draft_id] = {"draft": draft, "document": document,
                                  "issues": issues, "filename": filename}
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = friendly_error(e)
            traceback.print_exc()
        finally:
            if ip:
                # A slot-wait timeout never reached the model -- give the free
                # action back rather than charging someone for a server that
                # was too busy, the same distinction _run_job makes.
                if started_ingest:
                    trial.LEDGER.settle("ingest", usage.cost(totals))
                else:
                    trial.give_back(s, ip, "ingest")

    threading.Thread(target=work, daemon=True).start()
    return redirect(url_for("resume_review", draft_id=draft_id))


@app.get("/resume/review/<draft_id>")
def resume_review(draft_id):
    s = current()
    job = _job(draft_id)
    if job.status == "error":
        return render_template("running.html", job=job, failed=True)
    if draft_id not in s.drafts:
        return render_template("running.html", job=job, ingesting=True)
    d = s.drafts[draft_id]
    return render_template("review.html", draft_id=draft_id, d=d,
                           draft=d["draft"], issues=d["issues"],
                           errors=[i for i in d["issues"] if i.level == qa.ERROR],
                           warns=[i for i in d["issues"] if i.level == qa.WARN])


@app.post("/resume/review/<draft_id>")
def resume_save(draft_id):
    s = current()
    if draft_id not in s.drafts:
        abort(404)
    draft = s.drafts[draft_id]["draft"]
    track = next(iter(draft.get("headlines") or {"general": []}))

    # Rebuilt from the form, not from the draft: what the person reviewed and
    # edited is the file, which is the whole point of this screen. The model's
    # version is only ever a starting point.
    edited = {
        "contact": {k: (request.form.get(f"contact.{k}") or "").strip()
                    for k in ("name", "phone", "email", "linkedin")},
        "headlines": {track: [h.strip() for h in
                              (request.form.get("headlines") or "").splitlines() if h.strip()]},
        "experience": [],
        "skills": draft.get("skills", {}),
        # Their document's look, unless they asked for the house template on
        # the review screen.
        "style": ({} if request.form.get("house_style") == "1"
                  else draft.get("style", {})),
        "education": {k: (request.form.get(f"education.{k}") or "").strip()
                      for k in ("degree", "school", "year")},
        # Certifications, awards, languages -- sections the schema has no field
        # of its own for. They were being dropped entirely, which is a worse
        # failure than any of the ones this screen exists to catch.
        "extras": [],
    }
    for i, extra in enumerate(draft.get("extras") or []):
        items = [x.strip() for x in
                 (request.form.get(f"extra.items.{i}") or "").splitlines() if x.strip()]
        label = (request.form.get(f"extra.label.{i}") or extra.get("label") or "").strip()
        if label and items:
            edited["extras"].append({"label": label, "items": items})
    for i, role in enumerate(draft.get("experience", [])):
        bullets = [b.strip() for b in
                   (request.form.get(f"bullets.{i}") or "").splitlines() if b.strip()]
        if not bullets:
            continue
        edited["experience"].append({
            "company": (request.form.get(f"company.{i}") or "").strip(),
            "location": (request.form.get(f"location.{i}") or "").strip(),
            "dates": (request.form.get(f"dates.{i}") or "").strip(),
            "titles": {track: (request.form.get(f"title.{i}") or "").strip()},
            "bullets": {track: bullets},
            "bullets_expected": role.get("bullets_expected", [3, 5]),
        })

    s.resume = edited
    s.drafts.pop(draft_id, None)

    # Written to disk only for the one local user whose disk it is. Hosted,
    # somebody else's resume stays in their session and is never persisted.
    if sessions.single_user():
        target = tr._HERE / "master_resume.json"
        if target.exists():
            (tr._HERE / "master_resume.backup.json").write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(json.dumps(edited, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    return redirect(url_for("resume_page"))


# ---------------------------------------------------------------------------

@app.context_processor
def template_globals():
    """`hosted` is needed by the layout on every page. Injected once here
    rather than passed through each render_template, where the one that gets
    forgotten shows a sign-out button to a local user with nothing to sign out
    of -- or worse, hides it from someone holding a key."""
    return {"hosted": not sessions.single_user()}


@app.get("/healthz")
def healthz():
    # Deliberately says whether a trial is offered but not how much of it is
    # left. A public countdown to "the budget is nearly gone" is a countdown
    # somebody can plan around.
    return jsonify(ok=True, hosted=not sessions.single_user(),
                   sessions=sessions.STORE.count(), trial=trial.enabled())


@app.get("/admin/trial")
def admin_trial():
    """Today's trial spending. Gated on a token, off unless one is set.

    This is how the host answers 'what is this costing me' without reading
    logs. It is the only place the ledger is exposed, and it exposes counts
    and dollars -- no addresses, no sessions, no resumes.
    """
    want = os.environ.get("TRIAL_ADMIN_TOKEN", "")
    got = request.args.get("token", "")
    if not want or not secrets.compare_digest(want, got):
        abort(404)
    return jsonify(trial.LEDGER.snapshot())


@app.errorhandler(401)
def unauthorized(_):
    # The one thing every 401 here actually means: g.session was never set,
    # which happens when the cookie names a session the store has never heard
    # of -- almost always because the machine went idle and Fly stopped it
    # (see fly.toml). Landing back on /start with no explanation reads as a
    # broken button, not an expired session, so say which one it was.
    return redirect(url_for("start_page", error=(
        "Your session timed out — the server was idle and restarted, which "
        "clears sessions to keep hosting free. Sign back in to continue; "
        "anything you hadn't downloaded yet is gone.")))


@app.errorhandler(413)
def too_large(_):
    return render_template("resume.html", resume=None, bullets=0,
                           hosted=not sessions.single_user(),
                           error="That file is over 8MB. A resume should be a "
                                 "fraction of that — check it's the right file."), 413


if __name__ == "__main__":
    print("\n  resume-agent  →  http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
