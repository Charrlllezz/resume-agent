#!/usr/bin/env python3
"""
Quality assurance for tailored resumes.

The tailoring step is instructed never to invent metrics, tools, or experience,
but instructions are not guarantees. Everything here is deterministic: each
bullet, number, skill, and date in the tailored output is traced back to
master_resume.json, so a fabricated claim is caught before it reaches a PDF.

Checks are grouped by severity:
  ERROR  something is wrong with the facts -- do not send this resume
  WARN   worth a look, usually a bullet the model edited rather than quoted
  INFO   context, such as how much of the ATS keyword list actually landed
"""

import re
from difflib import SequenceMatcher
from typing import NamedTuple

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"

# A bullet at or above VERBATIM is a clean quote from the master resume.
# Between EDITED and VERBATIM the model reworded it -- allowed by the prompt,
# but worth showing. Below EDITED there is no plausible source bullet.
VERBATIM = 0.92
EDITED = 0.72

# The headline is positioning, not a factual claim, so it gets a looser bar
# than a bullet. A bullet can invent work that never happened; a headline can
# only describe work the bullets already have to prove. What it CAN fake is
# seniority, so that is checked separately and exactly -- see UNEARNED below.
HEADLINE_EDITED = 0.50

# A headline may not award a rank the master resume never held. This is the
# one thing loosening the threshold would otherwise let through, and unlike
# phrasing it is checkable exactly.
UNEARNED = ("director", "vp", "vice president", "head of", "chief", "principal",
            "staff", "senior manager", "founder", "co-founder", "president")

# How many bullets each role should carry, mirroring the tailoring prompt.
# Set per role in the master resume as "bullets_expected": [low, high] -- older
# roles usually want fewer. A hardcoded table here keyed by company name looked
# fine but silently fell through to "any count is acceptable" for every company
# not in it, so the check disappeared for anyone who edited the resume.
DEFAULT_BULLETS = (3, 5)


def expected_bullets(source: dict) -> tuple:
    got = source.get("bullets_expected")
    return (got[0], got[1]) if got else DEFAULT_BULLETS

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class Issue(NamedTuple):
    level: str
    check: str
    message: str


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace so cosmetic edits don't count as drift."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _numbers(text: str) -> set:
    """Every numeric token, normalized so '1,000' and '1000' compare equal."""
    return {m.group(0).replace(",", "").rstrip(".") for m in NUMBER_RE.finditer(text)}


def _best_match(candidate: str, pool: list) -> tuple:
    """Return the (ratio, source) from pool that best matches candidate."""
    best_ratio, best_source = 0.0, None
    target = _norm(candidate)
    for source in pool:
        ratio = SequenceMatcher(None, target, _norm(source)).ratio()
        if ratio > best_ratio:
            best_ratio, best_source = ratio, source
    return best_ratio, best_source


def _flatten(by_track) -> list:
    """Master resume pools are keyed by track; a hybrid role may draw from both."""
    if isinstance(by_track, dict):
        out = []
        for value in by_track.values():
            out.extend(value if isinstance(value, list) else [value])
        return out
    return list(by_track)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

# Seniority markers, ranked. A title may be reworded to match a posting's
# vocabulary, but climbing this ladder turns tailoring into a false claim.
SENIORITY_RANKS = (
    (0, ("intern", "associate", "assistant", "junior", "jr")),
    (2, ("senior", "sr", "sr.")),
    (3, ("staff", "principal", "lead")),
    (4, ("manager", "head of", "director")),
    (5, ("vp", "svp", "evp", "vice president", "chief", "president", "partner")),
)
IC_RANK = 1

# Words too generic to prove two titles describe the same job.
TITLE_STOPWORDS = {"and", "the", "for", "new", "of", "senior", "lead"}


def _seniority(title: str) -> int:
    # Punctuation must not hide a marker: " manager " never matches "Manager,",
    # which let "Director, Solutions" read as individual-contributor.
    padded = " " + re.sub(r"[^a-z0-9]+", " ", _norm(title)).strip() + " "
    found = [
        level
        for level, markers in SENIORITY_RANKS
        for marker in markers
        if f" {marker} " in padded
    ]
    return max(found) if found else IC_RANK


def _anchors(title: str) -> set:
    """Significant words -- two titles sharing none are different jobs."""
    return {
        w for w in re.findall(r"[a-z]+", _norm(title))
        if len(w) > 2 and w not in TITLE_STOPWORDS
    }


def _check_title(company: str, title: str, masters: list) -> list:
    if title in masters:
        return []

    ratio, closest = _best_match(title, masters)
    closest = closest or ""

    claimed, actual = _seniority(title), _seniority(closest)
    if claimed > actual:
        return [Issue(
            ERROR, "roles",
            f"{company}: title {title!r} claims more seniority than {closest!r}",
        )]

    if not (_anchors(title) & _anchors(closest)) and ratio < 0.5:
        return [Issue(
            ERROR, "roles",
            f"{company}: title {title!r} is a different role than {closest!r} "
            f"({ratio:.0%} match)",
        )]

    return [Issue(
        INFO, "roles",
        f"{company}: title reworked to {title!r} (master: {closest!r})",
    )]


# Words too common to prove a bullet came from a given source.
BULLET_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "was", "were",
    "are", "our", "all", "new", "not", "but", "its", "his", "her", "they",
    "them", "then", "than", "who", "what", "when", "how", "via", "per",
}


def _content_words(text: str) -> set:
    return {
        w for w in re.findall(r"[a-z0-9$%+.]+", _norm(text))
        if len(w) > 2 and w not in BULLET_STOPWORDS
    }


def _containment(candidate: str, source: str) -> float:
    """Share of the candidate's content words that appear in the source.

    Sequence similarity punishes clause reordering, but reordering a bullet to
    lead with its outcome is explicitly allowed -- the facts are unchanged.
    Containment ignores order while still catching invented content: any word
    the candidate adds drives the score down.
    """
    words = _content_words(candidate)
    if not words:
        return 0.0
    return len(words & _content_words(source)) / len(words)


def _best_bullet_match(bullet: str, pool: list) -> tuple:
    """Best (score, source) by sequence similarity or content containment."""
    best_score, best_source = 0.0, None
    for source in pool:
        score = max(
            SequenceMatcher(None, _norm(bullet), _norm(source)).ratio(),
            _containment(bullet, source),
        )
        if score > best_score:
            best_score, best_source = score, source
    return best_score, best_source


def check_structure(tailored: dict) -> list:
    issues = []
    for field in ("headline", "experience", "skills"):
        if field not in tailored:
            issues.append(Issue(ERROR, "structure", f"missing top-level field: {field}"))
    for i, role in enumerate(tailored.get("experience", [])):
        for field in ("company", "title", "location", "dates", "bullets"):
            if not role.get(field):
                issues.append(
                    Issue(ERROR, "structure", f"experience[{i}] missing {field}")
                )
    return issues


def check_roles(tailored: dict, resume: dict) -> list:
    """Company, dates, location, and title must come from the master resume."""
    issues = []
    master = {r["company"]: r for r in resume["experience"]}

    for role in tailored.get("experience", []):
        company = role.get("company", "")
        source = master.get(company)
        if not source:
            issues.append(
                Issue(ERROR, "roles", f"'{company}' is not in the master resume")
            )
            continue

        for field in ("dates", "location"):
            if role.get(field, "") != source[field]:
                issues.append(Issue(
                    ERROR, "roles",
                    f"{company}: {field} altered -- "
                    f"got {role.get(field)!r}, master says {source[field]!r}",
                ))

        titles = _flatten(source["titles"])
        issues += _check_title(company, role.get("title", ""), titles)

    covered = {r.get("company") for r in tailored.get("experience", [])}
    for company in master:
        if company not in covered:
            issues.append(Issue(WARN, "roles", f"{company} dropped from output"))
    return issues


def check_bullets(tailored: dict, resume: dict) -> list:
    """Every bullet must trace to a master bullet, and introduce no new numbers."""
    issues = []
    master = {r["company"]: r for r in resume["experience"]}

    for role in tailored.get("experience", []):
        company = role.get("company", "")
        source = master.get(company)
        if not source:
            continue

        pool = _flatten(source["bullets"])
        bullets = role.get("bullets", [])

        low, high = expected_bullets(source)
        if not low <= len(bullets) <= high:
            issues.append(Issue(
                WARN, "bullets",
                f"{company}: {len(bullets)} bullets, expected {low}-{high}",
            ))

        seen = set()
        for bullet in bullets:
            key = _norm(bullet)
            if key in seen:
                issues.append(
                    Issue(WARN, "bullets", f"{company}: duplicated bullet -- {bullet[:60]}...")
                )
            seen.add(key)

            ratio, matched = _best_bullet_match(bullet, pool)
            if ratio < EDITED:
                issues.append(Issue(
                    ERROR, "bullets",
                    f"{company}: no source bullet (best {ratio:.0%}) -- {bullet[:80]}...",
                ))
                continue

            if ratio < VERBATIM:
                issues.append(Issue(
                    WARN, "bullets",
                    f"{company}: reworded ({ratio:.0%}) -- {bullet[:70]}...",
                ))

            # The dangerous edit is a new number, not a new adjective.
            invented = _numbers(bullet) - _numbers(matched or "")
            if invented:
                issues.append(Issue(
                    ERROR, "metrics",
                    f"{company}: metric not in master resume: "
                    f"{', '.join(sorted(invented))} -- {bullet[:70]}...",
                ))
    return issues


def check_skills(tailored: dict, resume: dict) -> list:
    issues = []
    known = set()
    for pool in resume["skills"].values():
        for group in pool.values():
            known.update(_norm(s) for s in group)

    for section, skills in tailored.get("skills", {}).items():
        for skill in skills:
            if _norm(skill) in known:
                continue
            ratio, closest = _best_match(skill, list(known))
            level = WARN if ratio >= 0.85 else ERROR
            issues.append(Issue(
                level, "skills",
                f"{section}: {skill!r} is not in the master skills pool "
                f"(closest {closest!r}, {ratio:.0%})",
            ))
    return issues


def check_headline(tailored: dict, resume: dict) -> list:
    headline = tailored.get("headline", "")
    if not headline:
        return [Issue(ERROR, "headline", "no headline")]
    pool = _flatten(resume["headlines"])
    issues = []

    flat = f" {re.sub(r'[^a-z0-9]+', ' ', _norm(headline)).strip()} "
    titles = [t for role in resume["experience"]
              for t in _flatten(role.get("titles", {}))]
    master = _norm(" ".join(pool + titles))
    for rank in UNEARNED:
        if f" {rank} " in flat and rank not in master:
            issues.append(Issue(
                ERROR, "headline",
                f"headline claims {rank!r}, which appears in no master headline "
                f"or title",
            ))

    ratio, closest = _best_match(headline, pool)
    if ratio < VERBATIM:
        level = WARN if ratio >= HEADLINE_EDITED else ERROR
        issues.append(Issue(
            level, "headline",
            f"headline is {ratio:.0%} match to master (closest {closest!r})",
        ))
    return issues


def check_render(html: str, resume: dict) -> list:
    """Catch corruption introduced between the JSON and the page."""
    issues = []
    if not html:
        return issues

    leaks = re.findall(r"&#x?\w{0,6};?", html)
    stray = [x for x in leaks if x not in ("&#183;",)]
    if stray:
        issues.append(Issue(
            ERROR, "render",
            f"HTML entity leaked into visible text ({len(stray)}x, e.g. {stray[0]!r})",
        ))

    if html.count("<span") != html.count("</span>"):
        issues.append(Issue(
            ERROR, "render",
            f"unbalanced spans: {html.count('<span')} open, {html.count('</span>')} close",
        ))

    for field in ("name", "email", "phone"):
        if resume["contact"][field] not in html:
            issues.append(Issue(ERROR, "render", f"contact {field} missing from page"))

    text = re.sub(r"<[^>]+>", " ", html)
    words = len(text.split())
    if words < 200:
        issues.append(Issue(WARN, "render", f"page looks thin ({words} words)"))
    return issues


# Interview feedback: too many building stories, not enough customer stories.
# Reported every run so the balance stays visible rather than drifting silently.
BUILDING_RE = re.compile(
    r"\b(built|build|architected|automated|automation|deployed|engineered|"
    r"wired|designed|stood up|implemented|shipped)\b", re.I
)
# Plural-tolerant on purpose. "Built trusted relationships with CMOs ... across
# enterprise accounts" was scored building-led -- the most customer-facing
# bullet in the Vial pool -- because the pattern matched "relationship" but not
# "relationships", and "account team" but not "accounts". That single miss
# produced five of six lead-bullet warnings in a sweep.
CUSTOMER_RE = re.compile(
    r"\b(customer\w*|client\w*|accounts?|account team|stakeholder\w*|"
    r"executive\w*|renewal\w*|onboard\w*|adoption|retention|relationship\w*|"
    r"discovery|partnered|demo\w*|quota|revenue|pipeline|C-level)\b", re.I
)


def check_story_balance(tailored: dict) -> list:
    """How many bullets lead with a customer outcome vs. a thing that was built."""
    leads, building, customer = [], 0, 0
    for role in tailored.get("experience", []):
        bullets = role.get("bullets", [])
        if bullets:
            leads.append((role.get("company", ""), bullets[0]))
        for b in bullets:
            if BUILDING_RE.search(b):
                building += 1
            if CUSTOMER_RE.search(b):
                customer += 1

    issues = [Issue(
        INFO, "balance",
        f"{customer} customer-outcome bullets vs {building} building-led",
    )]

    build_leads = [c for c, b in leads if BUILDING_RE.search(b) and not CUSTOMER_RE.search(b)]
    if build_leads:
        issues.append(Issue(
            WARN, "balance",
            f"lead bullet is building-led at: {', '.join(build_leads)} "
            f"-- a reviewer reads these first",
        ))
    return issues


def check_ats(tailored: dict, analysis: dict, html: str) -> list:
    """Informational: how many of the posting's keywords actually made the page."""
    keywords = analysis.get("ats_keywords", [])
    if not keywords:
        return []
    haystack = _norm(re.sub(r"<[^>]+>", " ", html) if html else str(tailored))
    hits = [k for k in keywords if _norm(k) in haystack]
    missed = [k for k in keywords if k not in hits]
    issues = [Issue(
        INFO, "ats",
        f"{len(hits)}/{len(keywords)} ATS keywords present ({len(hits)/len(keywords):.0%})",
    )]
    if missed:
        issues.append(Issue(INFO, "ats", f"not present: {', '.join(missed[:8])}"))
    return issues


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def verify(tailored: dict, resume: dict, analysis: dict, html: str = "") -> list:
    issues = []
    issues += check_structure(tailored)
    issues += check_roles(tailored, resume)
    issues += check_bullets(tailored, resume)
    issues += check_skills(tailored, resume)
    issues += check_headline(tailored, resume)
    issues += check_render(html, resume)
    issues += check_story_balance(tailored)
    issues += check_ats(tailored, analysis, html)
    return issues


def format_report(issues: list) -> str:
    symbol = {ERROR: "✗", WARN: "⚠", INFO: "·"}
    counts = {level: sum(1 for i in issues if i.level == level) for level in (ERROR, WARN, INFO)}

    lines = []
    for level in (ERROR, WARN, INFO):
        for issue in [i for i in issues if i.level == level]:
            lines.append(f"  {symbol[level]} [{issue.check}] {issue.message}")

    if counts[ERROR]:
        verdict = f"FAILED — {counts[ERROR]} error(s), {counts[WARN]} warning(s)"
    elif counts[WARN]:
        verdict = f"PASSED with {counts[WARN]} warning(s)"
    else:
        verdict = "PASSED — every bullet, metric, and skill traced to the master resume"

    lines.append(f"\n  {verdict}")
    return "\n".join(lines)


def has_errors(issues: list) -> bool:
    return any(i.level == ERROR for i in issues)
