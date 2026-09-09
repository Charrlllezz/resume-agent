#!/usr/bin/env python3
"""
Find the vocabulary your resume is systematically missing.

The per-run ATS score tells you how one posting went. This inverts it: analyze
many postings, pool their keywords, and rank the ones your master resume never
covers. A term demanded by six of eight roles and absent from your resume is a
content gap worth writing a real bullet about. A term demanded once is noise.

This deliberately reads master_resume.json, not a tailored output -- the question
is what your source material lacks, which no amount of tailoring can fix.

Usage:
  python gaps.py roles.tsv
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import anthropic

import qa
import tailor_resume as tr


def resume_text(resume: dict) -> str:
    parts = []
    for role in resume["experience"]:
        parts += qa._flatten(role["bullets"])
        parts += qa._flatten(role["titles"])
    for pool in resume["skills"].values():
        for group in pool.values():
            parts += group
    parts += qa._flatten(resume["headlines"])
    return qa._norm(" ".join(parts))


def _singularize(text: str) -> str:
    """Crude plural fold: 'integrations' -> 'integration'.

    Without this, a resume saying "REST API integration" reads as missing the
    keyword "integrations" -- a word-ending artifact reported as a content gap.
    """
    return re.sub(r"(\w{4,}?)(?:ies\b|s\b)", lambda m: m.group(1), text)


def covered(keyword: str, haystack: str) -> bool:
    """Whole-word match, plural-insensitive, so short terms can't match inside
    longer words and 'renewal'/'renewals' are not treated as different terms."""
    k = _singularize(qa._norm(keyword))
    hay = _singularize(haystack)
    return bool(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", hay))


def main():
    ap = argparse.ArgumentParser(description="Find systematic resume vocabulary gaps")
    ap.add_argument("file", help="TSV of postings (same format as compare.py)")
    ap.add_argument("--json", help="write raw keyword data here")
    args = ap.parse_args()

    rows = [r for r in _load(Path(args.file))]
    resume = json.loads(tr.MASTER_RESUME.read_text())
    hay = resume_text(resume)
    client = tr.make_client()

    demand = Counter()       # keyword -> how many postings asked for it
    by_track = {}            # keyword -> set of tracks
    seen_tracks = Counter()
    records = []

    for i, row in enumerate(rows, 1):
        label = row["label"] or row["url"][:50]
        print(f"[{i}/{len(rows)}] {row['company']} {label}...", flush=True)
        try:
            posting = tr.fetch_url(row["url"])
            analysis = tr.analyze_job(client, posting, resume)
        except Exception as e:
            print(f"      FAILED: {type(e).__name__}: {e}", flush=True)
            continue

        track = analysis.get("track", "?")
        seen_tracks[track] += 1
        terms = analysis.get("ats_keywords", []) + analysis.get("required_skills", [])
        for t in {qa._norm(x) for x in terms if x.strip()}:
            demand[t] += 1
            by_track.setdefault(t, set()).add(track)
        records.append({"company": row["company"], "label": label,
                        "track": track, "terms": sorted({qa._norm(x) for x in terms})})
        print(f"      {track} | {len(terms)} terms", flush=True)

    if not records:
        print("no postings analyzed")
        return

    gaps = [(n, t) for t, n in demand.items() if not covered(t, hay)]
    gaps.sort(reverse=True)
    have = [(n, t) for t, n in demand.items() if covered(t, hay)]
    have.sort(reverse=True)

    n = len(records)
    print("\n" + "=" * 78)
    print(f"VOCABULARY GAPS across {n} postings ({dict(seen_tracks)})")
    print("=" * 78)
    print("\nMost-demanded terms your master resume never uses:\n")
    print(f"{'asked by':>9}  {'tracks':12} term")
    print("-" * 78)
    for count, term in gaps[:30]:
        if count < 2:
            continue
        print(f"{count}/{n:<7}  {','.join(sorted(by_track[term])):12} {term[:52]}")

    singles = [t for c, t in gaps if c == 1]
    print(f"\n({len(singles)} more asked by only one posting -- noise, ignoring)")

    print("\nMost-demanded terms you DO cover:\n")
    for count, term in have[:12]:
        print(f"{count}/{n:<7}  {term[:52]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"records": records, "gaps": gaps, "covered": have}, indent=2))
        print(f"\nraw -> {args.json}")


def _load(path: Path):
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = (line.split("\t") + [""] * 4)[:4]
        yield {"expected": parts[0].strip(), "company": parts[1].strip(),
               "label": parts[2].strip(), "url": parts[3].strip()}


if __name__ == "__main__":
    main()
