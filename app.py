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


if __name__ == "__main__":
    tr.load_env()
    print("\n  resume-agent  →  http://127.0.0.1:5000\n")
    # threaded so a 90-second run does not block the page polling for it.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
