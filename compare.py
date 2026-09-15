#!/usr/bin/env python3
"""
Run the tailoring agent across several postings and compare the results.

One posting tells you whether a run succeeded. A batch tells you whether the
agent is actually good: does it classify tracks correctly, does story balance
hold across role types, which master bullets never get picked, and where does
ATS coverage really sit once you stop reading a single noisy number.

Input is a TSV: expected_track <TAB> company <TAB> label <TAB> url
The expected_track column is optional -- with it, classification is scored.

Usage:
  python compare.py roles.tsv
  python compare.py roles.tsv --json results.json

PDFs are skipped; this measures selection quality, not rendering.

Results are cached per URL as they land. A sweep is dozens of paid calls over
20+ minutes; holding them in memory until the end meant one wedged role threw
away every role before it. A rerun skips what is cached.
"""

import argparse
import json
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import anthropic

import qa
import tailor_resume as tr

CACHE = Path("compare_cache.json")


def load_rows(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 1:
            rows.append({"expected": "", "company": "", "label": "", "url": parts[0].strip()})
        else:
            expected, company, label, url = (parts + [""] * 4)[:4]
            rows.append({
                "expected": expected.strip(), "company": company.strip(),
                "label": label.strip(), "url": url.strip(),
            })
    return rows


def run_one(client, resume: dict, row: dict) -> dict:
    started = time.time()
    posting = tr.fetch_url(row["url"])
    analysis = tr.analyze_job(client, posting, resume)
    tailored = tr.tailor_resume(client, resume, analysis, posting)
    html = tr.render_html(tailored, resume)
    issues = qa.verify(tailored, resume, analysis, html)

    def find(check, prefix):
        for i in issues:
            if i.check == check and i.message.startswith(prefix):
                return i.message
        return ""

    balance = find("balance", "")
    customer = building = 0
    if balance:
        parts = balance.split()
        customer, building = int(parts[0]), int(parts[4])

    ats = next((i.message for i in issues if i.check == "ats"), "")
    coverage = int(ats.split("(")[1].rstrip("%)")) if "(" in ats else 0

    return {
        "url": row["url"],
        "company": row["company"] or analysis.get("company", ""),
        "label": row["label"],
        "expected": row["expected"],
        "detected": analysis.get("track", ""),
        "role_title": analysis.get("role_title", ""),
        "headline": tailored.get("headline", ""),
        "titles": [r.get("title", "") for r in tailored.get("experience", [])],
        "bullets": [b for r in tailored.get("experience", []) for b in r.get("bullets", [])],
        "skills": sorted({s for v in tailored.get("skills", {}).values() for s in v}),
        "customer_bullets": customer,
        "building_bullets": building,
        "ats_coverage": coverage,
        "ats_total": len(analysis.get("ats_keywords", [])),
        "errors": [i.message for i in issues if i.level == qa.ERROR],
        "warnings": [i.message for i in issues if i.level == qa.WARN],
        "seconds": round(time.time() - started, 1),
    }


def report(results: list, resume: dict):
    ok = [r for r in results if "failed" not in r]
    if not ok:
        print("no successful runs")
        return

    print("\n" + "=" * 100)
    print("PER-ROLE")
    print("=" * 100)
    print(f"{'company':8} {'role':34} {'exp':4} {'got':6} {'cust/build':11} {'ATS':7} {'QA':14} {'s':>5}")
    print("-" * 100)
    for r in ok:
        match = "" if not r["expected"] else ("ok" if r["expected"] == r["detected"] else "MISS")
        qa_s = "clean" if not r["errors"] else f"{len(r['errors'])} ERROR"
        if r["warnings"] and not r["errors"]:
            qa_s = f"{len(r['warnings'])} warn"
        print(f"{r['company'][:8]:8} {r['label'][:34]:34} {r['expected']:4} "
              f"{r['detected']:3}{match:>3} {r['customer_bullets']:4}/{r['building_bullets']:<6} "
              f"{r['ats_coverage']:3}%/{r['ats_total']:<3} {qa_s:14} {r['seconds']:5}")

    labeled = [r for r in ok if r["expected"]]
    if labeled:
        hits = sum(1 for r in labeled if r["expected"] == r["detected"])
        print(f"\nTrack classification: {hits}/{len(labeled)} correct")
        for r in labeled:
            if r["expected"] != r["detected"]:
                print(f"  MISS  {r['company']} {r['label'][:44]} -> expected {r['expected']}, got {r['detected']}")

    cust = sum(r["customer_bullets"] for r in ok)
    build = sum(r["building_bullets"] for r in ok)
    print(f"\nStory balance across all runs: {cust} customer-outcome vs {build} building-led")
    for track in ("gtm", "cs", "hybrid"):
        rows = [r for r in ok if r["detected"] == track]
        if rows:
            c = sum(x["customer_bullets"] for x in rows)
            b = sum(x["building_bullets"] for x in rows)
            print(f"  {track:7} {c:3} customer / {b:3} building   "
                  f"ATS avg {sum(x['ats_coverage'] for x in rows) // len(rows)}%")

    # Which master bullets never get chosen -- dead weight worth rewriting.
    pool = {}
    for role in resume["experience"]:
        for bl in qa._flatten(role["bullets"]):
            pool[qa._norm(bl)] = (role["company"], bl)
    # Ask "was this pool bullet used?" for each pool bullet, rather than
    # attributing each output bullet to one best source. Near-duplicate pool
    # entries otherwise steal each other's credit and look unused.
    outputs = [b for r in ok for b in r["bullets"]]
    used = Counter()
    for key, (_, source) in pool.items():
        for out in outputs:
            both_ways = min(qa._containment(out, source), qa._containment(source, out))
            seq = SequenceMatcher(None, qa._norm(out), qa._norm(source)).ratio()
            if max(seq, both_ways) >= 0.70:
                used[key] += 1

    print(f"\nBullet pool usage ({len(used)}/{len(pool)} bullets ever selected):")
    never = [v for k, v in pool.items() if k not in used]
    by_company = Counter(c for c, _ in never)
    for company, n in by_company.most_common():
        print(f"  {company:10} {n} never selected")
    if never:
        print("\n  Never selected (candidates to rewrite or cut):")
        for company, bullet in never[:12]:
            print(f"    [{company}] {bullet[:86]}")

    skills = Counter(s for r in ok for s in r["skills"])
    print(f"\nMost-selected skills: {', '.join(s for s, _ in skills.most_common(10))}")

    errs = [(r["company"], e) for r in ok for e in r["errors"]]
    if errs:
        print(f"\nQA errors ({len(errs)}):")
        for company, e in errs:
            print(f"  [{company}] {e[:92]}")


def main():
    ap = argparse.ArgumentParser(description="Compare tailoring across several postings")
    ap.add_argument("file", help="TSV of postings")
    ap.add_argument("--json", help="also write raw results here")
    ap.add_argument("--refresh", action="store_true", help="ignore cached results")
    ap.add_argument("--budget", type=int, default=300,
                    help="hard seconds per posting before giving up (default 300)")
    args = ap.parse_args()

    rows = load_rows(Path(args.file))
    resume = json.loads(tr.MASTER_RESUME.read_text(encoding="utf-8"))
    client = tr.make_client()

    cache = {} if args.refresh else (
        json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {})
    cached = sum(1 for r in rows if r["url"] in cache)
    if cached:
        print(f"{cached}/{len(rows)} already cached -- rerun with --refresh to redo\n")

    results = []
    for i, row in enumerate(rows, 1):
        label = row["label"] or row["url"][:60]
        if row["url"] in cache:
            results.append(cache[row["url"]])
            print(f"[{i}/{len(rows)}] {row['company']} {label} (cached)", flush=True)
            continue
        print(f"[{i}/{len(rows)}] {row['company']} {label}...", flush=True)
        try:
            with tr.deadline(args.budget):
                r = run_one(client, resume, row)
            print(f"      {r['detected']} | ATS {r['ats_coverage']}% | "
                  f"{r['customer_bullets']}c/{r['building_bullets']}b | "
                  f"{len(r['errors'])} err | {r['seconds']}s", flush=True)
            results.append(r)
        except Exception as e:
            print(f"      FAILED: {type(e).__name__}: {e}", flush=True)
            r = {**row, "failed": f"{type(e).__name__}: {e}"}
            results.append(r)
        # Written per role: a wedged role should cost the roles it did not
        # reach, not the ones already paid for.
        cache[row["url"]] = r
        CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    report(results, resume)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nraw results -> {args.json}")


if __name__ == "__main__":
    main()
