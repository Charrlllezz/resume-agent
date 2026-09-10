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
import unicodedata
from pathlib import Path

import qa
import style as style_mod
import tailor_resume as tr
import usage

# Below this, an extracted bullet is not a transcription of anything in the
# document. qa.EDITED is the bar for a *tailored* bullet, which is allowed to
# be reordered; a transcription should sit far higher, so this is stricter.
TRACED = 0.75

MAX_CHARS = 60_000


# Formats that carry a resume but that this cannot read, each with the thing
# to do about it. Falling through to "decode it as text" turned an .rtf into
# a draft full of \rtf1\ansi control words -- silent garbage is a worse
# outcome than a refusal.
UNREADABLE = {
    ".doc": "Word's older .doc format can't be read here. Open it and use "
            "File > Save As to make a .docx or a PDF.",
    ".rtf": "RTF can't be read here. Export it as a PDF or a .docx.",
    ".pages": "Pages files can't be read here. Use File > Export To > PDF.",
    ".odt": "OpenDocument files can't be read here. Export as PDF or .docx.",
    ".jpg": "That's an image. A resume needs to have text in it — export a PDF.",
    ".jpeg": "That's an image. A resume needs to have text in it — export a PDF.",
    ".png": "That's an image. A resume needs to have text in it — export a PDF.",
    ".heic": "That's an image. A resume needs to have text in it — export a PDF.",
}

TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".text", "")


def read_document(data: bytes, filename: str) -> str:
    """Plain text out of a PDF, DOCX, or text file."""
    suffix = Path(filename).suffix.lower()
    if suffix in UNREADABLE:
        raise ValueError(UNREADABLE[suffix])

    if suffix == ".pdf":
        import io

        from pypdf import PdfReader
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        text = "\n".join(pages)
    elif suffix == ".docx":
        import io

        import docx
        try:
            d = docx.Document(io.BytesIO(data))
        except Exception:
            raise ValueError(
                "That file isn't a readable .docx. If it came from an older "
                "version of Word, open it and Save As .docx or PDF.")
        blocks = [p.text for p in d.paragraphs]
        # Plenty of resumes lay themselves out in a table; ignoring tables
        # silently drops entire roles.
        for table in d.tables:
            for row in table.rows:
                blocks.extend(c.text for c in row.cells)
        text = "\n".join(blocks)
    elif suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
        if _looks_binary(text):
            raise ValueError("That doesn't look like a text file. Upload a PDF "
                             "or a .docx instead.")
    else:
        raise ValueError(f"{suffix or 'That file type'} isn't supported. "
                         "Upload a PDF, a .docx, or a plain text file.")

    text = normalise(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("No text found in that file. A scanned or image-only "
                         "PDF has nothing to read — export a text PDF instead.")
    return text[:MAX_CHARS]


def _looks_binary(text: str) -> bool:
    """Enough replacement characters or control bytes to be a binary file."""
    if not text:
        return False
    sample = text[:4000]
    odd = sum(1 for ch in sample
              if ch == "�" or (ord(ch) < 32 and ch not in "\n\r\t"))
    return odd / len(sample) > 0.02


def normalise(text: str) -> str:
    """Undo what a PDF does to text on the way out.

    A PDF stores "fi" as a single ligature glyph, so extraction returns
    "Whitﬁeld" -- one character, U+FB01, where a person typed two. The model
    transcribes it back to "Whitfield", correctly, and then the provenance
    check reports that the name is not in the document. A false positive in
    the fabrication guard is worse than a missing feature: it teaches whoever
    reads it to ignore the guard.

    NFKC folds the ligatures. The rest are the other things PDFs leave behind:
    soft hyphens from justified text, non-breaking spaces, and the private-use
    bullet glyphs some generators emit.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00ad", "").replace("\u00a0", " ")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    # Curly quotes and dashes are normalised on BOTH sides of the comparison,
    # never in one place only, or the mismatch just moves.
    for fancy, plain in (("\u2018", "'"), ("\u2019", "'"),
                         ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(fancy, plain)
    return re.sub(r"[\uf000-\uf0ff]", " ", text)


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
  "education": {{"degree": "", "school": "", "year": ""}},
  "extras": [{{"label": "AS WRITTEN, e.g. Certifications", "items": ["each line, verbatim"]}}]
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
- "extras" is every OTHER section the resume has -- certifications, awards,
  languages, publications, volunteering, projects, anything. One entry per
  section, its heading as written, its lines verbatim. Return an empty list if
  there are none. A section with nowhere to go was being dropped silently,
  which is a worse failure than any of the ones this file guards against.

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


def _flat(text: str) -> str:
    """Lowercased with runs of non-alphanumerics collapsed, for containment.

    A PDF breaks lines and pads spacing wherever it likes; comparing the
    squeezed forms means a wrapped line still matches.
    """
    return re.sub(r"[^a-z0-9]+", " ", normalise(text).lower()).strip()


def check(draft: dict, document: str) -> list:
    """Trace every extracted bullet back to the document. Deterministic.

    Same idea as qa.verify, pointed at the other end of the pipeline: there we
    ask whether a tailored bullet traces to the master resume, here whether a
    master-resume bullet traces to the document it came from.
    """
    issues = []
    # Compared against whole lines rather than the raw blob so a bullet has
    # something the same shape to match: PDF extraction breaks lines mid-phrase.
    document = normalise(document)
    lines = [ln for ln in document.splitlines() if len(ln.strip()) > 25]
    pool = lines + [" ".join(lines[i:i + 3]) for i in range(len(lines))]

    for role in draft.get("experience", []):
        company = role.get("company", "?")
        for bullet in qa._flatten(role.get("bullets", {})):
            bullet = normalise(bullet)
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

    # Extras are short verbatim lines -- "Salesforce Administrator", "Spanish
    # (fluent)" -- so they are checked by containment, not by the bullet
    # matcher. Scoring a 24-character line against a pool of full sentences
    # returned 38% for a certification that was plainly there, which is the
    # false positive this whole layer must not produce.
    flat = _flat(document)
    for extra in draft.get("extras") or []:
        label = (extra.get("label") or "?").strip()
        for item in extra.get("items") or []:
            item = normalise(str(item)).strip()
            if len(item) < 4:
                continue
            if _flat(item) not in flat:
                issues.append(qa.Issue(
                    qa.ERROR, "traced",
                    f"{label}: not found in the document — {item[:80]}"))

    contact = draft.get("contact", {})
    for field in ("name", "email"):
        value = normalise((contact.get(field) or "").strip())
        if value and value.lower() not in document.lower():
            issues.append(qa.Issue(qa.WARN, "contact",
                                   f"{field} '{value}' is not in the document"))
    if not contact.get("name"):
        issues.append(qa.Issue(qa.WARN, "contact", "no name found"))
    if not draft.get("experience"):
        issues.append(qa.Issue(qa.ERROR, "experience", "no roles found"))
    return issues


def ingest(client, data: bytes, filename: str, track: str = "general") -> tuple:
    """(draft resume, source text, provenance issues). Saves nothing.

    The draft carries the document's own visual style, so the tailored resume
    comes back looking like the one that was uploaded rather than in a house
    style nobody chose.
    """
    document = read_document(data, filename)
    draft = extract(client, document, track)
    draft["style"] = style_mod.detect(client, data, filename, tr.MODEL)
    return draft, document, check(draft, document)
