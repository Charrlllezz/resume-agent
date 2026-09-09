#!/usr/bin/env python3
"""
Rank a whole list of postings by whether they are worth applying to.

fit.py judges one role. This runs it across a TSV and sorts the result, so a
day of applications starts from evidence instead of from whichever posting is
at the top of the file.

Resumable by design. A sweep is dozens of paid calls over half an hour, and the
last one died 41 roles in when the credit balance hit zero -- with nothing
written down, because results were only held in memory. Every assessment is
appended to the cache the moment it lands, and a rerun skips what is already
there. Interrupting this is safe.

Usage:
  python rank.py resolved.tsv
  python rank.py resolved.tsv --limit 8        # try a few first
  python rank.py resolved.tsv --refresh        # ignore the cache
"""

import argparse
import json
import sys
import time
from pathlib import Path

import anthropic

import fit
import tailor_resume as tr

CACHE = Path("rank_cache.json")
ORDER = {"Strong": 0, "Possible": 1, "Stretch": 2, "Skip": 3}

# Scoring this list produced 52 "Strong" out of 58 -- the saved jobs were
# already filtered by hand, so a fine-grained ranking has nothing to separate.
# Buckets say the one thing that changes what you do: apply, apply with an
# answer ready, or do not bother.
BUCKETS = ("apply", "prep", "stretch", "skip")

# A gap you cannot close by reading the docs the night before.
HARD = ("react", "full-stack", "full stack", "postgres", "kafka", "java",
        "saml", "shibboleth", "scim", "sis platform", "bachelor", "degree",
        "git", "workday", "dayforce", "apex", "lightning web")

# Not a gap at all -- a different job.
STRUCTURAL = ("people management", "team leadership", "managing a team",
              "manage a team", "people manager", "deal desk")


def bucket(row: dict) -> str:
    unmet = [u.lower() for u in row.get("unmet", [])]
    if row.get("verdict") == "Skip" or row.get("blockers"):
        return "skip"
    if any(k in u for u in unmet for k in STRUCTURAL):
        return "skip"
    if any(k in u for u in unmet for k in HARD):
        return "stretch"
    if not unmet:
        return "apply"
    return "prep" if len(unmet) <= 2 else "stretch"


def load_rows(path: Path) -> list:
    rows = []
    for line in path.read_text().splitlines()[1:]:
        if not line.strip():
            continue
        p = (line.split("\t") + [""] * 4)[:4]
        if p[3].startswith("http"):
            rows.append({"track": p[0], "company": p[1], "title": p[2], "url": p[3]})
    return rows


def out_of_credit(err: Exception) -> bool:
    """Stop the sweep rather than fail the same way 50 more times."""
    text = str(err).lower()
    return "credit balance" in text or "insufficient" in text


def main():
    ap = argparse.ArgumentParser(description="Rank postings by fit")
    ap.add_argument("file", nargs="?", default="resolved.tsv")
    ap.add_argument("--limit", type=int, default=0, help="only the first N roles")
    ap.add_argument("--refresh", action="store_true", help="ignore cached results")
    ap.add_argument("--out", default="ranked.tsv")
    ap.add_argument("--budget", type=int, default=300,
                    help="hard seconds per role before giving up (default 300)")
    args = ap.parse_args()

    rows = load_rows(Path(args.file))
    if args.limit:
        rows = rows[: args.limit]

    cache = {} if args.refresh else (
        json.loads(CACHE.read_text()) if CACHE.exists() else {})
    resume = json.loads(tr.MASTER_RESUME.read_text())
    client = tr.make_client()

    # A cached error is not completed work -- it cost nothing and the cause is
    # usually transient (three roles failed a sweep with an empty fetch and all
    # three loaded fine minutes later). Retry those; only keep real verdicts.
    todo = [r for r in rows if cache.get(r["url"], {}).get("verdict", "Error") == "Error"]
    print(f"{len(rows)} roles | {len(rows) - len(todo)} cached | {len(todo)} to assess\n")

    stopped = None
    for i, row in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {row['company']} — {row['title'][:44]}", flush=True)
        started = time.time()
        try:
            with tr.deadline(args.budget):
                posting = tr.fetch_url(row["url"])
                if len(posting) < 500:
                    raise ValueError(f"posting too short ({len(posting)} chars) "
                                     "-- likely login-walled or filled")
                analysis = tr.analyze_job(client, posting)
                assessment = fit.assess(client, analysis, resume, tr.MODEL)
        except Exception as e:
            if out_of_credit(e):
                stopped = "out of API credit"
                print(f"      STOPPED: {e}", flush=True)
                break
            print(f"      FAILED: {type(e).__name__}: {e}", flush=True)
            cache[row["url"]] = {**row, "verdict": "Error",
                                 "reason": f"{type(e).__name__}: {e}"[:160]}
            CACHE.write_text(json.dumps(cache, indent=2))
            continue

        cache[row["url"]] = {**row, "role_title": analysis.get("role_title", ""),
                             "detected_track": analysis.get("track", ""),
                             "verdict": assessment["verdict"],
                             "score": assessment["score"],
                             "reason": assessment["reason"],
                             "unmet": [a["requirement"] for a in assessment["assessments"]
                                       if a.get("status") == "unmet"]}
        # Written per role, not at the end: a sweep that dies partway should
        # cost the roles it did not reach, not the ones it already paid for.
        CACHE.write_text(json.dumps(cache, indent=2))
        print(f"      {assessment['verdict']} ({assessment['score']:.0%}) "
              f"| {time.time() - started:.0f}s", flush=True)

    results = [cache[r["url"]] for r in rows if r["url"] in cache]
    for r in results:
        r["bucket"] = bucket(r)
    results.sort(key=lambda r: (BUCKETS.index(r["bucket"]),
                                len(r.get("unmet", [])), -r.get("score", 0)))

    HEAD = {"apply": "APPLY — nothing unmet",
            "prep":  "PREP — apply, but have an answer ready",
            "stretch": "STRETCH — a gap you cannot close tonight",
            "skip":  "SKIP — a different job, not a gap"}
    for b in BUCKETS:
        rows_b = [r for r in results if r["bucket"] == b]
        if not rows_b:
            continue
        print(f"\n{'=' * 92}\n{HEAD[b]}  ({len(rows_b)})\n{'=' * 92}")
        for r in rows_b:
            gaps = ", ".join(u[:30] for u in r.get("unmet", [])[:2])
            print(f"  {r['company'][:16]:16} {r['title'][:44]:44} {gaps[:34]}")

    counts = {b: sum(1 for r in results if r["bucket"] == b) for b in BUCKETS}
    print("\n" + "  ".join(f"{b}: {n}" for b, n in counts.items() if n))

    with open(args.out, "w") as f:
        f.write("bucket\ttrack\tcompany\ttitle\turl\tgaps\n")
        for r in results:
            f.write(f"{r['bucket']}\t{r['track']}\t{r['company']}\t{r['title']}\t"
                    f"{r['url']}\t{'; '.join(r.get('unmet', []))}\n")
    print(f"\n{counts['apply'] + counts['prep']} to send -> {args.out}")
    print(f"cache -> {CACHE}  (rerun skips these; --refresh to redo)")
    print(tr.usage_report())

    if stopped:
        print(f"\nSTOPPED EARLY: {stopped}. "
              f"{len(todo) - len(results)} roles not assessed — rerun to continue.")
        sys.exit(1)


if __name__ == "__main__":
    main()
