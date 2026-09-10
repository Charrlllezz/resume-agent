#!/usr/bin/env python3
"""
Resume tailoring agent — takes a job posting and outputs a styled scroll PDF + HTML.

Usage:
  python tailor_resume.py --url https://jobs.example.com/posting
  python tailor_resume.py --file job_posting.txt
  python tailor_resume.py --text "paste short job posting here"
  cat job_posting.txt | python tailor_resume.py
"""

import anthropic
import argparse
import signal
from contextlib import contextmanager
import csv
import html as html_lib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import fit
import qa
import usage

_HERE = Path(__file__).parent

# Your real resume is gitignored; the fictional example is committed so a fresh
# clone runs (and its tests pass) before you have written anything personal.
# Copy the example to master_resume.json and edit that -- never edit the
# example in place, or your own history will carry your details.
MASTER_RESUME = (_HERE / "master_resume.json" if (_HERE / "master_resume.json").exists()
                 else _HERE / "master_resume.example.json")
MODEL = "claude-opus-5"

# Ordered most-specific-first: alternation is greedy in order, so "1:48" and
# "1.5%" must be tried before the bare-number branch would grab just "1".
# The surrounding boundaries keep digits inside words (n8n, H1) unmatched, and
# the year guard keeps "H1 2023" from being styled as a metric.
METRIC_RE = re.compile(
    r"(?<![\w.$#])"
    r"("
    r"\$\d[\d,]*(?:\.\d+)?\s*[-–]\s*\$?\d[\d,]*(?:\.\d+)?\s*[KMB]?\+?"  # $5-20M
    r"|\$\d[\d,]*(?:\.\d+)?\s*[KMB]?\+?"        # $204K, $2.1M
    r"|#\d+"                                     # #1
    r"|\d+:\d+"                                  # 1:48
    r"|\d[\d,]*(?:\.\d+)?%\+?"                   # 40%, 1.5%, 4%+
    r"|\d[\d,]*(?:\.\d+)?[KMB]\+?"               # 2M+, 12M
    r"|\d[\d,]*(?:\.\d+)?x"                      # 2x
    r"|\d[\d,]*\+"                               # 25+, 100+
    r"|(?!(?:19|20)\d{2}(?!\d))\d[\d,]*"         # bare count, but never a year
    r")"
    r"(?!\w)"
)


def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


load_env()


# ---------------------------------------------------------------------------
# Job fetching
# ---------------------------------------------------------------------------

# Job boards differ in where they put the posting; try the most specific
# container first and fall back to the whole document.
JOB_SELECTORS = [
    "#content",                        # Greenhouse
    ".job__description",               # Greenhouse (newer)
    '[class*="_descriptionText"]',     # Ashby
    ".ashby-job-posting-brief",        # Ashby
    ".posting-page",                   # Lever
    ".description__text",              # LinkedIn
    '[data-automation-id="jobPostingPage"]',  # Workday
    "main",
    "body",
]

STRIP_TAGS_RE = re.compile(
    r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", re.I | re.S
)

MAX_POSTING_CHARS = 20000


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_POSTING_CHARS]


def fetch_url(url: str) -> str:
    """Render the posting in Chromium and pull its visible text.

    Most boards (Ashby, Lever, LinkedIn, Workday) are JS-rendered, and the ones
    that aren't bury the posting under inline CSS. innerText sidesteps both:
    it runs after hydration and never returns script or style contents.
    """
    try:
        return _fetch_rendered(url)
    except Exception as e:
        print(f"  \u26a0  Rendered fetch failed ({type(e).__name__}: {e}) \u2014 falling back to raw HTML")
        return _fetch_raw(url)


def _fetch_rendered(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # some boards keep a socket open; the DOM is usually ready

            best = ""
            for selector in JOB_SELECTORS:
                try:
                    el = page.query_selector(selector)
                except Exception:
                    continue
                if not el:
                    continue
                text = _clean(el.inner_text())
                # A real posting is substantial; a shell page is not.
                if len(text) > len(best):
                    best = text
                if len(best) > 1500:
                    break
            return best
        finally:
            browser.close()


def _fetch_raw(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", errors="replace")
    raw = STRIP_TAGS_RE.sub(" ", raw)
    return _clean(re.sub(r"<[^>]+>", " ", raw))


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

# Claude Opus 5 list pricing, USD per million tokens.
# Token accounting lives in usage.py -- it must not sit in a module that can
# also be __main__, or a second copy of it keeps its own totals. These names
# are kept so existing callers (and anyone's fork) do not break.
PRICE_IN, PRICE_OUT = usage.PRICE_IN, usage.PRICE_OUT
PRICE_CACHE_WRITE, PRICE_CACHE_READ = usage.PRICE_CACHE_WRITE, usage.PRICE_CACHE_READ

usage_scope = usage.scope
record_usage = usage.record
usage_cost = usage.cost
usage_line = usage.line


def usage_report() -> str:
    return usage.line(usage.now())


def extract_json(response) -> dict:
    """Pull the text blocks out of a response and parse them as JSON.

    Adaptive thinking is on by default, so content[0] is a thinking block --
    never index the content list positionally.
    """
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Asking for "ONLY JSON" gets JSON almost always, and occasionally JSON
        # followed by a sentence explaining it. Losing a whole paid run to a
        # trailing pleasantry is a bad trade, so find the object and take it.
        return json.loads(_first_json_object(raw))


def _first_json_object(text: str) -> str:
    """The first balanced {...}, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the response")
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unterminated JSON object in the response")


# Default descriptions for the two tracks this started with. A resume may name
# its tracks anything -- an uploaded one usually has just one -- so these are a
# fallback for those two names, not a fixed list. Put a "tracks" block in the
# master resume to describe your own.
TRACK_HINTS = {
    "gtm": "GTM Engineering, RevOps, Sales Ops, Growth Engineering, Marketing Ops, Automation",
    "cs": "Customer Success, Implementation, Solutions Engineering, Account Management, Post-Sales",
}


def tracks_for(resume: dict) -> list:
    """The track names this resume has actual content for, in a stable order.

    Derived from the bullet pools, not from headlines. Headlines can carry
    presentational variants with no bullets behind them -- "technical" is one
    in the sample resume -- and offering the model a track it cannot then
    select any experience for produces an empty resume.
    """
    found = set()
    for role in resume.get("experience", []):
        bullets = role.get("bullets")
        if isinstance(bullets, dict):
            found.update(k for k, v in bullets.items() if v)
    return sorted(found)


def track_block(resume: dict) -> tuple:
    """(the JSON union for the prompt, the definitions beneath it).

    These were hardcoded to gtm | cs | hybrid, which quietly described one
    person's resume to everybody else's model -- and a resume ingested from an
    uploaded PDF has a single track that is neither.
    """
    names = tracks_for(resume)
    described = resume.get("tracks", {})
    lines = [f"- {n}: {described.get(n) or TRACK_HINTS.get(n, n)}" for n in names]
    options = names + (["hybrid"] if len(names) > 1 else [])
    if len(names) > 1:
        lines.append("- hybrid: role that draws roughly equally on more than one of the above")
    return " | ".join(f'"{o}"' for o in options), "\n".join(lines)


def analyze_job(client: anthropic.Anthropic, job_posting: str, resume: dict) -> dict:
    track_options, track_defs = track_block(resume)
    response = send(
        client,
        model=MODEL,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"""Analyze this job posting and return ONLY a valid JSON object (no markdown, no explanation) with these exact fields:

{{
  "track": {track_options},
  "company": "the hiring company's name as written in the posting, or empty string if not stated",
  "role_title": "exact job title from the posting",
  "required_skills": ["list of must-have skills/tools explicitly mentioned"],
  "preferred_skills": ["list of nice-to-have skills mentioned"],
  "ats_keywords": ["exact terms to mirror verbatim in the resume for ATS matching"],
  "role_flavor": "automation_builder" | "platform_developer" | "software_engineer" | "strategy_ops" | "customer_facing",
  "seniority": "ic" | "senior" | "lead" | "manager",
  "company_stage": "startup" | "growth" | "enterprise" | "unknown",
  "tone": "technical" | "relationship" | "hybrid",
  "key_themes": ["2-4 short phrases describing what this role prioritizes most"],
  "comp_range": "salary range as written in the posting, or empty string if not listed"
}}

role_flavor definitions — "GTM Engineer" is an unstandardized title covering
very different jobs, so classify by what the posting actually asks you to DO,
not by the title:
- automation_builder: wires GTM tools together, builds workflows and AI agents,
  owns the stack (Clay, n8n, Zapier, HubSpot, MCP, scripting). Ships automation.
- platform_developer: develops ON a platform, usually Salesforce — Apex, Flows,
  Lightning Web Components, governor limits, CPQ, quote-to-cash internals.
- software_engineer: builds applications — frontend/backend frameworks, APIs,
  services, CI/CD, unit testing. A SWE role with GTM as the domain.
- strategy_ops: analysis, forecasting, reporting, territory and funnel design.
- customer_facing: owns customer relationships, renewals, onboarding, demos.

track definitions:
{track_defs}

Job posting:
{job_posting}"""
        }]
    )
    record_usage(response)
    return extract_json(response)


def bullet_spec(resume: dict) -> str:
    """"4-5 for Northwind AI, 3-4 for Contoso Data, ..." built from the resume.

    This instruction used to name five companies literally, which meant the
    prompt described one person's history to everyone else's model. It has to
    come from the resume in hand, and it must agree with qa.expected_bullets --
    otherwise the prompt asks for a count QA then flags.
    """
    parts = []
    for role in resume["experience"]:
        low, high = qa.expected_bullets(role)
        parts.append(f"{low}-{high} for {role['company']}")
    return ", ".join(parts)


def tailor_resume(client: anthropic.Anthropic, resume: dict, analysis: dict, job_posting: str) -> dict:
    resume_block = json.dumps(resume, indent=2)
    analysis_block = json.dumps(analysis, indent=2)

    response = send(
        client,
        model=MODEL,
        max_tokens=16000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "You are a resume tailoring expert. "
                        "Here is the master resume containing all bullet variations, "
                        "title options, headline options, and skills pools:\n\n"
                        f"{resume_block}"
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"""Job analysis:
{analysis_block}

Job posting excerpt (for context):
{job_posting[:3000]}

Tailor the resume for this specific role. Return ONLY a valid JSON object with these exact fields:

{{
  "headline": "selected or lightly adapted headline string",
  "experience": [
    {{
      "company": "company name",
      "title": "master title for this track, optionally reworked within the rules below",
      "location": "location string",
      "dates": "dates string",
      "bullets": ["3-5 selected bullet strings"]
    }}
  ],
  "skills": {{
    "Section Name": ["skill1", "skill2"]
  }},
  "changes_summary": ["bullet explaining each key tailoring decision made"]
}}

Rules:
- Select this many bullets per role: {bullet_spec(resume)}
- Choose bullets that best match required_skills, ats_keywords, and key_themes from the analysis
- Balance customer outcomes against building. Roughly half the bullets in each role should
  lead with what changed for a customer, an account team, or the business — not with the
  system that was built. The FIRST bullet of every role must be a customer or business
  outcome whenever the pool contains one; a reviewer reads those first. Where a bullet
  covers both, order it outcome-first and let the system be the mechanism ("cut X for the
  account team by building Y", not "built Y"). Do not drop technical substance to do this,
  and do not reword past what the source bullet actually says. You may reorder a bullet's
  clauses; you may not introduce a fact, tool, or number the source bullet does not contain.
- For a hybrid track, blend bullets from the tracks the resume carries, across roles
- Mirror ATS keywords verbatim in bullets where they appear naturally — do not force them
- Only lightly edit a bullet to add a missing keyword if it genuinely belongs; otherwise use it as-is
- Never fabricate metrics, tools, or experience not present in the master resume
- Titles may be reworked to better match the posting's vocabulary, but only within reason:
  keep the same seniority (never add Senior, Lead, Manager, Head, Director, VP, or Chief
  if the master title lacks it) and the same underlying function. Reword the framing, not
  the job — "GTM Engineer" may become "GTM Systems Engineer", never "Director of GTM".
  If no honest rewording helps, use the master title verbatim.
- Pick the headline that best matches the company stage and role tone
- Adjust skills sections: emphasize what matches required_skills; drop or demote irrelevant items
- Copy every skill string VERBATIM from the master skills pools. Reorder them, regroup them,
  rename a section heading — but never reword a skill itself. "LLMs" stays "LLMs"; it does not
  become "LLMs / language model integration". A skill you cannot copy exactly does not go in.
- changes_summary should explain your 3-5 most impactful tailoring decisions""",
                },
            ],
        }]
    )

    record_usage(response)
    return extract_json(response)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

RESUME_CSS = """
  /* The token values are written by style_block(); everything below is
     expressed in terms of them so a detected style changes the whole sheet. */

  * { box-sizing: border-box; }

  html, body {
    margin: 0; padding: 0;
    background: var(--canvas);
    background-image: radial-gradient(var(--canvas-dot) 1px, transparent 1px);
    background-size: 22px 22px;
    color: var(--ink);
    font-family: var(--font-body);
    -webkit-font-smoothing: antialiased;
  }

  .page { max-width: 780px; margin: 0 auto; padding: 56px 32px 80px; }

  .hero { margin-bottom: 40px; }
  .hero h1 {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: clamp(32px, 6vw, 44px);
    letter-spacing: -0.01em;
    margin: 0 0 6px;
  }
  .hero .tagline {
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.09em;
    font-size: 12.5px;
    color: var(--accent);
    font-weight: 500;
    margin-bottom: 18px;
  }
  .contact-row {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--ink-soft);
    display: flex;
    flex-wrap: wrap;
    gap: 0 10px;
    align-items: center;
  }
  .contact-row .sep { color: var(--line); }

  .section-label {
    font-family: var(--font-heading);
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-size: 11.5px;
    color: var(--ink-soft);
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 44px 0 20px;
  }
  .section-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--line);
  }

  .timeline { position: relative; padding-left: 30px; }
  .timeline::before {
    content: '';
    position: absolute;
    left: 5px; top: 10px; bottom: 10px;
    width: 2px;
    background-image: linear-gradient(var(--line) 60%, transparent 0%);
    background-size: 2px 10px;
    background-repeat: repeat-y;
  }

  .node { position: relative; margin-bottom: 26px; }
  .node:last-child { margin-bottom: 0; }
  .port {
    position: absolute;
    left: -30px; top: 7px;
    width: 12px; height: 12px;
    border-radius: 50%;
    background: var(--surface);
    border: 2px solid var(--ink-soft);
  }
  .node[data-status="current"] .port {
    background: var(--accent);
    border-color: var(--accent);
    box-shadow: 0 0 0 4px var(--accent-soft);
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px 20px;
  }
  .card-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: 8px; row-gap: 2px;
    margin-bottom: 8px;
  }
  .card-head h3 {
    font-family: var(--font-heading);
    font-size: 16.5px; font-weight: 600; margin: 0;
  }
  .card-head .at { font-weight: 600; color: var(--accent); font-size: 14.5px; }
  .card-head .at::before { content: '@ '; }
  .card-head .loc { font-size: 12.5px; color: var(--ink-soft); }
  .card-head .dates {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 12px; color: var(--ink-soft); white-space: nowrap;
  }

  .bullets { margin: 0; padding-left: 18px; }
  .bullets li { font-size: 13.8px; line-height: 1.55; color: var(--ink); margin-bottom: 7px; }
  .bullets li:last-child { margin-bottom: 0; }
  .bullets .m {
    font-family: var(--font-mono);
    font-weight: 600;
    color: var(--data);
    background: var(--data-soft);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 12.6px;
  }

  .skills { display: flex; flex-direction: column; gap: 14px; }
  .skill-group { display: flex; align-items: flex-start; gap: 14px; flex-wrap: wrap; }
  .skill-label {
    font-family: var(--font-mono);
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-soft);
    width: 140px; flex-shrink: 0; padding-top: 5px;
  }
  .pills { display: flex; flex-wrap: wrap; gap: 7px; flex: 1; }
  .pill {
    font-family: var(--font-mono);
    font-size: 12px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 4px 11px;
    color: var(--ink);
  }

  .education {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 14px 20px;
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 13.8px;
  }
  .education .degree { font-weight: 600; }
  .education .school { color: var(--ink-soft); }

  @media print {
    html, body {
      background: var(--canvas) !important;
      background-image: radial-gradient(var(--canvas-dot) 1px, transparent 1px) !important;
      background-size: 22px 22px !important;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    .page { max-width: 100%; padding: 12px 24px; }
    .hero { margin-bottom: 11px; }
    .hero h1 { font-size: 26px; }
    .hero .tagline { margin-bottom: 5px; }
    .section-label { margin: 11px 0 7px; }
    .timeline { padding-left: 25px; }
    .timeline::before { left: 5px; }
    .node { margin-bottom: 7px; break-inside: avoid; page-break-inside: avoid; }
    .port { left: -25px; top: 5px; width: 9px; height: 9px; }
    .card { padding: 6px 13px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .card-head { margin-bottom: 2px; }
    .card-head h3 { font-size: 13px; }
    .card-head .at { font-size: 11.5px; }
    .card-head .loc { font-size: 10.2px; }
    .card-head .dates { font-size: 10.2px; }
    .bullets li { font-size: 10.6px; line-height: 1.24; margin-bottom: 1.5px; }
    .bullets .m { font-size: 10px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .skills { gap: 5px; }
    .skill-group { gap: 10px; }
    .skill-label { font-size: 9.5px; width: 116px; padding-top: 2px; }
    .pills { gap: 4px; }
    .pill { font-size: 9.6px; padding: 1.5px 7px; }
    .education { padding: 8px 14px; font-size: 11.3px; }
    .port, .card, .education, .pill { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
"""


# A number following one of these is part of a proper noun ("Fortune 500",
# "S&P 500"), not an achievement worth styling as a metric.
NON_METRIC_PREFIXES = (
    "fortune ",
    "s&p ",
    "s&amp;p ",  # the escaped form, since we match against escaped text
    "forbes ",
    "inc. ",
    "deloitte fast ",
    "office ",
    "series ",
)


def highlight_metrics(text: str) -> str:
    # quote=False leaves apostrophes as-is. With quote=True an apostrophe became
    # "&#x27;", and METRIC_RE then matched the "27" inside the entity.
    escaped = html_lib.escape(text, quote=False)

    def style(match: re.Match) -> str:
        preceding = escaped[: match.start()].lower()
        if preceding.endswith(NON_METRIC_PREFIXES):
            return match.group(0)
        return f'<span class="m">{match.group(1)}</span>'

    return METRIC_RE.sub(style, escaped)


# ---------------------------------------------------------------------------
# Style: make the tailored resume look like the one that was uploaded
# ---------------------------------------------------------------------------

# The dotted canvas, the timeline and the cards are this project's signature.
# No uploaded resume has them. Applying them to somebody's document because it
# was "designed" swaps their design for mine, which is the exact thing this
# feature exists to stop -- a two-column resume lost its sidebar AND gained a
# timeline it never had.
#
# So: any resume that came from a document is rendered on neutral structure, in
# its own type and its own colour. The house decoration is what you get when
# there is no source document to match.
NEUTRAL_CSS = """
  html, body { background: #FFFFFF; background-image: none; }
  .page { max-width: 760px; padding: 40px 30px 60px; }
  .timeline { padding-left: 0; }
  .timeline::before { display: none; }
  .port { display: none; }
  .card { background: transparent; border: 0; border-radius: 0;
          box-shadow: none; padding: 0 0 2px 0; }
  .node { margin-bottom: 15px; }
  .card-head .at::before { color: var(--line); }
  /* Plain resumes still use their one colour somewhere, and section headings
     are where it almost always is. Sending everything to --ink threw a
     detected accent away entirely: a resume with blue headings came back
     black, which is not "matching" it. Where the accent is #000000 this
     renders black anyway, which is the right answer for that resume. */
  .section-label { color: var(--accent); letter-spacing: .10em; }
  .section-label::after { background: var(--accent-2); }
  .pill { background: transparent; border: 0; padding: 0 2px 0 0; color: var(--ink); }
  .pill:not(:last-child)::after { content: ","; color: var(--ink-soft); }
  .pills { gap: 3px; }
  .education { background: transparent; border: 0; border-radius: 0;
               box-shadow: none; padding: 4px 0 0 0; }
  .skill-group { padding: 2px 0; }
"""

# Structure is one question; how much colour a resume uses is another. A plain
# document gets its emphasis in weight rather than in tinted chips.
PLAIN_CSS = """
  .bullets .m { background: transparent; color: inherit; padding: 0;
                font-family: inherit; font-weight: 600; }
  .card-head .at { color: var(--ink); }
"""

COMPACT_CSS = """
  .page { padding: 30px 30px 40px; }
  .bullets li { font-size: 13px; line-height: 1.42; margin-bottom: 4px; }
  .node { margin-bottom: 12px; }
"""


def style_for(resume: dict) -> dict:
    """The detected style, with anything missing filled in.

    A missing font slot inherits from a slot that WAS supplied, not from the
    house default. Falling back to the default let this project's own IBM Plex
    Mono into a Times New Roman resume's section headings -- type the document
    never had, which is the failure this whole feature exists to avoid. It also
    matters for a style stored before a slot existed.
    """
    import style as style_mod
    given = {k: v for k, v in (resume.get("style") or {}).items() if v}
    spec = dict(style_mod.DEFAULTS)
    spec.update(given)
    # A style block at all means this resume came from a document.
    spec["from_document"] = bool(given)
    if given:
        if "font_display" not in given:
            spec["font_display"] = given.get("font_body", spec["font_display"])
        if "font_heading" not in given:
            spec["font_heading"] = spec["font_display"]
        if "font_mono" not in given:
            spec["font_mono"] = given.get("font_body", spec["font_mono"])
        if "accent_2" not in given:
            spec["accent_2"] = given.get("accent", spec["accent_2"])
    return spec


def _tint(hex_colour: str, amount: float) -> str:
    """Mix a colour towards white. Used for the soft backgrounds the template
    pairs with each accent, so a detected accent brings its own tint."""
    try:
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "#EEEEEE"
    mix = lambda c: int(c + (255 - c) * amount)
    return "#%02X%02X%02X" % (mix(r), mix(g), mix(b))


def style_block(spec: dict) -> str:
    accent, ink = spec["accent"], spec["ink"]
    plain = spec.get("chrome") == "plain"
    css = f"""
  :root {{
    --canvas: {"#FFFFFF" if plain else "#F3F4F0"};
    --canvas-dot: {"#FFFFFF" if plain else "#E1E5DE"};
    --ink: {ink};
    --ink-soft: {_tint(ink, 0.42)};
    --surface: #FFFFFF;
    --line: {_tint(ink, 0.80)};
    --accent: {accent};
    --accent-soft: {_tint(accent, 0.88)};
    --data: {accent if plain else "#9A5B1E"};
    --data-soft: {_tint(accent if plain else "#9A5B1E", 0.86)};
    --radius: {"0px" if plain else "10px"};
    --font-body: {spec["font_body"]};
    --font-display: {spec["font_display"]};
    --font-heading: {spec["font_heading"]};
    --font-mono: {spec["font_mono"]};
    --accent-2: {spec["accent_2"]};
  }}
  .hero {{ text-align: {spec.get("header_align", "left")}; }}
  .contact-row {{ justify-content: {"center" if spec.get("header_align") == "center" else "flex-start"}; }}
"""
    # from_document is set for anything that came out of an upload. It is not
    # the same question as plain-vs-designed: a colourful two-column resume is
    # "designed" and still must not inherit this project's timeline.
    if spec.get("from_document"):
        css += NEUTRAL_CSS
    if plain:
        css += PLAIN_CSS
    if spec.get("density") == "compact":
        css += COMPACT_CSS
    return css


def font_link(spec: dict) -> str:
    families = "&".join(f"family={f}" for f in spec.get("google_fonts") or [])
    if not families:
        return ""
    return ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            f'<link href="https://fonts.googleapis.com/css2?{families}&display=swap" '
            'rel="stylesheet">')


def render_html(tailored: dict, resume: dict) -> str:
    c = resume["contact"]
    edu = resume["education"]
    spec = style_for(resume)

    nodes_html = []
    for i, role in enumerate(tailored["experience"]):
        status = "current" if i == 0 else "past"
        bullets_html = "\n".join(
            f'            <li>{highlight_metrics(b)}</li>'
            for b in role["bullets"]
        )
        nodes_html.append(f"""
      <article class="node" data-status="{status}">
        <span class="port" aria-hidden="true"></span>
        <div class="card">
          <div class="card-head">
            <h3>{html_lib.escape(role["title"])}</h3>
            <span class="at">{html_lib.escape(role["company"])}</span>
            <span class="loc">{html_lib.escape(role["location"])}</span>
            <span class="dates">{html_lib.escape(role["dates"])}</span>
          </div>
          <ul class="bullets">
{bullets_html}
          </ul>
        </div>
      </article>""")

    skills_html = []
    # A resume that just lists its skills gets one group called "Skills",
    # which then prints under a section heading also called SKILLS. One of
    # them has to go.
    only_group = len(tailored["skills"]) == 1
    for section, skills in tailored["skills"].items():
        if only_group and section.strip().lower() in ("skills", "skill", ""):
            section = ""
        pills = "\n".join(
            f'            <span class="pill">{html_lib.escape(s)}</span>'
            for s in skills
        )
        skills_html.append(f"""
      <div class="skill-group">
        {f'<span class="skill-label">{html_lib.escape(section)}</span>' if section else ''}
        <div class="pills">
{pills}
        </div>
      </div>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_lib.escape(c["name"])} · Résumé</title>
{font_link(spec)}
<style>
{RESUME_CSS}
{style_block(spec)}
</style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <h1>{html_lib.escape(c["name"])}</h1>
      <div class="tagline">{html_lib.escape(tailored["headline"])}</div>
      <div class="contact-row">
        <span>{html_lib.escape(c["phone"])}</span>
        <span class="sep">&#183;</span>
        <span>{html_lib.escape(c["email"])}</span>
        <span class="sep">&#183;</span>
        <span>{html_lib.escape(c["linkedin"])}</span>
      </div>
    </header>

    <div class="section-label">Experience</div>
    <div class="timeline">
      {"".join(nodes_html)}
    </div>

    <div class="section-label">Skills</div>
    <div class="skills">
      {"".join(skills_html)}
    </div>

    <div class="section-label">Education</div>
    <div class="education">
      <span class="degree">{html_lib.escape(edu["degree"])}</span>
      <span class="school">{html_lib.escape(edu["school"])} · {html_lib.escape(edu["year"])}</span>
    </div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Google Sheets logging (via Apps Script webhook)
# ---------------------------------------------------------------------------

APPLICATIONS_CSV = Path(__file__).parent / "applications.csv"

# One row per application. Matches what the tracking sheet shows.
LOG_COLUMNS = [
    "Date Applied", "Company", "Role Title", "Track", "Stage",
    "Comp Range", "Status", "Flavor", "Fit", "Fit Score", "Fit Reason",
    "Key Themes", "ATS Keywords", "Required Skills", "Output Files",
]


def log_application(company: str, analysis: dict, pdf_path: Path, html_path: Path,
                    assessment: dict = None):
    """Append this application to the local CSV tracker.

    Local-first on purpose: this always works with no setup, and the CSV is the
    source of truth. Pushing it to Google Sheets is a separate step, because the
    Drive connector lives in a Claude session and is not reachable from here.
    """
    row = {
        "Date Applied": datetime.today().strftime("%Y-%m-%d"),
        "Company": company,
        "Role Title": analysis.get("role_title", ""),
        "Track": analysis.get("track", "").upper(),
        "Stage": analysis.get("company_stage", "").capitalize(),
        "Comp Range": analysis.get("comp_range", ""),
        "Status": "Applied",
        "Flavor": analysis.get("role_flavor", ""),
        "Fit": (assessment or {}).get("verdict", ""),
        "Fit Score": f"{(assessment or {}).get('score', 0):.0%}" if assessment else "",
        "Fit Reason": (assessment or {}).get("reason", ""),
        "Key Themes": " | ".join(analysis.get("key_themes", [])),
        "ATS Keywords": ", ".join(analysis.get("ats_keywords", [])),
        "Required Skills": ", ".join(analysis.get("required_skills", [])),
        "Output Files": f"{pdf_path.name} | {html_path.name}",
    }

    # Re-running the same posting appended a second identical row, which is
    # how two OpenAI entries ended up in the tracker.
    if APPLICATIONS_CSV.exists():
        with APPLICATIONS_CSV.open(encoding="utf-8") as f:
            for existing in csv.DictReader(f):
                if (existing.get("Company", "").strip().lower() == company.strip().lower()
                        and existing.get("Role Title", "").strip().lower()
                        == row["Role Title"].strip().lower()):
                    print(f"  Already tracked ({existing.get('Date Applied')}) — not logging again")
                    return

    is_new = not APPLICATIONS_CSV.exists()
    with APPLICATIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)

    total = sum(1 for _ in APPLICATIONS_CSV.open(encoding="utf-8")) - 1
    print(f"  Logged to {APPLICATIONS_CSV.name} ({total} application(s) tracked)")
    print("  To push to Google Sheets, ask Claude: \"sync my applications to Sheets\"")


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_scroll_pdf(html_path: Path, pdf_path: Path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 844, "height": 900})
        page.goto(f"file://{html_path.absolute()}", wait_until="networkidle")

        scroll_height = page.evaluate("document.documentElement.scrollHeight")

        page.pdf(
            path=str(pdf_path),
            width="844px",
            height=f"{scroll_height}px",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def make_client(api_key: str = ""):
    """Anthropic client bounded so one bad role cannot eat an hour.

    api_key is passed explicitly when the caller is holding someone else's --
    a hosted build takes a key per session and must never fall through to a
    key sitting in the server's own environment.

    A 240s timeout with 2 retries still let a single role run 50 minutes: the
    budget multiplies by attempts and by the number of calls per role, and
    backoff stacks on top. 120s with one retry caps a call near four minutes.

    Pair this with send(), which streams. A non-streaming request holds the
    whole generation open on one socket, which is what made these long enough
    to time out and retry in the first place.
    """
    extra = {"api_key": api_key} if api_key else {}
    return anthropic.Anthropic(timeout=120.0, max_retries=1, **extra)


@contextmanager
def deadline(seconds: int):
    """Hard wall-clock budget for one unit of work.

    Client timeouts bound a single request; they cannot bound a role that makes
    several, or one wedged somewhere other than the API. This does, whatever
    the cause, so a sweep's worst case per role is knowable.
    """
    def _fire(signum, frame):
        raise TimeoutError(f"exceeded {seconds}s budget")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def send(client, **kwargs):
    """One model call, streamed, returning the completed message.

    Streaming is the documented way to keep a long generation from tripping
    request timeouts -- these calls run to 16k max_tokens with adaptive
    thinking, which is exactly the shape that stalls unstreamed.
    """
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def main():
    parser = argparse.ArgumentParser(description="Tailor resume to a job posting")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--url", help="URL of job posting page")
    group.add_argument("--file", help="Path to plain-text job posting file")
    group.add_argument("--text", help="Job posting text")
    parser.add_argument("--company", help="Company name for tracking and filenames", default="")
    parser.add_argument("--slug", default="",
                        help="Override the filename slug. Needed when one company "
                             "has several open roles, which otherwise all write to "
                             "the same file and overwrite each other.")
    parser.add_argument("--name", default="",
                        help="Set the output filename exactly, without the "
                             "tailored_<date>_ prefix. This is the name a "
                             "recruiter sees on the attachment.")
    parser.add_argument("--no-log", "--no-sheet", dest="no_log", action="store_true",
                        help="Skip writing to applications.csv")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if QA finds a factual error")
    parser.add_argument("--no-qa", action="store_true", help="Skip the QA pass")
    args = parser.parse_args()

    # Imported here, not at module scope: pipeline imports this module, and a
    # top-level import either way would be circular.
    import pipeline

    posting_text = ""
    if args.file:
        posting_text = Path(args.file).read_text()
    elif args.text:
        posting_text = args.text
    elif not args.url and not sys.stdin.isatty():
        posting_text = sys.stdin.read()
    elif not args.url:
        parser.print_help()
        sys.exit(1)

    resume = json.loads(MASTER_RESUME.read_text())
    client = make_client()

    def show(stage, message, result):
        """Print what the CLI has always printed, driven by pipeline stages."""
        if stage == "fetch":
            print(f"Fetching job posting from {args.url}...")
        elif stage == "analyze":
            if result.posting_chars:
                print(f"  Extracted {result.posting_chars} chars")
            print("Analyzing job posting...")
        elif stage == "fit":
            a = result.analysis
            print(f"  Track:   {a['track']}")
            print(f"  Tone:    {a['tone']}")
            print(f"  Stage:   {a['company_stage']}")
            print(f"  Flavor:  {a.get('role_flavor', '?')}")
            print(f"  Themes:  {', '.join(a['key_themes'])}")
            print(f"  ATS:     {', '.join(a['ats_keywords'][:6])}")
            print("\nShould you apply?")
        elif stage == "tailor":
            if result.assessment:
                print(fit.format_report(result.assessment))
            print("\nTailoring resume...")
        elif stage == "pdf":
            print("Generating scroll PDF...")
        elif stage == "warn":
            print(f"  \u26a0  {message}")

    run = pipeline.run(resume=resume, client=client, url=args.url or "",
                       text=posting_text, company=args.company,
                       progress=show, do_qa=not args.no_qa)

    company = run.company
    if not args.company and company:
        print(f"  Company detected: {company}")

    slug = re.sub(r"[^a-z0-9]+", "_", (args.slug or company).lower()).strip("_")
    date_str = datetime.today().strftime("%Y-%m-%d")
    stem = f"tailored_{date_str}_{slug}" if slug else f"tailored_{date_str}"

    if args.name:
        html_path, pdf_path = Path(f"{args.name}.html"), Path(f"{args.name}.pdf")
    else:
        html_path, pdf_path = Path(f"{stem}.html"), Path(f"{stem}_Scroll.pdf")

    html_path.write_text(run.html)
    print(f"\nHTML written to: {html_path}")
    pdf_path.write_bytes(run.pdf)
    print(f"PDF written to:  {pdf_path}")

    print("\nUsage:")
    print(usage_line(run.usage))

    print("\nTailoring decisions:")
    for change in run.tailored.get("changes_summary", []):
        print(f"  \u2022 {change}")

    failed = False
    if not args.no_qa:
        print("\nQA:")
        print(qa.format_report(run.issues))
        failed = run.has_errors

    if not args.no_log:
        if failed:
            print("\nSkipping application log — QA failed.")
        else:
            print("\nLogging application...")
            log_application(company or "Unknown", run.analysis, pdf_path, html_path,
                            run.assessment)

    if failed and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
