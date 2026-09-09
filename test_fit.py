#!/usr/bin/env python3
"""Does the fit assessment actually discriminate? Run: python test_fit.py

Separate from test_qa.py because this costs money -- it calls the model. It
exists because the assessment quietly stopped discriminating: across 58 real
postings, 52 came back "Strong" and 35 had nothing unmet at all, and a sample
run judged 12 of 12 requirements met. Half of those were keyword hits on the
skills list rather than evidence of having done anything.

The eval mixes requirements the resume plainly satisfies with ones it plainly
does not, plus the specific loophole: a tool that appears ONLY in the skills
list and in no experience bullet.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fit
import tailor_resume as tr

RESUME = json.loads(tr.MASTER_RESUME.read_text())

# (requirement, acceptable statuses, why)
CASES = [
    ("Strong Python for automation and data work", {"met"},
     "several bullets describe building Python automations"),
    ("Experience owning Salesforce or HubSpot as a system, not just using it", {"met"},
     "a bullet describes rebuilding routing and enrichment across both"),
    ("Track record generating pipeline through outbound automation", {"met"},
     "bullets carry dollar pipeline figures from automation work"),

    # The loophole. Playwright and Git appear in the skills list and in no
    # experience bullet anywhere. Before the fix these came back "met".
    ("Hands-on experience building browser automation with Playwright",
     {"partial", "unmet"}, "Playwright is in the skills list and no bullet"),
    ("Proficiency with Git-based workflows", {"partial", "unmet"},
     "Git is in the skills list and no bullet"),

    # Nowhere in the resume at all.
    ("Production experience with Kubernetes", {"unmet"}, "never mentioned"),
    ("Strong Java or Scala for backend services", {"unmet"}, "never mentioned"),
    ("Experience owning a SOC 2 audit end to end", {"unmet"}, "never mentioned"),
    ("Managing and growing a team of engineers", {"partial", "unmet"},
     "every role in the resume is an individual contributor"),
    ("12+ years of go-to-market experience", {"partial", "unmet"},
     "the resume covers 2021-2026"),
]


def main():
    tr.load_env()
    client = tr.make_client()
    analysis = {"required_skills": [c[0] for c in CASES],
                "role_title": "GTM Engineer", "track": "gtm"}

    with tr.usage_scope() as totals:
        result = fit.assess(client, analysis, RESUME, tr.MODEL)

    got = {a["requirement"]: a for a in result.get("assessments", [])}
    failures = []
    print(f"verdict: {result['verdict']} ({result['score']:.0%})\n")

    for requirement, allowed, why in CASES:
        a = got.get(requirement) or next(
            (v for k, v in got.items() if k[:40] == requirement[:40]), None)
        status = (a or {}).get("status", "MISSING")
        ok = status in allowed
        if not ok:
            failures.append(f"{requirement[:52]} -> {status}")
        print(f"  {'ok  ' if ok else 'FAIL'}  {status:8} {requirement[:56]}")
        if not ok:
            print(f"          expected {'/'.join(sorted(allowed))} — {why}")
            if (a or {}).get("evidence"):
                print(f"          it cited: {a['evidence'][:88]}")

    unmet = sum(1 for a in got.values() if a.get("status") == "unmet")
    print(f"\n  {unmet} of {len(CASES)} judged unmet")
    if unmet == 0:
        failures.append("nothing at all came back unmet")
        print("  FAIL  a resume with real gaps must produce some")

    print(f"\n{tr.usage_line(totals)}")
    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
