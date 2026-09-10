#!/usr/bin/env python3
"""Read a PDF's typography, colour and geometry. One pass, no judgment.

Everything here is a fact the file states about itself, extracted with
pdfplumber. It replaced a hand-rolled content-stream parser that had to track
the transformation matrix through q/Q/cm by hand, and which could not attribute
the body font at all -- pypdf's text visitor returned no font dictionary for
the largest run of text on the page, so the one font that mattered most was the
one it could not name.

pdfplumber is MIT, on MIT pdfminer.six. PyMuPDF does more and is faster, and is
AGPL unless you buy a licence: a repo published for other people to fork and
host cannot quietly put that obligation on them.
"""

import collections
import logging
import re

# pdfminer complains about missing FontBBox on perfectly readable files, once
# per font, straight to stderr. It is noise about a field nothing here uses.
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _hex(colour) -> str:
    """A pdfplumber colour to #RRGGBB, or '' if it is not RGB.

    Fills can be a pattern name rather than a colour ('P6'), a single grey
    value, or CMYK. Anything not understood is skipped rather than guessed at.
    """
    if isinstance(colour, (int, float)):
        v = round(float(colour) * 255)
        return "#%02X%02X%02X" % (v, v, v)
    if not isinstance(colour, (list, tuple)):
        return ""
    try:
        values = [float(v) for v in colour]
    except (TypeError, ValueError):
        return ""
    if len(values) == 1:
        v = round(values[0] * 255)
        return "#%02X%02X%02X" % (v, v, v)
    if len(values) == 3:
        return "#%02X%02X%02X" % tuple(round(v * 255) for v in values)
    if len(values) == 4:
        c, m, y, k = values
        return "#%02X%02X%02X" % tuple(
            round(255 * (1 - min(1.0, ch + k))) for ch in (c, m, y))
    return ""


def clean_font(name: str) -> str:
    """'DAAAAA+Lato-Regular' -> 'lato'. The subset prefix and weight are not
    part of the family."""
    name = re.sub(r"^[A-Z]{6}\+", "", (name or "").lstrip("/"))
    name = re.split(r"[-,_]", name)[0]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def read(data: bytes, max_pages: int = 2) -> dict:
    """Everything the file says about how it looks.

    {fonts, colours, panel, page, title_align, sections} -- or {} if the file
    cannot be opened. Style detection is cosmetic, so a failure here costs the
    house template, never the resume.
    """
    import io

    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception:
        return {}

    with pdf:
        pages = pdf.pages[:max_pages]
        if not pages:
            return {}
        first = pages[0]
        width, height = float(first.width), float(first.height)

        chars = []
        for page in pages:
            try:
                chars.extend(page.chars)
            except Exception:
                continue
        if not chars:
            return {}

        panel = _panel(first, width, height)
        split = (panel["x0"] + panel["width"]) if panel and panel["side"] == "left" \
            else (panel["x0"] if panel else None)

        return {
            "page": (width, height),
            "fonts": _fonts(chars),
            "colours": _colours(chars),
            "panel": panel,
            "sidebar_ink": _sidebar_ink(chars, panel),
            "title_align": _title_align(chars, width, panel),
            "sections": _sections(first, split),
        }


def _fonts(chars: list) -> list:
    """[(family, point size, character count)], most used first.

    Size is what separates the roles. The name is the biggest thing on the
    page and one of the least frequent; the body is the smallest and by far
    the most frequent. Guessing that from a bare list of font names is what
    made section headings come back in the name's typeface.
    """
    agg = collections.Counter()
    for ch in chars:
        try:
            agg[(clean_font(ch.get("fontname")), round(float(ch.get("size", 0)), 1))] += 1
        except (TypeError, ValueError):
            continue
    merged = collections.Counter()
    for (family, size), n in agg.items():
        if family:
            merged[(family, size)] += n
    return [(f, s, n) for (f, s), n in merged.most_common()]


def _colours(chars: list) -> list:
    """[(hex, character count)] for text fill colours, most used first."""
    agg = collections.Counter()
    for ch in chars:
        colour = _hex(ch.get("non_stroking_color"))
        if colour:
            agg[colour] += 1
    return agg.most_common()


def _panel(page, width: float, height: float):
    """A tall block down one edge: a sidebar. Already in page coordinates."""
    best = None
    for rect in getattr(page, "rects", []) or []:
        try:
            w, h, x0 = float(rect["width"]), float(rect["height"]), float(rect["x0"])
        except (KeyError, TypeError, ValueError):
            continue
        colour = _hex(rect.get("non_stroking_color"))
        if not colour:
            continue
        # Tall, narrow, against an edge. A full-width block is the page
        # background; a short one is a heading rule.
        if not (h > 0.6 * height and 0.15 * width < w < 0.55 * width):
            continue
        if not (x0 < 0.05 * width or x0 + w > 0.95 * width):
            continue
        if best is None or w > best["width"]:
            best = {"x0": x0, "width": w, "colour": colour,
                    "side": "left" if x0 < width / 2 else "right"}
    return best


def _sidebar_ink(chars: list, panel) -> str:
    """The text colour inside the panel, measured rather than assumed.

    Deriving it from how dark the panel is gets white-on-navy right and a
    two-tone panel wrong.
    """
    if not panel:
        return ""
    lo, hi = panel["x0"], panel["x0"] + panel["width"]
    inside = collections.Counter()
    for ch in chars:
        try:
            if lo <= float(ch["x0"]) < hi:
                colour = _hex(ch.get("non_stroking_color"))
                if colour:
                    inside[colour] += 1
        except (KeyError, TypeError, ValueError):
            continue
    return inside.most_common(1)[0][0] if inside else ""


def _title_align(chars: list, width: float, panel=None) -> str:
    """Is the name centred? Measured off the largest type on the page.

    Centred within its own column, not within the page. A two-column resume
    puts the name in the main column, whose centre is nowhere near the page's
    -- measuring against the page called a left-aligned name centred.
    """
    if not chars:
        return "left"
    lo, hi = 0.0, width
    if panel:
        if panel["side"] == "left":
            lo = panel["x0"] + panel["width"]
        else:
            hi = panel["x0"]
    try:
        in_column = [c for c in chars if lo <= float(c["x0"]) < hi] or chars
        biggest = max(float(c.get("size", 0)) for c in in_column)
        title = [c for c in in_column if abs(float(c.get("size", 0)) - biggest) < 0.5]
        x0 = min(float(c["x0"]) for c in title)
        x1 = max(float(c["x1"]) for c in title)
    except (KeyError, TypeError, ValueError):
        return "left"
    span = max(hi - lo, 1.0)
    return "center" if abs(((x0 + x1) / 2) - (lo + hi) / 2) / span < 0.07 else "left"


# Section headings are set in capitals on essentially every resume.
KNOWN_SECTIONS = {
    "contact": "contact", "details": "contact", "info": "contact",
    "skills": "skills", "expertise": "skills", "competencies": "skills",
    "education": "education", "academic": "education",
    "certifications": "certifications", "certificates": "certifications",
    "licenses": "certifications",
}


def _sections(page, split) -> dict:
    """{section: 'side' | 'main'} for headings we can place in a column.

    Which sections live in the sidebar was the last judged part of the layout,
    and it did not need to be: the headings are on the page and so is the
    column boundary.
    """
    if split is None:
        return {}
    found = {}
    try:
        words = page.extract_words()
    except Exception:
        return {}
    for word in words:
        text = (word.get("text") or "").strip(" :")
        if len(text) < 4 or not text.isupper():
            continue
        name = KNOWN_SECTIONS.get(re.sub(r"[^a-z]", "", text.lower()))
        if not name:
            continue
        try:
            column = "side" if float(word["x0"]) < split else "main"
        except (KeyError, TypeError, ValueError):
            continue
        found.setdefault(name, column)
    return found
