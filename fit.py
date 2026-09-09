#!/usr/bin/env python3
"""
Should you actually apply to this role?

Judges the posting's must-haves against the master resume and returns a verdict
with reasons.

Two different questions need two different mechanisms, and mixing them up
produces garbage:

  * qa.py asks "does this claim trace back to the master resume?" -- a factual
    property, checkable by string comparison, and it must stay deterministic
    because it is the fabrication guard.

  * This module asks "does this experience satisfy this requirement?" --
    semantic judgment. Postings write requirements as phrases like "working
    with non-technical stakeholders", which a resume satisfies without ever
    containing the words. A literal matcher scored every real posting at 0-14%
    and recommended skipping all of them.

So this one calls the model -- but it must cite the resume evidence behind
every "met", which keeps the judgment auditable instead of a vibe. Seniority
is still checked deterministically, since a title parses reliably.

It is a heuristic. It cannot see referrals, hiring urgency, or how much a team
will train. "Skip" means "the posting asks for mostly things you cannot
evidence", not "you would not get this job".
"""

import json
import re

import qa

# Every role on the master resume is individual-contributor. A posting that
# requires managing people is a structural mismatch, not a keyword gap.
IC_CEILING = 3  # staff / principal / lead is reachable; manager+ is not

# Split so the resume half can be cached. It is identical across every posting
# in a sweep -- 58 roles meant paying full input price to re-send the same
# ~3,900 tokens 58 times. The cacheable block must come first and stay
# byte-identical between calls, so nothing per-role may leak into it.
ASSESS_RESUME = """You are assessing whether a candidate should apply to a role.

Here is the candidate's complete master resume:
{resume}"""

ASSESS_TASK = """The posting requires:
{requirements}

For EACH requirement, judge whether the resume evidences it. Return ONLY a JSON
object:

{{
  "assessments": [
    {{
      "requirement": "the requirement, verbatim",
      "status": "met" | "partial" | "unmet",
      "evidence": "the specific bullet or skill from the resume that supports
                   this, quoted. Empty string if unmet."
    }}
  ]
}}

Rules:
- "met" requires concrete evidence in the resume. Quote it.
- "partial" means adjacent or transferable experience, not the thing itself.
- "unmet" means no supporting evidence. Say unmet rather than stretching.
- Never credit a requirement to evidence that is not in the resume above.
- Judge the substance, not the wording: a requirement phrased as "working with
  non-technical stakeholders" is met by bullets showing cross-functional work,
  even though the resume never uses that phrase."""


# In GTM and CS, "Manager" is usually an individual-contributor title --
# Account Manager, Customer Success Manager. Only these markers actually imply
# managing people. A false "Skip" costs an opportunity, so this errs toward
# not blocking.
PEOPLE_MANAGEMENT = (
    "director", "vp", "svp", "evp", "vice president", "chief", "president",
    "head of", "senior manager", "sr manager", "group manager", "manager of",
    "people manager", "engineering manager",
)


def manages_people(title: str) -> bool:
    flat = " " + re.sub(r"[^a-z0-9]+", " ", qa._norm(title)).strip() + " "
    return any(f" {m} " in flat for m in PEOPLE_MANAGEMENT)


def assess(client, analysis: dict, resume: dict, model: str) -> dict:
    required = [s for s in analysis.get("required_skills", []) if s.strip()]
    title = analysis.get("role_title", "")

    blockers = []
    if manages_people(title):
        blockers.append(f"role manages people ({title})")

    if not required:
        return {
            "verdict": "Possible", "score": 0.0, "assessments": [],
            "blockers": blockers,
            "reason": "posting listed no explicit required skills",
        }

    tr_mod = __import__("tailor_resume")
    response = tr_mod.send(
        client,
        model=model,
        max_tokens=8000,
        messages=[{"role": "user", "content": [
            {"type": "text",
             "text": ASSESS_RESUME.format(resume=json.dumps(resume, indent=2)),
             "cache_control": {"type": "ephemeral"}},
            {"type": "text",
             "text": ASSESS_TASK.format(
                 requirements="\n".join(f"- {r}" for r in required))},
        ]}],
    )
    tr_mod.record_usage(response)
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    assessments = json.loads(raw).get("assessments", [])

    met = [a for a in assessments if a.get("status") == "met"]
    partial = [a for a in assessments if a.get("status") == "partial"]
    unmet = [a for a in assessments if a.get("status") == "unmet"]
    total = len(assessments) or 1

    # Partial credit is halved: adjacent experience helps, but it is not the
    # thing the posting asked for.
    score = (len(met) + 0.5 * len(partial)) / total

    if blockers:
        verdict = "Skip"
    elif score >= 0.65:
        verdict = "Strong"
    elif score >= 0.45:
        verdict = "Possible"
    elif score >= 0.28:
        verdict = "Stretch"
    else:
        verdict = "Skip"

    reason = f"{len(met)} met, {len(partial)} partial, {len(unmet)} unmet"
    if unmet:
        reason += " | gaps: " + ", ".join(a["requirement"][:34] for a in unmet[:4])
    if blockers:
        reason += " | " + "; ".join(blockers)

    return {
        "verdict": verdict, "score": round(score, 3),
        "assessments": assessments, "blockers": blockers, "reason": reason,
    }


def format_report(fit: dict) -> str:
    mark = {"Strong": "++", "Possible": "+", "Stretch": "~", "Skip": "--"}[fit["verdict"]]
    lines = [f"  [{mark}] {fit['verdict']} ({fit['score']:.0%}) — {fit['reason']}"]
    for a in fit.get("assessments", []):
        if a.get("status") == "unmet":
            lines.append(f"      unmet: {a['requirement'][:74]}")
    return "\n".join(lines)
