#!/usr/bin/env python3
"""Local web UI for the resume agent.

Paste a job posting URL, get the tailored resume plus everything the pipeline
knows about the role -- the per-requirement fit evidence, the QA findings, the
tailoring decisions, the ATS gaps. A spreadsheet row holds about a tenth of
that; the rest was being generated and thrown away.

Run it:  python app.py   ->  http://127.0.0.1:5000

This is the single-user local build. It trusts whoever can reach the port,
reads the API key from .env, and keeps jobs in memory. The hosted build adds
per-request keys, a real queue, and storage -- see README.
"""

import json
import re
import secrets
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   Response, url_for)

import ingest
import pipeline
import qa
import tailor_resume as tr

app = Flask(__name__)

# One run takes 40-90s, which is far too long to hold a request open, so runs
# happen on a worker thread and the page polls. In memory is honest for a
# single-user local tool: nothing here is worth persisting, and a restart
# losing an in-flight run costs one re-run.
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50


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


# Raw exception text is accurate and useless. These are the failures a real
# posting URL actually produces, in the words of someone who wants the resume.
ERROR_HINTS = (
    ("nodename nor servname", "That address doesn't resolve — check the URL."),
    ("Name or service not known", "That address doesn't resolve — check the URL."),
    ("timed out", "The posting took too long to load. Paste the text instead."),
    ("403", "The board refused the request — it blocks automated fetches. "
            "Paste the posting text instead."),
    ("404", "That posting is gone. It may have been filled or renamed."),
    ("credit balance", "Your Anthropic account is out of credit."),
    ("authentication", "The API key was rejected. Check ANTHROPIC_API_KEY in .env."),
    ("JSONDecode", "The model returned something unparseable. Try again — this is "
                   "usually transient."),
)


def friendly_error(e: Exception) -> str:
    raw = f"{type(e).__name__}: {e}"
    for needle, hint in ERROR_HINTS:
        if needle.lower() in raw.lower():
            return hint
    return raw


def _resume() -> dict:
    return json.loads(tr.MASTER_RESUME.read_text())


def _run_job(job: Job, url: str, text: str):
    try:
        job.status = "running"
        client = tr.make_client()
        job.result = pipeline.run(
            resume=_resume(), client=client, url=url, text=text,
            company=job.company,
            progress=lambda stage, message, result: job.note(stage, message),
        )
        job.company = job.result.company or job.company
        job.status = "done"
    except Exception as e:
        job.status = "error"
        job.error = friendly_error(e)
        # Printed, not shown: a traceback can carry a file path or a key
        # fragment, and the page is the wrong place for either.
        traceback.print_exc()


@app.get("/")
def index():
    resume = _resume()
    return render_template("index.html",
                           name=resume["contact"]["name"],
                           roles=[r["company"] for r in resume["experience"]],
                           tracks=sorted(resume["headlines"]),
                           using_sample=tr.MASTER_RESUME.name.endswith("example.json"))


@app.post("/run")
def start():
    url = (request.form.get("url") or "").strip()
    text = (request.form.get("text") or "").strip()
    if not url and not text:
        return redirect(url_for("index"))
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    job = Job(id=secrets.token_urlsafe(8), url=url,
              company=(request.form.get("company") or "").strip())
    with JOBS_LOCK:
        JOBS[job.id] = job
        for old in list(JOBS)[:-MAX_JOBS]:
            JOBS.pop(old, None)
    threading.Thread(target=_run_job, args=(job, url, text), daemon=True).start()
    return redirect(url_for("job_page", job_id=job.id))


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/job/<job_id>")
def job_page(job_id):
    job = _job(job_id)
    if job.status == "error":
        # Rendered server-side rather than left to the poll: someone reloading a
        # failed job should not be told it is still working.
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
    name = re.sub(r"[^A-Za-z0-9]+", "_",
                  f"{_resume()['contact']['name']} Resume {job.company}").strip("_")
    return Response(job.result.pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})


# ---------------------------------------------------------------------------
# Ingestion: upload a resume, review what was read out of it, commit it
# ---------------------------------------------------------------------------

DRAFTS: dict[str, dict] = {}
MAX_UPLOAD = 8 * 1024 * 1024


@app.get("/resume")
def resume_page():
    resume = _resume()
    return render_template("resume.html", resume=resume,
                           using_sample=tr.MASTER_RESUME.name.endswith("example.json"),
                           bullets=sum(len(qa._flatten(r.get("bullets", {})))
                                       for r in resume.get("experience", [])))


@app.post("/resume/upload")
def resume_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return redirect(url_for("resume_page"))
    data = upload.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return render_template("resume.html", resume=_resume(), bullets=0,
                               error="That file is over 8MB. A resume should be "
                                     "a fraction of that — check it is the right file.")

    draft_id = secrets.token_urlsafe(8)
    job = Job(id=draft_id)
    JOBS[draft_id] = job

    # Read off the request here, not inside the thread. The request context is
    # gone by the time the worker runs, and touching it there raises rather
    # than returning a default.
    filename = upload.filename
    track = (request.form.get("track") or "").strip() or "general"

    def work():
        try:
            job.status = "running"
            job.note("analyze", "Reading the document")
            draft, document, issues = ingest.ingest(
                tr.make_client(), data, filename, track=track)
            DRAFTS[draft_id] = {"draft": draft, "document": document,
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
    job = _job(draft_id)
    if job.status == "error":
        return render_template("running.html", job=job, failed=True)
    if draft_id not in DRAFTS:
        return render_template("running.html", job=job, ingesting=True)
    d = DRAFTS[draft_id]
    return render_template("review.html", draft_id=draft_id, d=d,
                           draft=d["draft"], issues=d["issues"],
                           errors=[i for i in d["issues"] if i.level == qa.ERROR],
                           warns=[i for i in d["issues"] if i.level == qa.WARN])


@app.post("/resume/review/<draft_id>")
def resume_save(draft_id):
    if draft_id not in DRAFTS:
        abort(404)
    draft = DRAFTS[draft_id]["draft"]
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

    target = tr._HERE / "master_resume.json"
    if target.exists():
        backup = tr._HERE / "master_resume.backup.json"
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(json.dumps(edited, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    DRAFTS.pop(draft_id, None)
    return redirect(url_for("resume_page"))


if __name__ == "__main__":
    tr.load_env()
    print("\n  resume-agent  →  http://127.0.0.1:5000\n")
    # threaded so a 90-second run does not block the page polling for it.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
