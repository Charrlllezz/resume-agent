# resume-agent

Tailors your resume to a specific job posting, **verifies it didn't invent
anything**, and tells you whether the role is worth applying to at all.

Built while job hunting, and open because the verification part is the part
that's missing from every "AI resume builder" — those will happily write that
you scaled a team to 60 engineers. This one traces every bullet, metric, skill,
title, and date in the output back to a file you wrote by hand, and refuses to
ship a claim it can't find a source for.

```
master_resume.json  ──▶  tailor  ──▶  QA (deterministic)  ──▶  PDF
   (you write this)        │              │
                       job posting     rejects anything
                                       not traceable to source
```

## Quick start

```bash
git clone <this repo> && cd resume-agent
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
echo "ANTHROPIC_API_KEY=sk-..." > .env

.venv/bin/python test_qa.py                       # runs against the sample resume
cp master_resume.example.json master_resume.json  # now make it yours
```

Everything runs against `master_resume.example.json` (a fictional person) until
you create `master_resume.json`, so you can see what it does before writing
anything real. **`master_resume.json` is gitignored — keep it that way.**

## Get your resume in

Upload a PDF, DOCX or text resume at `/resume` and it is transcribed into the
schema below — then **you review and edit it before anything is saved**.

That review step is load-bearing, not ceremony. `qa.py` certifies tailored
output by comparing it to `master_resume.json`. If a model wrote that file
unsupervised, the fabrication guard would be a machine marking its own
homework. A person has to commit to it for it to mean anything.

Between the two, ingestion checks itself the same way the rest of the pipeline
does: every extracted bullet is traced back to the document by string
comparison, and every number in it is checked against the document's numbers —
because changing "25+" to "250+" leaves a bullet 97% identical and completely
false. Anything that doesn't trace is flagged on the review screen.

Ingestion transcribes. It does not write bullets, add metrics, improve weak
phrasing, or invent per-track variants.

## Write your master resume

One file, everything you've actually done — more than fits on a page, because
tailoring means *selecting*, and it can only select from what's there.

```jsonc
{
  "contact":  { "name": "...", "phone": "...", "email": "...", "linkedin": "..." },
  "headlines": {
    "gtm": ["...", "..."],        // one pool per track you apply to;
    "cs":  ["...", "..."]         // name the tracks whatever you want
  },
  "experience": [{
    "company": "...", "location": "...", "dates": "Apr 2024 – Aug 2026",
    "titles":  { "gtm": "...", "cs": "..." },      // how the same role reads per track
    "bullets": { "gtm": ["...11 of them..."], "cs": ["...5..."] },
    "bullets_expected": [4, 5]                     // how many the output should carry
  }],
  "skills":    { "gtm": { "Group name": ["..."] } },
  "education": { "degree": "...", "school": "...", "year": "..." }
}
```

Write more bullets per role than you need — 10+ for recent roles. Selection
across a wide pool is where the tailoring happens.

> **This file is the ground truth.** QA certifies output by comparing it to
> this, so anything exaggerated here becomes permanently "verified." Its only
> value is that it is honest.

## Tailor one posting

```bash
python tailor_resume.py --url https://jobs.ashbyhq.com/company/<id>
python tailor_resume.py --file posting.txt
python tailor_resume.py --text "paste posting here"
```

Renders the posting in Chromium (most boards are JS-rendered, and the ones that
aren't bury the text under inline CSS), classifies the role, selects and orders
bullets, then writes styled HTML plus a single-page PDF. Prints an apply/skip
verdict, a QA report, and token usage with an estimated cost (~$0.14/role).

| Flag | Effect |
|---|---|
| `--company NAME` | Override the detected company (used for filenames) |
| `--strict` | Exit non-zero if QA finds a factual error |
| `--no-qa` | Skip the QA pass |
| `--no-log` | Don't append to `applications.csv` |

QA errors suppress logging, so a bad run can't enter your tracker.

## The web UI

```bash
python app.py          # -> http://127.0.0.1:5000
```

Paste a posting URL, watch it work, get a page with the tailored resume plus
everything the pipeline knows about the role: the fit verdict with the resume
evidence quoted for **each** requirement, every QA finding, the tailoring
decisions, the ATS terms the output never says, and the PDF.

The spreadsheet row is on that page too, as one small card — which is roughly
the proportion of what a run produces that a spreadsheet can hold.

A run takes 40–90s, so it happens on a worker thread and the page polls. This
is the single-user local build: it trusts whoever can reach the port, reads
your key from `.env`, and keeps runs in memory.

## A whole list at once

```bash
python resolve_urls.py saved_jobs.tsv --out resolved.tsv   # names → real URLs
python rank.py resolved.tsv                                # which are worth your day
python generate.py ranked.tsv --dry-run                    # what it'd cost
python generate.py ranked.tsv                              # one PDF per role
```

Finished resumes land in `resumes/`, named for the person receiving them —
`Firstname_Lastname_Resume_<Company>.pdf`, taken from your `contact.name`.
Where a company has more than one open role the role is appended, since naming
both by company alone silently overwrites one with the other.

Both `rank.py` and `generate.py` are resumable: every result is cached the
moment it lands, and roles already on disk are skipped, so an interrupted run
resumes instead of re-paying. Both stop the sweep on a credit-balance error
rather than failing the same way for every remaining role.

## The other tools

| Script | What it answers |
|---|---|
| `qa.py` | Did the tailoring invent anything? Traces every claim to source. Imported by the pipeline; not run directly. |
| `fit.py` | Should you apply? Judges the posting's requirements against your resume and returns Strong / Possible / Stretch / Skip with evidence per requirement. |
| `rank.py resolved.tsv` | Runs `fit.py` across a whole list and sorts into apply / prep / stretch / skip. Writes `ranked.tsv`. |
| `compare.py roles.tsv` | Across many postings: track accuracy, story balance, ATS coverage, which of your bullets never get picked. |
| `gaps.py roles.tsv` | What vocabulary is your resume systematically missing across many postings? |
| `resolve_urls.py saved.tsv` | Turn a list of company + title into real posting URLs. Writes `*_candidates.tsv` for roles that may be posted under another name. |
| `roles.py` | Role families: is a differently-worded title the same job? Imported, not run. |
| `test_qa.py` | The test suite: QA, provenance, token accounting. Free and offline. Run it after changing `qa.py` or the prompts. |
| `test_fit.py` | An eval for whether the fit assessment still discriminates. Calls the model, so it costs about $0.07 a run. |

`roles.tsv` and `resolved.tsv` are TSVs of `track⇥company⇥title⇥url`.
See `saved_jobs.example.tsv` and `resolved.example.tsv` for the input formats.

## Two mechanisms, deliberately different

**`qa.py` is deterministic.** It asks whether a claim traces back to the master
resume — a factual property, checkable by string comparison. It is the
fabrication guard, so it must never be a model judging itself. Bullet
provenance scores by content containment, which lets a bullet be reordered
outcome-first while still rejecting invented content.

**`fit.py` calls the model.** It asks whether experience *satisfies* a
requirement, which is semantic: postings write must-haves as phrases like
"working with non-technical stakeholders", which a resume satisfies without
containing the words. A literal matcher scored every real posting at 0–14% and
recommended skipping all of them. The model must cite resume evidence per
requirement, which keeps the judgment auditable.

The split is the whole design: **facts are checked by code, judgment is
delegated to the model, and neither does the other's job.**

## Privacy

`.gitignore` excludes your resume, your applications, your job list, and the
caches holding a model's read on every role. This matters more than it sounds:
a fit cache contains lines like *"skip — weakest fit on the list"* next to a
company name, and a recruiter at that company can read it. If you fork this to
share your own version, check `git status` before the first push, and remember
that deleting a file later doesn't remove it from history.

## Known limits

- ATS coverage is a rough proxy. The keyword list is a model's guess at what a
  posting screens on and its length varies 24–44 terms between postings, so the
  percentage is not comparable across roles. Read the missing-terms list.
- `resolve_urls.py` won't find every posting. Some companies have no reachable
  board (recruiting firms, some Workday tenants), and saved roles get renamed
  or filled — those are reported with a reason rather than matched to an
  approximate posting.
- Fit verdicts on borderline roles can shift one level between runs. Read the
  unmet-requirements list, which is stable, not the label.
- Fit scores are for reading, not for ranking. `rank.py` sorts into buckets --
  apply, apply with an answer ready, a gap you can't close tonight, a different
  job -- because those change what you do and a position in an ordered list
  does not. The buckets key off the unmet list, not the score.
- An assessment run before the rules tightened is not comparable to one after.
  The old prompt accepted a name in the skills list as evidence, so across 58
  real postings 52 came back "Strong" and 35 had nothing unmet at all. Delete
  `rank_cache.json` if you want a list re-judged.
- A PDF gives you text as rendered, so a headline set in small caps comes back
  shouting. Ingestion transcribes faithfully, including that; fix it in review.
- An ingested resume has one track and roughly five bullets a role, so there is
  little for the tailoring to select between. It gets better as you add
  bullets — that is where the tailoring actually happens.
- QA verifies *provenance*, not truth. It proves the output matches your master
  resume. If that file overstates, QA will certify the overstatement.

## License

MIT
