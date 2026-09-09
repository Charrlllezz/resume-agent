#!/usr/bin/env python3
"""
Tests for the QA layer. Run: python test_qa.py

A QA layer that never fires is worse than none, so these test both directions:
a clean resume built straight from the master pools must pass with zero
findings, and a resume with five specific defects planted in it must catch
exactly those five.
"""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import qa
import tailor_resume as tr

RESUME = json.loads(tr.MASTER_RESUME.read_text())
# How many bullets the fixture takes per role. Derived, not hardcoded, so
# the suite follows whatever resume is in place instead of one person's.
COUNTS = {r["company"]: qa.expected_bullets(r)[1] for r in RESUME["experience"]}
ANALYSIS = {"ats_keywords": ["Clay", "HubSpot", "n8n"]}



def _reorder(bullet: str) -> str:
    """Move a bullet's trailing outcome to the front, keeping every word.

    This is exactly the edit the tailoring prompt is allowed to make -- the
    same content, led by the result instead of the build -- so provenance must
    still accept it. Anything the model cannot reach by reordering is invention.
    """
    parts = [c.strip() for c in bullet.split(",")]
    tail = parts[-1]
    return ", ".join([tail[0].upper() + tail[1:]] + parts[:-1])


def clean_resume() -> dict:
    """A tailored resume assembled entirely from master content."""
    return {
        "headline": RESUME["headlines"]["gtm"][0],
        "experience": [
            {
                "company": r["company"],
                "title": r["titles"]["gtm"],
                "location": r["location"],
                "dates": r["dates"],
                "bullets": r["bullets"]["gtm"][: COUNTS[r["company"]]],
            }
            for r in RESUME["experience"]
        ],
        "skills": {k: list(v) for k, v in RESUME["skills"]["gtm"].items()},
    }


# "balance" is a style signal about story ordering, not a factual finding, and
# the fixture below takes bullets in pool order rather than a curated order.
STYLE_CHECKS = {"balance"}


def findings(tailored: dict):
    html = tr.render_html(tailored, RESUME)
    issues = qa.verify(tailored, RESUME, ANALYSIS, html)
    return [i for i in issues if i.level != qa.INFO and i.check not in STYLE_CHECKS]


failures = []


def expect(condition, label):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


print("clean resume:")
issues = findings(clean_resume())
expect(not issues, f"passes with no findings (got {[i.message for i in issues]})")

print("\nplanted defects:")

cases = [
    (
        "fabricated bullet",
        lambda r: r["experience"][0]["bullets"].__setitem__(
            0, "Drove $47M in net-new ARR and scaled the team from 3 to 60 engineers"
        ),
        "bullets",
    ),
    (
        "invented metric in a real bullet",
        lambda r: r["experience"][1]["bullets"].__setitem__(
            0, r["experience"][1]["bullets"][0] + " across 900 enterprise accounts"
        ),
        "metrics",
    ),
    (
        "stretched employment dates",
        lambda r: r["experience"][2].__setitem__("dates", "Jun 2021 - Apr 2025"),
        "roles",
    ),
    (
        "unearned skill",
        lambda r: r["skills"]["AI Tooling"].append("Kubernetes"),
        "skills",
    ),
    (
        "inflated title",
        lambda r: r["experience"][4].__setitem__("title", "VP of Revenue Operations"),
        "roles",
    ),
    (
        "dropped bullet count",
        lambda r: r["experience"][0].__setitem__("bullets", r["experience"][0]["bullets"][:1]),
        "bullets",
    ),
]

for label, corrupt, expected_check in cases:
    bad = clean_resume()
    corrupt(bad)
    caught = [i for i in findings(bad) if i.check == expected_check]
    expect(bool(caught), f"catches {label} -> [{expected_check}]")

print("\nmetric highlighting:")
highlight_cases = [
    ("Led presentations for Fortune 500 prospects", False, "Fortune 500 not styled"),
    ("Closed $204K at 123% of quota", True, "currency and percent styled"),
    ("using Clay, n8n, and Zapier", False, "n8n left intact"),
    ("Ranked #1 on team H1 2023", True, "#1 styled, year is not"),
]
for text, should_style, label in highlight_cases:
    out = tr.highlight_metrics(text)
    styled = '<span class="m">' in out
    ok = styled == should_style and "&#x" not in out
    if text.startswith("Ranked"):
        ok = ok and "2023" in out and '>2023<' not in out
    expect(ok, f"{label} -> {out}")

print("\ntitle reworking (allowed within reason, blocked when inflated):")
title_cases = [
    ("Solutions Architect, New Verticals",  "Solutions Architect, GTM Systems", True,  "reworded framing"),
    ("GTM Engineer",                        "GTM Systems Engineer",             True,  "added qualifier"),
    ("GTM Engineer",                        "Growth Engineer, GTM",             True,  "vocabulary swap"),
    ("GTM Engineer",                        "Senior GTM Engineer",              False, "adds seniority"),
    ("GTM Engineer",                        "Lead GTM Engineer",                False, "adds lead"),
    ("Solutions Consultant",                "VP of Revenue Operations",         False, "adds VP"),
    ("Solutions Consultant",                "Director of Solutions",            False, "adds director"),
    ("Solutions Consultant",                "Head of Customer Success",         False, "adds head of"),
    ("Strategic Growth Representative",     "Data Engineer",                    False, "different role"),
]
for master, proposed, allowed, label in title_cases:
    found = qa._check_title("X", proposed, [master])
    blocked = any(i.level == qa.ERROR for i in found)
    expect(blocked != allowed, f"{'allows' if allowed else 'blocks'} {label}: {proposed!r}")

print("\nbullet provenance (reordering allowed, invention is not):")
_pool = RESUME["experience"][0]["bullets"]["gtm"]
_src = _pool[0]  # the reordering case below is built from this bullet
provenance_cases = [
    (_src, True, "verbatim bullet"),
    (_reorder(_src), True, "reordered outcome-first"),
    ("Drove $47M in net-new ARR and scaled the team from 3 to 60 engineers", False, "fabricated"),
    ("Led a cross-functional migration to Snowflake that cut reporting latency by 60%",
     False, "plausible-sounding invention"),
]
for text, should_pass, label in provenance_cases:
    score, _ = qa._best_bullet_match(text, _pool)
    expect((score >= qa.EDITED) == should_pass,
           f"{'accepts' if should_pass else 'rejects'} {label} ({score:.0%})")

print("\nseniority parsing (punctuation must not hide a marker):")
import fit as fit_mod
for title, rank_over_ic, label in [
    ("Director, Solutions", True, "comma after Director"),
    ("Senior Manager, Deal Desk", True, "comma after Manager"),
    ("GTM Engineer", False, "plain IC title"),
    ("Solutions Consultant", False, "plain IC title"),
]:
    expect((qa._seniority(title) > 1) == rank_over_ic, f"{label}: {title!r}")

print("\npeople-management blocker (IC 'Manager' titles must not be blocked):")
for title, blocked, label in [
    ("Enterprise Account Manager", False, "Account Manager is IC"),
    ("Customer Success Manager, Commercial East", False, "CSM is IC"),
    ("Senior Manager, Deal Desk", True, "Senior Manager manages"),
    ("Director of Revenue Operations", True, "Director manages"),
    ("Head of Customer Success", True, "Head of manages"),
    ("Sr. AI GTM Engineer", False, "Sr. Engineer is IC"),
]:
    expect(fit_mod.manages_people(title) == blocked, f"{label}: {title!r}")

print("\nstory balance (customer outcomes vs building):")
build_lead = copy.deepcopy(clean_resume())
build_lead["experience"][0]["bullets"] = ["Built and deployed the automation stack"]
warned = [i for i in qa.check_story_balance(build_lead) if i.level == qa.WARN]
expect(bool(warned), "warns when a role leads with a building bullet")

cust_lead = copy.deepcopy(clean_resume())
cust_lead["experience"][0]["bullets"] = ["Cut onboarding time for 25+ enterprise customers"]
quiet = [i for i in qa.check_story_balance(cust_lead) if i.level == qa.WARN]
_first = RESUME["experience"][0]["company"]
expect(not any(_first in i.message for i in quiet), "quiet when a role leads with a customer outcome")

print("\ncustomer/building classification (plurals count):")
for text, want_customer, label in [
    ("Built trusted relationships with CMOs across enterprise accounts", True,
     "plural 'relationships' and 'accounts' are customer signals"),
    ("Partnered daily with Account Management across 20+ premium accounts", True,
     "plural 'accounts'"),
    ("Built and deployed the automation stack", False,
     "a genuinely building-led bullet stays building-led"),
]:
    got = bool(qa.CUSTOMER_RE.search(text))
    expect(got == want_customer, f"{label}: {text[:52]!r}")

print("\nheadline (positioning is loose; seniority is not):")
_master = json.loads(tr.MASTER_RESUME.read_text())
for headline, want_error, label in [
    ("AI Agents, Workflow Automation & Technical Implementation", False,
     "verbatim pick from the master pool"),
    ("Enterprise AI Implementation & Customer Onboarding", False,
     "adapted positioning is allowed, not an error"),
    ("Director of Solutions Engineering", True, "unearned rank: director"),
    ("VP of Customer Success", True,
     "unearned rank caught even at 51%, above the loosened threshold"),
    ("Principal Architect, AI Platforms", True, "unearned rank: principal"),
]:
    got = any(i.level == qa.ERROR
              for i in qa.check_headline({"headline": headline}, _master))
    expect(got == want_error, f"{label}: {headline!r}")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
