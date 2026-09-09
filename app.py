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
    s.resume = json.loads(tr.MASTER_RESUME.read_text())
    return s


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
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def note(self, stage: str, message: str):
        self.stage = stage
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


def _run_job(s: sessions.Session, job: Job, url: str, text: str):
    try:
        job.status = "queued"
        # Waits for a slot rather than launching a browser regardless. A few
        # simultaneous Chromiums will exhaust a small machine.
        with sessions.RUN_SLOTS:
            job.status = "running"
            job.result = pipeline.run(
                resume=s.resume, client=tr.make_client(s.api_key),
                url=url, text=text, company=job.company,
                progress=lambda stage, message, result: job.note(stage, message),
            )
        job.company = job.result.company or job.company
        job.status = "done"
    except Exception as e:
        job.status = "error"
        job.error = friendly_error(e)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Getting started (hosted only)
# ---------------------------------------------------------------------------

@app.get("/start")
def start_page():
    if sessions.single_user():
        return redirect(url_for("index"))
    return render_template("start.html", s=getattr(g, "session", None))


@app.post("/start")
def start_submit():
    if sessions.single_user():
        return redirect(url_for("index"))
    s = getattr(g, "session", None) or new_session()
    key = (request.form.get("api_key") or "").strip()
    if key:
        if not key.startswith("sk-"):
            return render_template("start.html", s=s,
                                   error="That doesn't look like an Anthropic API "
                                         "key — they begin with sk-.")
        s.api_key = key
    if not s.api_key:
        return render_template("start.html", s=s, error="An API key is needed to run.")
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
    if not s or not s.api_key:
        return redirect(url_for("start_page"))
    if not s.resume:
        return redirect(url_for("resume_page"))
    return None


# ---------------------------------------------------------------------------
# Tailoring
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    detour = needs_setup()
    if detour:
        return detour
    s = current()
    return render_template("index.html",
                           name=s.resume["contact"].get("name", ""),
                           roles=[r["company"] for r in s.resume["experience"]],
                           tracks=tr.tracks_for(s.resume),
                           hosted=not sessions.single_user(),
                           using_sample=sessions.single_user()
                           and tr.MASTER_RESUME.name.endswith("example.json"))


@app.post("/run")
def start():
    detour = needs_setup()
    if detour:
        return detour
    s = current()
    if s.runs_started >= sessions.MAX_RUNS_PER_SESSION:
        return render_template("index.html", name=s.resume["contact"].get("name", ""),
                               roles=[], tracks=[], hosted=not sessions.single_user(),
                               error=f"That's {sessions.MAX_RUNS_PER_SESSION} runs this "
                                     "session, which is the cap. Sign out and back in "
                                     "to reset it.")

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
            return render_template(
                "index.html", name=s.resume["contact"].get("name", ""),
                roles=[r["company"] for r in s.resume["experience"]],
                tracks=tr.tracks_for(s.resume), error=refusal)

    job = Job(id=secrets.token_urlsafe(8), url=url,
              company=(request.form.get("company") or "").strip())
    remember(s, job)
    s.runs_started += 1
    threading.Thread(target=_run_job, args=(s, job, url, text), daemon=True).start()
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


# ---------------------------------------------------------------------------
# Ingestion: upload a resume, review what was read out of it, commit it
# ---------------------------------------------------------------------------

@app.get("/resume")
def resume_page():
    if not sessions.single_user():
        s = getattr(g, "session", None)
        if not s or not s.api_key:
            return redirect(url_for("start_page"))
    s = current()
    return render_template("resume.html", resume=s.resume,
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

    draft_id = secrets.token_urlsafe(8)
    job = Job(id=draft_id)
    remember(s, job)

    def work():
        try:
            job.status = "running"
            job.note("analyze", "Reading the document")
            with sessions.RUN_SLOTS:
                draft, document, issues = ingest.ingest(
                    tr.make_client(s.api_key), data, filename, track=track)
            s.drafts[draft_id] = {"draft": draft, "document": document,
                                  "issues": issues, "filename": filename}
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = friendly_error(e)
            traceback.print_exc()

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
    }
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
    return jsonify(ok=True, hosted=not sessions.single_user(),
                   sessions=sessions.STORE.count())


@app.errorhandler(401)
def unauthorized(_):
    return redirect(url_for("start_page"))


@app.errorhandler(413)
def too_large(_):
    return render_template("resume.html", resume=None, bullets=0,
                           hosted=not sessions.single_user(),
                           error="That file is over 8MB. A resume should be a "
                                 "fraction of that — check it's the right file."), 413


if __name__ == "__main__":
    print("\n  resume-agent  →  http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
