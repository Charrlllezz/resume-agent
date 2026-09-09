#!/usr/bin/env python3
"""
Generate a tailored resume for every role in a bucketed list.

rank.py decides which roles are worth the effort; this produces the actual
documents. One subprocess per role, so a role that fails or wedges cannot take
the batch down with it, and each run still logs to applications.csv on its own.

Files already on disk are skipped, so an interrupted batch resumes for free.

Usage:
  python generate.py ranked.tsv                 # the apply bucket
  python generate.py ranked.tsv --buckets apply,prep
  python generate.py ranked.tsv --dry-run       # what it would do, and cost
"""

import argparse
import re
import os
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path

COST_PER_ROLE = 0.14  # measured: analyze + tailor, with the resume cached

# Stop the batch rather than fail the same way for every remaining role. This
# guard exists in rank.py and was not carried here; a run then burned through
# 28 roles after the balance hit zero, each one launching a browser and
# fetching a posting before the API refused it.
BROKE = ("credit balance is too low", "insufficient")


def resume_prefix() -> str:
    """`Firstname_Lastname_Resume`, taken from the master resume.

    The filename is the first thing a recruiter reads, so it carries your name
    rather than the role slug the pipeline uses internally.
    """
    import json
    import tailor_resume as tr
    name = json.loads(tr.MASTER_RESUME.read_text())["contact"]["name"]
    return f"{clean(name)}_Resume"

# One folder holding only the things you send. The repo root also holds source,
# logs, TSVs and caches, which is a poor place to hunt for an attachment.
OUT_DIR = Path("resumes")


def clean(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", text)).strip("_")


def name_for(company: str, title: str, ambiguous: bool) -> str:
    """The filename a recruiter sees on the attachment.

    Company alone reads best, but a company with six open roles you want shares
    that name across all six: naming by it silently overwrites five resumes,
    which is a bug this hit for real before the check existed. The role is
    appended only where a company has more than one open role.
    """
    base = f"{OUT_DIR}/{resume_prefix()}_{clean(company)}"
    return f"{base}_{clean(title)[:40].strip('_')}" if ambiguous else base


def slug_for(company: str, title: str = "") -> str:
    """Filename slug for one ROLE, not one company.

    When one company has six roles in the same bucket, naming by company alone
    gives all six the same file: five silently overwritten, and the skip check
    then reports them as already generated. The title has to be part of the
    name.
    """
    base = re.sub(r"[^a-z0-9]+", "_", company.lower()).strip("_")
    if not title:
        return base
    short = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:34].strip("_")
    return f"{base}_{short}"


def main():
    ap = argparse.ArgumentParser(description="Generate resumes for a ranked list")
    ap.add_argument("file", nargs="?", default="ranked.tsv")
    ap.add_argument("--buckets", default="apply",
                    help="comma-separated buckets to generate (default: apply)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep-html", action="store_true",
                    help="Keep the intermediate HTML each PDF is rendered from. "
                         "It is only a build artifact; the PDF is what gets sent.")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds per role before abandoning it (default 300)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    want = {b.strip() for b in args.buckets.split(",")}
    rows = []
    for line in Path(args.file).read_text().splitlines()[1:]:
        if not line.strip():
            continue
        p = (line.split("\t") + [""] * 6)[:6]
        if p[0] in want:
            rows.append({"bucket": p[0], "company": p[2], "title": p[3], "url": p[4]})
    if args.limit:
        rows = rows[: args.limit]

    from collections import Counter
    per_company = Counter(r["company"] for r in rows)
    todo, have = [], []
    for r in rows:
        r["slug"] = slug_for(r["company"], r["title"])
        r["name"] = name_for(r["company"], r["title"], per_company[r["company"]] > 1)
        pdf = Path(f"{r['name']}.pdf")
        (have if pdf.exists() else todo).append(r)

    print(f"{len(rows)} roles in {sorted(want)} | {len(have)} already generated "
          f"| {len(todo)} to run  (~${len(todo) * COST_PER_ROLE:.2f})\n")
    if args.dry_run:
        for r in todo:
            print(f"  {r['company'][:18]:18} {r['title'][:52]}")
        return

    ok, failed = [], []
    for i, r in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {r['company']} — {r['title'][:44]}", flush=True)
        # Popen in its own session, not subprocess.run(timeout=...).
        # tailor_resume spawns Chromium through Playwright, and Chromium
        # inherits the stdout pipe. On timeout, run() kills the direct child
        # and then blocks forever waiting for EOF that the surviving browser
        # never sends -- a batch wedged there for good. Killing the whole
        # process group releases the pipe.
        proc = subprocess.Popen(
            [sys.executable, "tailor_resume.py", "--url", r["url"],
             "--company", r["company"], "--slug", r["slug"],
             "--name", r["name"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        try:
            out = proc.communicate(timeout=args.timeout)[0]
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            out = proc.communicate()[0] or ""
            failed.append((r, [f"exceeded {args.timeout}s"]))
            print(f"      TIMED OUT after {args.timeout}s — moving on", flush=True)
            continue
        proc.stdout_text = out
        if proc.returncode == 0:
            # The HTML is the source Playwright prints from, not a deliverable.
            # Two files per role doubles what you scroll past looking for the
            # one you actually attach.
            if not args.keep_html:
                Path(f"{r['name']}.html").unlink(missing_ok=True)
            ok.append(r)
            tail = [l for l in out.splitlines() if "PDF written" in l]
            print(f"      {tail[0] if tail else 'done'}", flush=True)
        else:
            err = out.strip()
            if any(k in err.lower() for k in BROKE):
                print("      STOPPED: out of API credit", flush=True)
                print(f"\n{len(ok)} generated before the balance ran out. "
                      f"{len(todo) - i + 1} not attempted — top up and rerun; "
                      f"finished roles are skipped.", flush=True)
                sys.exit(1)
            failed.append((r, err.splitlines()[-1:]))
            print(f"      FAILED: {failed[-1][1]}", flush=True)

    print(f"\n{len(ok)} generated, {len(failed)} failed")
    for r, err in failed:
        print(f"  {r['company'][:18]:18} {err}")
    print("\nEach run appends to applications.csv. "
          "Ask Claude to push that to the Google Sheet.")


if __name__ == "__main__":
    main()
