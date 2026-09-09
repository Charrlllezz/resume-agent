#!/usr/bin/env python3
"""Turn an uploaded resume into the master_resume.json schema -- and prove it.

This is the step that could quietly destroy the point of the project. qa.py is
only meaningful because master_resume.json is ground truth a human wrote; if a
model generates that file and qa.py then verifies output against it, the
fabrication guard is checking a machine against itself.

So ingestion is deliberately transcription, not authoring:

  1. It structures what the document says. It does not write new bullets,
     invent metrics, or produce per-track variants of anything.
  2. Every extracted bullet is then traced back to the source text by the same
     containment scoring qa.py uses on tailored output. Anything that does not
     trace is flagged, in the draft, where a person has to look at it.
  3. A person reviews and commits the result. Nothing is saved until they do.

Step 3 is the one that matters, and it is why the web flow has a review screen
rather than a spinner and a success message.
"""

import json
import re
from pathlib import Path

import qa
import tailor_resume as tr
import usage

# Below this, an extracted bullet is not a transcription of anything in the
# document. qa.EDITED is the bar for a *tailored* bullet, which is allowed to
# be reordered; a transcription should sit far higher, so this is stricter.
TRACED = 0.75

MAX_CHARS = 60_000


def read_document(data: bytes, filename: str) -> str:
    """Plain text out of a PDF, DOCX, or text file."""
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        import io

        from pypdf import PdfReader
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        text = "\n".join(pages)
    elif suffix in (".docx", ".doc"):
        import io

        import docx
        d = docx.Document(io.BytesIO(data))
        blocks = [p.text for p in d.paragraphs]
        # Plenty of resumes lay themselves out in a table; ignoring tables
        # silently drops entire roles.
        for table in d.tables:
            for row in table.rows:
                blocks.extend(c.text for c in row.cells)
        text = "\n".join(blocks)
    else:
        text = data.decode("utf-8", errors="replace")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("No text found in that file. A scanned or image-only "
                         "PDF has nothing to read — export a text PDF instead.")
    return text[:MAX_CHARS]


EXTRACT = """Here is the plain text of someone's resume:

<resume>
{document}
</resume>

Restructure it as JSON. You are transcribing, not writing.

{{
  "contact": {{"name": "", "phone": "", "email": "", "linkedin": ""}},
  "headlines": {{"{track}": ["the headline or summary line, if the resume has one"]}},
  "experience": [
    {{
      "company": "",
      "location": "",
      "dates": "as written, e.g. Apr 2024 - Aug 2026",
      "titles": {{"{track}": "the title as written"}},
      "bullets": {{"{track}": ["each bullet, verbatim"]}},
      "bullets_expected": [3, 5]
    }}
  ],
  "skills": {{"{track}": {{"Group name": ["skill", "skill"]}}}},
  "education": {{"degree": "", "school": "", "year": ""}}
}}

Rules:
- Copy bullets VERBATIM. Fix only obvious PDF extraction damage: a word split
  across a line break, a bullet glyph left at the start, doubled spaces.
- Invent nothing. No metrics, no tools, no responsibilities that are not
  written above. If the resume has no headline, return an empty list.
- Do not summarise, merge, split, or improve a bullet. A weak bullet stays weak;
  its owner will fix it in the review step.
- Leave a field as an empty string if the resume does not state it. An empty
  field is correct; a guess is not.
- Order experience most recent first.
- "bullets_expected" is [low, high] for how many bullets that role should carry
  in a tailored resume: [4, 5] for recent roles, fewer for older ones.
- Group skills the way the resume groups them. If it just lists them, use one
  group called "Skills".

Return ONLY the JSON object."""


def extract(client, document: str, track: str = "general") -> dict:
    response = tr.send(
        client,
        model=tr.MODEL,
        max_tokens=16000,
        messages=[{"role": "user",
                   "content": EXTRACT.format(document=document, track=track)}],
    )
    usage.record(response)
    return tr.extract_json(response)


def check(draft: dict, document: str) -> list:
    """Trace every extracted bullet back to the document. Deterministic.

    Same idea as qa.verify, pointed at the other end of the pipeline: there we
    ask whether a tailored bullet traces to the master resume, here whether a
    master-resume bullet traces to the document it came from.
    """
    issues = []
    # Compared against whole lines rather than the raw blob so a bullet has
    # something the same shape to match: PDF extraction breaks lines mid-phrase.
    lines = [ln for ln in document.splitlines() if len(ln.strip()) > 25]
    pool = lines + [" ".join(lines[i:i + 3]) for i in range(len(lines))]

    for role in draft.get("experience", []):
        company = role.get("company", "?")
        for bullet in qa._flatten(role.get("bullets", {})):
            score, _ = qa._best_bullet_match(bullet, pool)
            if score < TRACED:
                issues.append(qa.Issue(
                    qa.ERROR, "traced",
                    f"{company}: not found in the document ({score:.0%}) — "
                    f"{bullet[:90]}"))
                continue
            # Similarity alone will not catch this. Changing "25+" to "250+"
            # and "40%" to "90%" leaves a bullet 97% identical and completely
            # false, so numbers are checked against the whole document
            # separately -- the same split qa.py makes on tailored output.
            invented = qa._numbers(bullet) - qa._numbers(document)
            if invented:
                issues.append(qa.Issue(
                    qa.ERROR, "numbers",
                    f"{company}: {', '.join(sorted(invented))} "
                    f"{'is' if len(invented) == 1 else 'are'} not in the document — "
                    f"{bullet[:70]}"))

    contact = draft.get("contact", {})
    for field in ("name", "email"):
        value = (contact.get(field) or "").strip()
        if value and value.lower() not in document.lower():
            issues.append(qa.Issue(qa.WARN, "contact",
                                   f"{field} '{value}' is not in the document"))
    if not contact.get("name"):
        issues.append(qa.Issue(qa.WARN, "contact", "no name found"))
    if not draft.get("experience"):
        issues.append(qa.Issue(qa.ERROR, "experience", "no roles found"))
    return issues


def ingest(client, data: bytes, filename: str, track: str = "general") -> tuple:
    """(draft resume, source text, provenance issues). Saves nothing."""
    document = read_document(data, filename)
    draft = extract(client, document, track)
    return draft, document, check(draft, document)
