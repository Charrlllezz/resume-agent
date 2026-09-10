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
import threading
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import qa
import tailor_resume as tr

# Always the fixture, never tr.MASTER_RESUME. That resolves to the user's own
# resume once they have one, and a suite that changes shape depending on whose
# resume is on disk tests nothing -- it crashed outright the first time a real
# one was ingested.
SAMPLE = Path(__file__).parent / "master_resume.example.json"
RESUME = json.loads(SAMPLE.read_text())
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
_master = RESUME
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

print("\ntoken accounting (one accumulator, whatever the import path):")
import usage as usage_mod


class _FakeResponse:
    class usage:
        input_tokens = 10
        output_tokens = 5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0


# `python tailor_resume.py` runs that file as __main__, and fit.py reaches back
# for it by name -- which loads a SECOND copy of the module. While the counters
# lived in tailor_resume, each copy kept its own, so every cost the CLI printed
# was missing the fit call. Two copies must now agree.
_src = (Path(__file__).parent / "tailor_resume.py").read_text()
_as_main = {"__name__": "__main__", "__file__": "tailor_resume.py"}
exec(compile(_src.replace("\nif __name__ ==", "\nif False and __name__ =="),
             "tailor_resume.py", "exec"), _as_main)
with usage_mod.scope() as _totals:
    _as_main["record_usage"](_FakeResponse())   # the copy running as __main__
    tr.record_usage(_FakeResponse())            # the copy fit.py imports
    expect(_totals["calls"] == 2,
           f"both module copies count into one accumulator (got {_totals['calls']}, want 2)")

# A server runs several at once; one user's spend must not appear in another's.
_seen = {}


def _one_run(name, calls):
    with usage_mod.scope() as totals:
        for _ in range(calls):
            tr.record_usage(_FakeResponse())
        _seen[name] = totals["calls"]


_threads = [threading.Thread(target=_one_run, args=(f"r{i}", i + 1)) for i in range(4)]
[t.start() for t in _threads]
[t.join() for t in _threads]
expect(_seen == {"r0": 1, "r1": 2, "r2": 3, "r3": 4},
       f"concurrent runs keep separate totals (got {_seen})")


print("\ningestion provenance (a draft must trace to the document it came from):")
import ingest

_DOC = """Jordan Reyes
jordan.reyes@example.com

Northwind AI                                        Apr 2026 - Aug 2026
Solutions Architect, New Verticals
- Built the onboarding workflow that account teams now run on all 25+
  enterprise accounts, trimming time-to-first-value by roughly 40%
- Caught platform configuration issues before they reached the account team,
  including scaling suppression logic past 1,000 domains
"""


def _draft(bullets):
    return {"contact": {"name": "Jordan Reyes", "email": "jordan.reyes@example.com"},
            "experience": [{"company": "Northwind AI", "bullets": {"general": bullets}}]}


_real = ["Built the onboarding workflow that account teams now run on all 25+ "
         "enterprise accounts, trimming time-to-first-value by roughly 40%"]

expect(not [i for i in ingest.check(_draft(_real), _DOC) if i.level == qa.ERROR],
       "accepts a bullet transcribed from the document")

expect(any(i.check == "traced" for i in ingest.check(
           _draft(["Drove $47M in net-new ARR and scaled the team from 3 to 60"]), _DOC)),
       "catches a bullet that is not in the document at all")

# Similarity will not catch this on its own: the bullet stays ~97% identical
# and becomes entirely false. It is why numbers are checked separately.
_inflated = [_real[0].replace("25+", "250+").replace("40%", "90%")]
expect(any(i.check == "numbers" for i in ingest.check(_draft(_inflated), _DOC)),
       "catches numbers inflated inside an otherwise real bullet")

expect(any(i.check == "contact" for i in ingest.check(
           {"contact": {"name": "Someone Else"}, "experience": [{"company": "X",
            "bullets": {"general": _real}}]}, _DOC)),
       "catches a name that is not in the document")


print("\nhosted URL safety (the posting URL comes from a stranger):")
import sessions as sessions_mod

for _url, _blocked, _why in [
    ("http://169.254.169.254/latest/meta-data/", True, "cloud metadata"),
    ("http://127.0.0.1:8080/healthz", True, "this container"),
    ("http://localhost/admin", True, "this container by name"),
    ("http://something.internal/", True, "the private network"),
    ("http://10.0.0.5/", True, "RFC1918"),
    ("file:///etc/passwd", True, "not http"),
    ("https://job-boards.greenhouse.io/anthropic/jobs/5290838008", False, "a real posting"),
]:
    expect(bool(sessions_mod.safe_url(_url)) == _blocked,
           f"{'blocks' if _blocked else 'allows'} {_why}: {_url[:46]}")


print("\nstyle detection (the output should look like what was uploaded):")
import style as style_mod

for _raw, _want, _why in [
    ("AAAAAA+IBMPlexMono-SemiBold", "monospace", "a subset prefix and weight are not the family"),
    ("Lora", "serif", "serif-ness is recorded, not guessed from the name"),
    ("Playfair Display", "serif", "same"),
    ("Inter", "sans-serif", "sans stays sans"),
    ("TimesNewRomanPSMT", "serif", "maps to a metric-compatible web font"),
]:
    _stack, _ = style_mod.resolve(_raw)
    expect(_stack.endswith(_want), f"{_why}: {_raw!r} -> {_stack}")

expect(style_mod.resolve("Calibri")[1] == "Carlito:wght@400;700",
       "Calibri maps to Carlito, which is metric-compatible so line breaks hold")

# Anything the model returns is untrusted input to a stylesheet.
_dirty = style_mod.merge(style_mod.DEFAULTS,
                         {"chrome": "sidebar", "accent": "red; } body { display:none",
                          "header_align": "justify", "density": "airy"})
expect(_dirty["chrome"] == "designed", "an unknown chrome falls back to the default")
expect(_dirty["accent"] == style_mod.DEFAULTS["accent"],
       "a colour that is not a hex triple is refused, not injected into the CSS")
expect(_dirty["header_align"] == "left", "an unknown alignment falls back")
expect(_dirty["density"] == "normal", "an unknown density falls back")

# A resume set in one font must come back set in one font. Falling through to
# a generic monospace for dates and taglines puts type on the page the original
# never had, which is the opposite of matching it.
expect(style_mod.from_fonts(["timesnewromanpsmt"])["font_mono"]
       == style_mod.from_fonts(["timesnewromanpsmt"])["font_body"],
       "a one-font document uses that font for everything, including dates")
expect("mono" in style_mod.from_fonts(["ibmplexsans", "ibmplexmono"])["font_mono"].lower(),
       "a document that really has a monospace keeps it")

# Whether a rule actually applied is a question about the rendered page, not
# about whether a string appears in a stylesheet -- three style bugs passed a
# suite that grepped the CSS source. test_render.py asserts computed style in a
# browser instead; run it after touching RESUME_CSS, PLAIN_CSS or style.py.


expect(style_mod._is_grey("#2B2B2B") and style_mod._is_grey("#FFFFFF"),
       "greys are recognised as having no hue")
expect(not style_mod._is_grey("#0B3C5D") and not style_mod._is_grey("#B85C1E"),
       "real colours are not mistaken for grey")
expect(style_mod._luma("#000000") < style_mod._luma("#2B2B2B") < style_mod._luma("#FFFFFF"),
       "luminance orders dark to light")


# A PDF stores "fi" as one ligature glyph, so extraction returns a character
# no one typed. The model transcribes it back correctly and the guard then
# reports the name as missing -- a false positive in the fabrication check,
# which teaches people to ignore it.
_LIG_DOC = ("Dana Whit\ufb01eld\ndana.whit\ufb01eld@example.com\n"
            "- Recti\ufb01ed the routing rules that raised speed-to-lead "
            "from 14 hours to 40 minutes across three regional teams")
_lig_draft = {"contact": {"name": "Dana Whitfield", "email": "dana.whitfield@example.com"},
              "experience": [{"company": "Northgate", "bullets": {"general": [
                  "Rectified the routing rules that raised speed-to-lead from 14 "
                  "hours to 40 minutes across three regional teams"]}}]}
expect(not ingest.check(_lig_draft, _LIG_DOC),
       "a ligature in the PDF does not make a correct transcription look invented")
expect("\ufb01" not in ingest.normalise(_LIG_DOC), "ligatures are folded")
expect(ingest.normalise("soft\u00adhyphen") == "softhyphen", "soft hyphens are dropped")
expect(ingest.normalise("curly\u2019s") == "curly's", "curly quotes are normalised")


print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
