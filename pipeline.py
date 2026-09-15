#!/usr/bin/env python3
"""One run of the pipeline: a job posting in, a tailored resume out.

The CLI and the web app both call `run()`. That is the point of this module.
Three separate bugs in this project's history were a fix landing in one script
and not its sibling -- the credit guard, the filename collision, the plural
regex -- and a web app that reimplemented the pipeline would be the fourth.
Progress is reported through a callback so a server can stream it without this
module knowing what a request is.
"""

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import fit
import qa
import tailor_resume as tr
import usage


class LocalRenderer:
    """Chromium on this machine, for both jobs it does.

    Fetching a posting and printing a PDF are the only two things here that
    need a browser, and they are the whole reason this has to run in a
    container rather than on an edge runtime. Keeping them behind one small
    interface means a hosted deployment can swap in a remote browser
    (Cloudflare Browser Rendering) without the engine noticing.
    """

    def fetch(self, url: str) -> str:
        return tr.fetch_url(url)

    def pdf(self, html: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "resume.html"
            out = Path(tmp) / "resume.pdf"
            src.write_text(html, encoding="utf-8")
            tr.generate_scroll_pdf(src, out)
            return out.read_bytes()


@dataclass
class Result:
    """Everything one run produced. The web app renders this directly."""

    company: str = ""
    role_title: str = ""
    posting_url: str = ""
    posting_chars: int = 0
    analysis: dict = field(default_factory=dict)
    assessment: dict | None = None
    tailored: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    html: str = ""
    pdf: bytes | None = None
    usage: dict = field(default_factory=dict)
    cost: float = 0.0

    @property
    def has_errors(self) -> bool:
        return qa.has_errors(self.issues)

    @property
    def verdict(self) -> str:
        return (self.assessment or {}).get("verdict", "")

    @property
    def score(self) -> float:
        return (self.assessment or {}).get("score", 0.0)

    def unmet(self) -> list:
        return [a for a in (self.assessment or {}).get("assessments", [])
                if a.get("status") == "unmet"]

    def missing_keywords(self) -> list:
        """ATS terms the posting screens on that the output never says."""
        low = self.html.lower()
        return [k for k in self.analysis.get("ats_keywords", []) if k.lower() not in low]

    def row(self) -> dict:
        """The tracker row -- the same columns the CLI writes to applications.csv.

        A results page can show far more than this (per-requirement evidence,
        QA findings, the tailoring decisions); these are just the fields that
        survive being flattened into a spreadsheet.
        """
        return {
            "Date": date.today().isoformat(),
            "Company": self.company,
            "Role": self.role_title,
            "Track": self.analysis.get("track", "").upper(),
            "Fit": self.verdict,
            "Score": f"{self.score:.0%}" if self.assessment else "",
            "Status": "Ready to send",
            "Posting": self.posting_url or "pasted text",
        }


def _noop(stage: str, message: str = "", result=None) -> None:
    pass


def _isolated(fn, *args, **kwargs):
    """Run fn under its own usage scope, for calling from a worker thread.

    usage.scope() keys off a ContextVar, and a new thread starts with none of
    the caller's context -- tokens recorded there would land in an accumulator
    nothing ever reads, and a run's reported cost would quietly go missing.
    Returning the thread's own totals lets the caller merge them back into its
    own scope explicitly, after the thread has finished.
    """
    with usage.scope() as totals:
        try:
            value = fn(*args, **kwargs)
        except Exception as e:
            # A call that reached the API and then failed to parse still
            # spent real tokens. Stash what was recorded on the exception
            # itself -- once this scope exits on unwind, the ContextVar no
            # longer points at `totals`, so a caller that only catches the
            # exception has no other way back to it.
            e.usage = dict(totals)
            raise
    return value, totals


def _merge_usage(totals: dict, *others: dict) -> None:
    for other in others:
        for key in totals:
            totals[key] += other.get(key, 0)


def run(*, resume: dict, client, url: str = "", text: str = "", company: str = "",
        progress=None, do_qa: bool = True, want_pdf: bool = True,
        renderer=None) -> Result:
    """Run the pipeline once and return everything it produced.

    Raises on a hard failure (unreachable posting, malformed model output) so a
    caller can report it; a failed *fit* assessment is not fatal, since the
    tailoring is still worth having.
    """
    # progress(stage, message, result) -- the Result is passed as it fills so a
    # consumer can render partial state without this module owning presentation.
    progress = progress or _noop
    renderer = renderer or LocalRenderer()
    result = Result(posting_url=url)

    # Per-run token accounting. Without this scope a server would report one
    # user's cost to another -- see usage_scope in tailor_resume.
    with usage.scope() as totals:
        if url:
            progress("fetch", f"Fetching {url}", result)
            posting = renderer.fetch(url)
        elif text:
            posting = text
        else:
            raise ValueError("run() needs either url or text")

        result.posting_chars = len(posting)
        if len(posting) < 500:
            # Not fatal: some real postings are terse. But it is almost always
            # a login wall or a bot block, and the output will be generic.
            progress("warn", "Posting looks too short — it may be login-walled "
                             "or bot-blocked. Paste the text instead.", result)

        progress("analyze", "Reading the posting", result)
        result.analysis = tr.analyze_job(client, posting, resume)
        result.role_title = result.analysis.get("role_title", "")
        result.company = company or result.analysis.get("company", "")

        # Fit and tailoring each need only result.analysis, not each other's
        # output, so they run as two concurrent model calls instead of two
        # sequential ones -- one of the three calls a run makes is effectively
        # free in wall-clock time.
        progress("fit", "Judging fit against your resume", result)
        progress("tailor", "Selecting and ordering bullets", result)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fit_future = pool.submit(
                _isolated, fit.assess, client, result.analysis, resume, tr.MODEL)
            tailor_future = pool.submit(
                _isolated, tr.tailor_resume, client, resume, result.analysis, posting)

            try:
                result.assessment, fit_usage = fit_future.result()
            except Exception as e:
                # The tailoring is independent of this and still worth producing.
                progress("warn", f"Fit assessment failed ({type(e).__name__}: {e})", result)
                fit_usage = getattr(e, "usage", None) or usage.new()

            result.tailored, tailor_usage = tailor_future.result()

        _merge_usage(totals, fit_usage, tailor_usage)

        progress("render", "Rendering", result)
        result.html = tr.render_html(result.tailored, resume)

        if want_pdf:
            progress("pdf", "Printing PDF", result)
            result.pdf = renderer.pdf(result.html)

        if do_qa:
            progress("qa", "Checking every claim against your master resume", result)
            result.issues = qa.verify(result.tailored, resume, result.analysis, result.html)

        result.usage = dict(totals)
        result.cost = usage.cost(totals)

    progress("done", "", result)
    return result


def in_main_thread() -> bool:
    """signal.alarm only works on the main thread.

    tailor_resume.deadline() is the CLI's hard wall-clock bound, and calling it
    from a worker thread raises ValueError rather than timing out. A server
    relies on the client's own per-call timeout instead (120s, one retry),
    which bounds a run without touching signals.
    """
    return threading.current_thread() is threading.main_thread()
