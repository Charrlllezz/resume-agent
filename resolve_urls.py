#!/usr/bin/env python3
"""
Resolve saved jobs to their posting on the company's own career page.

LinkedIn job URLs sit behind a sign-in wall and carry a truncated description.
The company's own board serves the full posting and is what fetch_url already
handles well -- so resolve to that instead.

The primary route is the company's own website: open it, follow its careers
link, and read whichever ATS it actually uses. That is authoritative -- the
company tells you where its jobs are. Guessing board slugs is inference and it
is wrong in both directions: Glean's Greenhouse slug is "gleanwork", and a bad
guess can land on a real board belonging to someone else. Slug guessing is kept
only as a fallback for companies whose site cannot be reached.

Every discovered board is checked against the company name before its postings
are trusted, so a generic ATS listing cannot masquerade as the company's board.

Usage:
  python resolve_urls.py saved_jobs.tsv --out resolved.tsv
"""

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import roles

POSTING_HREF = re.compile(
    r"/jobs/\d+|/[0-9a-f]{8}-[0-9a-f-]{20,}|/j/[A-Z0-9]{6,}|/\d{7,}|"
    r"_(?:JR|R)-?\d{4,}")  # Workday

ATS_LINK = re.compile(
    r"(job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.ashbyhq\.com|"
    r"jobs\.lever\.co|myworkdayjobs\.com|careers\.smartrecruiters\.com|"
    r"apply\.workable\.com|jobs\.jobvite\.com|jobs\.rippling\.com|"
    r"comeet\.com|breezy\.hr|teamtailor\.com|recruitee\.com|"
    r"eightfold\.ai|icims\.com|bamboohr\.com)", re.I)

CAREERS_PATHS = ("/careers", "/company/careers", "/about/careers", "/jobs",
                 "/careers/open-roles", "/company/jobs", "/careers/jobs")

# Fallback only -- used when the company's own site cannot be reached.
BOARD_TEMPLATES = [
    "https://job-boards.greenhouse.io/{slug}",
    "https://jobs.ashbyhq.com/{slug}",
    "https://jobs.lever.co/{slug}",
    "https://careers.smartrecruiters.com/{slug}",
]

DOMAIN_OVERRIDES = {
    "Elastic": "elastic.co", "Toast": "toasttab.com", "Zoom": "zoom.us",
    "Notion": "notion.so", "Customer.io": "customer.io", "Apollo.io": "apollo.io",
    "1mind": "1mind.com", "Bright Data": "brightdata.com",
    "Fireworks AI": "fireworks.ai", "Trayo AI": "trayo.ai",
    "Triple Seat Software": "tripleseat.com", "Theo Ai": "theoai.com",
    "Relevance AI": "relevanceai.com", "Nooks": "nooks.ai",
    "Mendable": "mendable.ai", "Base": "base.inc", "Mesh": "meshpayments.com",
    "Berkley Hunt": "berkleyhunt.com", "groundcover": "groundcover.com",
    "Motion": "motion.com", "Faire": "faire.com",
}


_DNS = {}


def resolvable(domain: str) -> bool:
    """Does this domain exist at all? Cached DNS lookup.

    Trying careers/jobs subdomains across four TLDs multiplied the candidate
    list, and every dead guess cost an 18s browser timeout. A getaddrinfo call
    settles it in microseconds.
    """
    if domain not in _DNS:
        import socket
        try:
            socket.getaddrinfo(domain, 443)
            _DNS[domain] = True
        except OSError:
            _DNS[domain] = False
    return _DNS[domain]


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t.lower())).strip()


def company_tokens(company: str) -> set:
    stop = {"ai", "inc", "llc", "software", "the", "io", "labs", "technologies"}
    return {w for w in norm(company).split() if w not in stop and len(w) > 2}


def domains_for(company: str) -> list:
    """Domains to try, apex first, then the careers subdomain.

    Plenty of companies serve their board from careers.<domain> with no link
    to any third-party ATS -- Zoom's board is careers.zoom.us/jobs. Guessing
    only the apex domain reports those as having no board at all.
    """
    base = ([DOMAIN_OVERRIDES[company]] if company in DOMAIN_OVERRIDES else
            [f"{flat}.com", f"{flat}.io", f"{flat}.ai", f"{flat}.co"]
            if (flat := re.sub(r"[^a-z0-9]", "", company.lower())) else [])
    return base + [f"careers.{d}" for d in base] + [f"jobs.{d}" for d in base]


def slugs_for(company: str) -> list:
    flat = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
    out, seen = [], set()
    for s in (flat.replace(" ", ""), flat.replace(" ", "-"), flat.split(" ")[0]):
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def postings_belong(listings: list, company: str) -> bool:
    """Do these postings actually live under the company's own board path?

    The URL guard below is not enough, and trusting it cost 19 saved jobs.
    careers.smartrecruiters.com/<slug> returns HTTP 200 and a large generic
    global listing for ANY slug -- including one that does not exist -- so the
    URL contains the company name while every posting on the page belongs to
    someone else. Those were reported as "title not on board", which reads as a
    fact about the posting when it was a fact about the scraper.

    Every real board namespaces postings under the employer: ashbyhq.com/<co>/,
    greenhouse.io/<co>/jobs/, lever.co/<co>/. So require the postings' own path
    to carry the company, not the index URL's.
    """
    if not listings:
        return False
    tokens = {re.sub(r"[^a-z0-9]", "", t) for t in company_tokens(company)}
    if not tokens:
        return True
    hits = 0
    for _, href in listings:
        # Host included: a company that hosts its own board puts its name
        # there (careers.zoom.us/jobs/123) rather than in the path.
        seg = re.sub(r"[^a-z0-9]", "", "/".join(href.lower().split("/")[2:5]))
        hits += any(t in seg for t in tokens)
    return hits / len(listings) >= 0.6


def board_belongs_to(board_url: str, company: str) -> bool:
    """Cheap URL-shaped pre-filter, applied before a board is worth loading.

    Necessary but not sufficient -- see postings_belong for why.
    """
    tokens = company_tokens(company)
    if not tokens:
        return True
    flat = re.sub(r"[^a-z0-9]", "", board_url.lower())
    return any(re.sub(r"[^a-z0-9]", "", t) in flat for t in tokens)


def scrape_listings(page, board_url: str) -> list:
    """Return (title_text, href) pairs for postings on a board index page."""
    try:
        resp = page.goto(board_url, wait_until="domcontentloaded", timeout=20000)
        if not resp or resp.status >= 400:
            return []
        page.wait_for_timeout(3600)
        # Boards lazy-load: Greenhouse renders ~50 postings, then more on
        # scroll. Without this the list is silently truncated and a role that
        # exists reads as "not on the board".
        seen = 0
        for _ in range(12):
            pairs = page.eval_on_selector_all("a", "els => els.map(e => e.href)")
            count = sum(1 for h in pairs if h and POSTING_HREF.search(h))
            if count == seen:
                break
            seen = count
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(1200)
        pairs = page.eval_on_selector_all("a", "els => els.map(e => [e.innerText, e.href])")
    except Exception:
        return []
    return [(" ".join(t.split()), h) for t, h in pairs
            if t and t.strip() and POSTING_HREF.search(h)]


def discover_via_careers(page, company: str) -> tuple:
    """PRIMARY route: follow the company's own careers page to its real board.

    Reads the board index rather than the careers page itself. Posting links on
    a careers page often carry no title text -- an Ashby href is a bare UUID --
    so titles have to come from the board, or nothing can be matched.
    """
    for domain in domains_for(company):
        if not resolvable(domain):
            continue
        # A careers/jobs subdomain is already the careers site; deep paths on
        # it are mostly 404s that cost a page load each.
        paths = ("", "/jobs", "/careers") if domain.split(".")[0] in ("careers", "jobs") else CAREERS_PATHS
        for path in paths:
            try:
                resp = page.goto(f"https://{domain}{path}",
                                 wait_until="domcontentloaded", timeout=14000)
                if not resp or resp.status >= 400:
                    continue
                page.wait_for_timeout(4500)
                anchors = page.eval_on_selector_all(
                    "a", "els => els.map(e => [e.innerText, e.href])")
                hrefs = [h for _, h in anchors]
                hrefs += [f.url for f in page.frames] + [page.url]
            except Exception:
                continue

            # Prefer the careers page's own listing. Companies embed the full
            # role list here, while the board index lazy-loads and caps out --
            # Glean's careers page carries 117 postings, its Greenhouse board
            # renders 50. Only usable when the anchors carry title text.
            direct = [(" ".join(t.split()), h) for t, h in anchors
                      if t and t.strip() and h and ATS_LINK.search(h)
                      and POSTING_HREF.search(h)]

            ats = [h for h in hrefs if h and ATS_LINK.search(h)]
            if not ats:
                # No third-party ATS anywhere. The company may be serving its
                # own board -- take same-host posting links as the listing.
                host = re.sub(r"^https://([^/]+).*", r"\1", page.url)
                own = [(" ".join(t.split()), h) for t, h in anchors
                       if t and t.strip() and h and host in h
                       and POSTING_HREF.search(h)]
                if len(own) > 5 and postings_belong(own, company):
                    return own, f"https://{host}{path}"
                continue

            roots, seen = [], set()
            for h in ats:
                m = re.match(r"(https://[^/]+/[^/?#]+)", h)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    roots.append(m.group(1))
            for root in roots[:3]:
                if not board_belongs_to(root, company):
                    continue
                # Union the careers page's own anchors with the board index.
                # Neither alone is complete: Glean's careers page carries 117
                # postings to its board's 50, while a careers page that lists
                # only featured roles hid Baseten's "GTM Engineer" behind 88
                # postings the board index shows in full.
                listings = scrape_listings(page, root)
                merged = {h: t for t, h in direct}
                merged.update({h: t for t, h in listings})
                pairs = [(t, h) for h, t in merged.items()]
                if postings_belong(pairs, company):
                    return pairs, root
            if len(direct) > 5 and postings_belong(direct, company):
                return direct, re.match(r"(https://[^/]+/[^/?#]+)", direct[0][1]).group(1)
    return [], None


def discover_via_slug(page, company: str) -> tuple:
    """FALLBACK: guess board slugs when the company site cannot be reached."""
    for tmpl in BOARD_TEMPLATES:
        for slug in slugs_for(company):
            url = tmpl.format(slug=slug)
            if not board_belongs_to(url, company):
                continue
            listings = scrape_listings(page, url)
            if listings and postings_belong(listings, company):
                return listings, url
    return [], None


def match_title(saved_title: str, listings: list) -> tuple:
    """Match a LinkedIn title against a board listing.

    LinkedIn titles often carry a location or region the board title omits
    ("Strategic Solutions Engineer, SF Bay Area" vs "Strategic Solutions
    Engineer"), and board listings append location and employment type to the
    link text. So symmetric overlap under-scores real matches in both
    directions; a board title fully contained in the saved title is treated as
    a match, provided it is specific enough (3+ words) to mean something.
    """
    target = norm(saved_title)
    tw = set(target.split())
    best, best_url, best_text = 0.0, None, None
    for text, href in listings:
        cand = norm(text)
        cw = set(cand.split())
        score = SequenceMatcher(None, target, cand).ratio()
        if target and target in cand:
            score = max(score, 0.95)
        if tw and cw:
            overlap = tw & cw
            score = max(score, len(overlap) / len(tw) * 0.92)
            # Board title is a subset of the saved title -- the saved one just
            # carries an extra location or region.
            if len(cw) >= 3 and cw <= tw:
                score = max(score, 0.93)
            # Saved title is a subset of a longer listing line.
            if len(tw) >= 3 and tw <= cw:
                score = max(score, 0.93)
        if score > best:
            best, best_url, best_text = score, href, text
    return best, best_url, best_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--out", default="resolved.tsv")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = []
    for line in Path(args.file).read_text(encoding="utf-8").splitlines()[1:]:
        if not line.strip():
            continue
        p = (line.split("\t") + [""] * 4)[:4]
        if "CLOSED" in p[3]:
            continue
        rows.append({"company": p[0], "title": p[1], "location": p[2]})
    if args.limit:
        rows = rows[: args.limit]

    by_company = {}
    for r in rows:
        by_company.setdefault(r["company"], []).append(r)

    from playwright.sync_api import sync_playwright

    resolved, unresolved = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})

        for i, (company, jobs) in enumerate(sorted(by_company.items()), 1):
            print(f"[{i}/{len(by_company)}] {company} ({len(jobs)} saved)...", flush=True)

            listings, board = discover_via_careers(page, company)
            route = "careers"
            if not listings:
                listings, board = discover_via_slug(page, company)
                route = "slug"

            if not listings:
                for j in jobs:
                    unresolved.append({**j, "reason": "no board found"})
                print("      no board found", flush=True)
                continue
            print(f"      [{route}] {board[:60]} ({len(listings)} postings)", flush=True)

            for j in jobs:
                score, url, text = match_title(j["title"], listings)
                # Word overlap alone accepted "Solutions Engineer, AI Agent" ->
                # "Lead Engineer - AI Agent Voice Experience": a software role,
                # scored past the threshold by two shared words. Below a
                # near-exact score, the match must also be the same kind of job.
                fam = roles.family(j["title"])
                if 0.62 <= score < 0.90 and fam and roles.family(text or "") != fam:
                    score = 0.0
                if score >= 0.62:
                    resolved.append({**j, "url": url, "score": round(score, 2),
                                     "route": route, "matched": (text or "")[:70]})
                    print(f"      OK  {score:.0%}  {j['title'][:42]}", flush=True)
                else:
                    # "Not on the board" is not a finding on its own -- the
                    # role may be posted under a different name. Carry the
                    # same-family candidates so a human can judge.
                    cands = roles.similar(j["title"], listings)
                    unresolved.append({**j, "reason": f"no title match (best {score:.0%})",
                                       "board": board, "board_size": len(listings),
                                       "candidates": cands})
                    print(f"      --  {score:.0%}  {j['title'][:42]}", flush=True)
                    for c in cands:
                        lvl = "" if c["same_level"] else " [diff level]"
                        print(f"          ~ {c['title'][:56]}{lvl}", flush=True)
        browser.close()

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("track\tcompany\ttitle\turl\n")
        for r in resolved:
            track = "cs" if re.search(
                r"customer success|account manager|implementation|success engineer",
                r["title"], re.I) else "gtm"
            f.write(f"{track}\t{r['company']}\t{r['title']}\t{r['url']}\n")

    Path(args.out.replace(".tsv", "_unresolved.json")).write_text(
        json.dumps(unresolved, indent=2), encoding="utf-8")

    # Suggestions, kept in their own file: these are same-family guesses, not
    # matches, and they must never flow into the tailoring input unreviewed.
    sugg = [(u, c) for u in unresolved for c in u.get("candidates", [])]
    if sugg:
        cand_path = args.out.replace(".tsv", "_candidates.tsv")
        with open(cand_path, "w", encoding="utf-8") as f:
            f.write("company\tsaved_title\tcandidate_title\tsame_level\turl\n")
            for u, c in sugg:
                f.write(f"{u['company']}\t{u['title']}\t{c['title']}\t"
                        f"{c['same_level']}\t{c['url']}\n")
        print(f"candidates {len(sugg)} (review before use) -> {cand_path}")
    by_route = {}
    for r in resolved:
        by_route[r["route"]] = by_route.get(r["route"], 0) + 1
    print(f"\nresolved {len(resolved)}/{len(rows)}  by route: {by_route}  -> {args.out}")
    print(f"unresolved {len(unresolved)}")


if __name__ == "__main__":
    main()
