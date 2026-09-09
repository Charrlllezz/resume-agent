#!/usr/bin/env python3
"""
Role families: when is a differently-worded title the same job?

A saved job that is not on the board has two very different explanations. The
role is gone, or it is posted under another name -- "GTM Engineer" as "Forward
Deployed Engineer", "Solutions Engineer, AI Agent" as "Senior Forward Deployed
Engineer (AI Agent)". String similarity cannot tell those apart: it scores the
real equivalent at 69% and an unrelated backend role at 69% too.

So families are enumerated, not inferred. A wrong suggestion costs a wasted
application, which is worse than a missed one, so this stays deliberately
narrow and reports candidates for a human to accept rather than resolving them
silently.
"""
import re

FAMILIES = {
    # Hyphenation and house style vary more than the job does: "Go-to-Market
    # Engineer", "AI Ops Engineer - GTM", "Deployment Strategist" are all the
    # same work as "GTM Engineer". Ten of 59 resolved roles fell outside the
    # families on the first pass, every one of them a real match.
    "gtm_eng": ("gtm engineer", "gtm systems", "go to market engineer",
                "deployment strategist", "deployment architect", "ai ops",
                "marketing engineer", "transformation architect",
                "gtm enablement", "enablement manager", "deal desk",
                "ai deployment", "growth engineer", "forward deployed",
                "sales engineer", "solutions engineer", "solution engineer",
                "solutions consultant", "solutions architect", "sales solutions",
                "delivery engineer", "success engineer", "ai agent builder",
                "agent builder", "solutions developer", "technical account",
                "field engineer", "deployment engineer", "revenue operations",
                "revops", "gtm ai", "applied ai engineer", "customer engineer"),
    "cs": ("customer success", "client partner", "account manager",
           "implementation", "professional services", "customer architect",
           "onboarding", "success manager", "engagement manager",
           "technical account manager", "customer experience"),
}

SENIOR = ("senior", "sr", "staff", "principal", "lead", "director", "head of",
          "vp", "vice president", "ii", "iii")

# Shares vocabulary with the GTM family without being the same job. Without
# this, "Solutions Engineer, AI Agent" matches "Staff Software Engineer,
# Backend" at the same score as the real equivalent.
EXCLUDE = ("software engineer", "backend", "frontend", "infrastructure",
           "research", "data engineer", "security engineer", "platform engineer",
           "machine learning engineer", "recruiter", "designer")
# "marketing" is deliberately absent: it blocked "Marketing Engineer", a real
# GTM-engineering role, while ordinary marketing titles match no family anyway.


def _flat(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def family(title: str):
    t = _flat(title)
    if any(x in t for x in EXCLUDE):
        return None
    for name, terms in FAMILIES.items():
        if any(x in t for x in terms):
            return name
    return None


def senior(title: str) -> bool:
    t = f" {_flat(title)} "
    return any(f" {s} " in t for s in SENIOR)


def similar(saved_title: str, listings: list, limit: int = 3) -> list:
    """Board listings in the same family as the saved title, best first.

    Same seniority ranks above different seniority: a saved IC role matching a
    Director posting is a real family match but not a real opportunity.
    """
    want = family(saved_title)
    if not want:
        return []
    out = []
    for text, href in listings:
        if family(text) != want:
            continue
        out.append((0 if senior(text) == senior(saved_title) else 1, text, href))
    out.sort(key=lambda r: (r[0], len(r[1])))
    return [{"title": t, "url": h, "same_level": rank == 0}
            for rank, t, h in out[:limit]]
