#!/usr/bin/env python3
"""What the uploaded resume looks like, so the tailored one looks like it too.

Handing someone back a document in a house style they did not choose is a
strange thing to do -- they picked their typography and their layout, and the
tailoring is supposed to change the words, not the design.

Two halves, deliberately split the way the rest of this project splits things:

  Deterministic. Font families come straight out of the PDF's own resource
  dictionary, or out of a DOCX's styles. No judgment involved.

  Judged. Whether a resume is plain or designed, what its accent colour is,
  whether the name is centred -- that is reading a picture, which is what the
  model is for.

Neither touches content. Style lives beside the resume, never inside a bullet.
"""

import re

# The default is this project's own template, used when nothing is detected.
DEFAULTS = {
    "chrome": "designed",       # designed | plain
    "font_display": "'Space Grotesk', sans-serif",
    "font_heading": "'IBM Plex Mono', monospace",
    "font_body": "'IBM Plex Sans', sans-serif",
    "font_mono": "'IBM Plex Mono', monospace",
    "google_fonts": ["Space+Grotesk:wght@700",
                     "IBM+Plex+Sans:wght@400;500;600",
                     "IBM+Plex+Mono:wght@400;500;600"],
    "accent": "#1F6E4A",
    "accent_2": "#D7DCD3",
    "ink": "#171D1A",
    "header_align": "left",     # left | center
    "layout": "single",         # single | two-column
    "sidebar_side": "left",
    "sidebar_width": 32,
    "sidebar_bg": "",
    "sidebar_sections": [],
    "from_document": False,     # set when the style came from an upload
    "density": "normal",        # normal | compact
}

# A resume's font is usually one of a couple of dozen, and several of the most
# common are not web fonts at all. Google hosts metric-compatible replacements
# for the Microsoft core set -- Carlito for Calibri, Arimo for Arial, Tinos for
# Times New Roman, Caladea for Cambria, Gelasio for Georgia -- which means the
# substitute occupies the same space, so line breaks land where they did.
FONT_MAP = {
    "calibri": ("Carlito", "Carlito, Calibri, sans-serif", "Carlito:wght@400;700"),
    "arial": ("Arimo", "Arimo, Arial, Helvetica, sans-serif", "Arimo:wght@400;500;700"),
    "helvetica": ("Arimo", "Arimo, Helvetica, Arial, sans-serif", "Arimo:wght@400;500;700"),
    "helveticaneue": ("Arimo", "Arimo, Helvetica, Arial, sans-serif", "Arimo:wght@400;500;700"),
    "liberationsans": ("Arimo", "Arimo, Arial, sans-serif", "Arimo:wght@400;500;700"),
    "timesnewroman": ("Tinos", "Tinos, 'Times New Roman', serif", "Tinos:wght@400;700"),
    "times": ("Tinos", "Tinos, 'Times New Roman', serif", "Tinos:wght@400;700"),
    "cambria": ("Caladea", "Caladea, Cambria, serif", "Caladea:wght@400;700"),
    "georgia": ("Gelasio", "Gelasio, Georgia, serif", "Gelasio:wght@400;500;700"),
    "garamond": ("EB Garamond", "'EB Garamond', Garamond, serif", "EB+Garamond:wght@400;600"),
    "book antiqua": ("EB Garamond", "'EB Garamond', Palatino, serif", "EB+Garamond:wght@400;600"),
    "palatino": ("EB Garamond", "'EB Garamond', Palatino, serif", "EB+Garamond:wght@400;600"),
    "centurygothic": ("Questrial", "Questrial, 'Century Gothic', sans-serif", "Questrial"),
    "verdana": ("Open Sans", "'Open Sans', Verdana, sans-serif", "Open+Sans:wght@400;600;700"),
    "tahoma": ("Open Sans", "'Open Sans', Tahoma, sans-serif", "Open+Sans:wght@400;600;700"),
    "trebuchet": ("Open Sans", "'Open Sans', 'Trebuchet MS', sans-serif", "Open+Sans:wght@400;600;700"),
}

# Fonts Google already hosts under their own name, with the generic family each
# one falls back to. Matched by prefix, so "IBMPlexSans-SemiBold" finds
# "ibmplexsans". The generic is recorded rather than guessed from the name --
# guessing gets "PT Serif" right and Lora, Cormorant and Playfair wrong.
SELF_HOSTED = {
    "ibmplexsans": ("IBM Plex Sans", "IBM+Plex+Sans:wght@400;500;600", "sans-serif"),
    "ibmplexmono": ("IBM Plex Mono", "IBM+Plex+Mono:wght@400;500;600", "monospace"),
    "ibmplexserif": ("IBM Plex Serif", "IBM+Plex+Serif:wght@400;600", "serif"),
    "spacegrotesk": ("Space Grotesk", "Space+Grotesk:wght@400;700", "sans-serif"),
    "lato": ("Lato", "Lato:wght@400;700", "sans-serif"),
    "roboto": ("Roboto", "Roboto:wght@400;500;700", "sans-serif"),
    "opensans": ("Open Sans", "Open+Sans:wght@400;600;700", "sans-serif"),
    "montserrat": ("Montserrat", "Montserrat:wght@400;600;700", "sans-serif"),
    "raleway": ("Raleway", "Raleway:wght@400;600;700", "sans-serif"),
    "merriweather": ("Merriweather", "Merriweather:wght@400;700", "serif"),
    "sourcesanspro": ("Source Sans 3", "Source+Sans+3:wght@400;600;700", "sans-serif"),
    "sourcesans3": ("Source Sans 3", "Source+Sans+3:wght@400;600;700", "sans-serif"),
    "inter": ("Inter", "Inter:wght@400;500;700", "sans-serif"),
    "poppins": ("Poppins", "Poppins:wght@400;600;700", "sans-serif"),
    "nunito": ("Nunito", "Nunito:wght@400;600;700", "sans-serif"),
    "ptsans": ("PT Sans", "PT+Sans:wght@400;700", "sans-serif"),
    "ptserif": ("PT Serif", "PT+Serif:wght@400;700", "serif"),
    "cormorant": ("Cormorant Garamond", "Cormorant+Garamond:wght@400;600", "serif"),
    "playfair": ("Playfair Display", "Playfair+Display:wght@400;700", "serif"),
    "karla": ("Karla", "Karla:wght@400;600;700", "sans-serif"),
    "worksans": ("Work Sans", "Work+Sans:wght@400;500;600", "sans-serif"),
    "robotomono": ("Roboto Mono", "Roboto+Mono:wght@400;500", "monospace"),
    "robotoslab": ("Roboto Slab", "Roboto+Slab:wght@400;700", "serif"),
    "sourcecodepro": ("Source Code Pro", "Source+Code+Pro:wght@400;500", "monospace"),
    "ibmplexsanscondensed": ("IBM Plex Sans Condensed",
                             "IBM+Plex+Sans+Condensed:wght@400;600", "sans-serif"),
    "ebgaramond": ("EB Garamond", "EB+Garamond:wght@400;600", "serif"),
    "lora": ("Lora", "Lora:wght@400;600;700", "serif"),
    "firasans": ("Fira Sans", "Fira+Sans:wght@400;500;700", "sans-serif"),
    "firacode": ("Fira Code", "Fira+Code:wght@400;500", "monospace"),
    "jetbrainsmono": ("JetBrains Mono", "JetBrains+Mono:wght@400;500", "monospace"),
}

SERIF_HINTS = ("serif", "times", "georgia", "garamond", "cambria", "book",
               "palatino", "minion", "caslon", "baskerville", "merriweather")


def clean_font_name(raw: str) -> str:
    """'AAAAAA+IBMPlexMono-SemiBold' -> 'ibmplexmono'.

    PDFs subset embedded fonts and prefix the name with six letters and a plus,
    then hang the weight off the end. Neither is part of the family.
    """
    name = re.sub(r"^[A-Z]{6}\+", "", raw.lstrip("/"))
    name = re.split(r"[-,]", name)[0]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve(raw: str) -> tuple:
    """(css stack, google fonts spec or None) for one detected font name."""
    key = clean_font_name(raw)
    if not key:
        return "", None
    # Longest key first. "roboto" is a prefix of "robotomono", so matching in
    # table order resolved Roboto Mono to Roboto -- a proportional font where
    # the document had a monospace, which pulls every aligned date out of line.
    for prefix in sorted(SELF_HOSTED, key=len, reverse=True):
        family, spec, generic = SELF_HOSTED[prefix]
        if key.startswith(prefix) or prefix in key:
            return f"'{family}', {generic}", spec
    for prefix in sorted(FONT_MAP, key=len, reverse=True):
        _family, stack, spec = FONT_MAP[prefix]
        clean = re.sub(r"[^a-z0-9]", "", prefix)
        if key.startswith(clean) or clean in key:
            return stack, spec
    return _generic(key), None


def _generic(key: str) -> str:
    """The fallback a stack ends with. A mono font falling back to sans-serif
    is worse than no detection at all -- aligned dates stop aligning."""
    if "mono" in key or "courier" in key or "consol" in key:
        return "monospace"
    return "serif" if any(h in key for h in SERIF_HINTS) else "sans-serif"


def fonts_from_pdf(data: bytes) -> list:
    """Every font family the PDF declares, most-used first-ish.

    Deterministic: this is the file telling us what it is set in, not a guess.
    """
    import io

    from pypdf import PdfReader
    seen = []
    reader = PdfReader(io.BytesIO(data))
    for page in reader.pages[:3]:
        fonts = (page.get("/Resources", {}) or {}).get("/Font", {}) or {}
        for key in fonts:
            try:
                base = str(fonts[key].get_object().get("/BaseFont", ""))
            except Exception:
                continue
            name = clean_font_name(base)
            if name and name not in seen:
                seen.append(name)
    return seen


def fonts_from_docx(data: bytes) -> list:
    import io

    import docx
    d = docx.Document(io.BytesIO(data))
    seen = []
    for style in d.styles:
        try:
            name = style.font.name
        except Exception:
            continue
        key = clean_font_name(name or "")
        if key and key not in seen:
            seen.append(key)
    return seen


def from_fonts(names: list) -> dict:
    """A style spec built from font names alone. No model involved."""
    spec = dict(DEFAULTS)
    if not names:
        return spec
    stacks, google = [], []
    for name in names:
        stack, gf = resolve(name)
        if stack and stack not in stacks:
            stacks.append(stack)
            if gf and gf not in google:
                google.append(gf)
    if not stacks:
        return spec
    body = stacks[0]
    display = next((s for s in stacks if s != body), body)
    # A resume that sets its name, its headings and its body in three different
    # faces is common in template-built resumes. Collapsing headings into the
    # display face loses one of them outright.
    heading = next((s for s in stacks if s not in (body, display)), display)
    # A plain Word resume is set in exactly one font. Falling back to a generic
    # monospace for dates and taglines puts type on the page that the original
    # never had, which is the opposite of matching it. Only use a monospace if
    # the document actually contains one.
    mono = next((s for s in stacks if "mono" in s.lower()), body)
    spec.update(font_body=body, font_display=display, font_heading=heading,
                font_mono=mono, google_fonts=google)
    return spec


def merge(base: dict, judged: dict) -> dict:
    """Model judgment on top of the deterministic base, keys we allow only."""
    out = dict(base)
    sections = (judged or {}).get("sidebar_sections")
    if isinstance(sections, list):
        # Constrained to sections that exist, so a hallucinated one cannot
        # silently drop a real section out of the rendered page.
        out["sidebar_sections"] = [x for x in sections
                                   if x in ("contact", "skills", "education")]
    for key in ("chrome", "accent", "accent_2", "ink", "header_align", "density"):
        value = (judged or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if out["chrome"] not in ("plain", "designed"):
        out["chrome"] = "designed"
    if out["header_align"] not in ("left", "center"):
        out["header_align"] = "left"
    if out["density"] not in ("normal", "compact"):
        out["density"] = "normal"
    for key in ("accent", "accent_2", "ink"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", out.get(key, "")):
            out[key] = DEFAULTS[key]
    return out


def colours_from_pdf(data: bytes) -> list:
    """[(hex, weight)] for every fill colour text is actually painted in.

    Deterministic, and strictly better than looking at the page. Asked to read
    the colours off a resume, the model returned #1b3a5c, #c05a26 and #1f1f1f
    for a document whose real values were #0B3C5D, #B85C1E and #2B2B2B -- close
    enough to look right and wrong enough to be someone else's brand colour.
    These come out of the content stream, so they are the file's own numbers.
    """
    import collections
    import io

    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    weights = collections.Counter()
    reader = PdfReader(io.BytesIO(data))
    for page in reader.pages[:3]:
        try:
            stream = ContentStream(page.get_contents(), reader)
        except Exception:
            continue
        current = None
        for operands, op in stream.operations:
            name = op.decode() if isinstance(op, bytes) else str(op)
            try:
                if name == "rg" and len(operands) == 3:
                    current = tuple(round(float(x) * 255) for x in operands)
                elif name == "g" and len(operands) == 1:
                    grey = round(float(operands[0]) * 255)
                    current = (grey, grey, grey)
                elif name == "k" and len(operands) == 4:
                    c, m, y, k = (float(x) for x in operands)
                    current = tuple(round(255 * (1 - min(1, ch + k)))
                                    for ch in (c, m, y))
                elif name in ("Tj", "TJ") and current is not None:
                    weights[current] += len(str(operands[0]))
            except (ValueError, TypeError, IndexError):
                continue
    return [("#%02X%02X%02X" % rgb, n) for rgb, n in weights.most_common()]


def _is_grey(hex_colour: str, tolerance: int = 18) -> bool:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b) <= tolerance


def _luma(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def colours_for(data: bytes) -> dict:
    """ink and up to two accents, taken from the document's own numbers.

    Ink is the darkest colour carrying real weight -- the body text. Accents
    are the colours with actual hue, in order of how much text uses them. A
    resume with no hue at all gets black, rather than an accent invented for it.
    """
    found = colours_from_pdf(data)
    if not found:
        return {}
    dark = [(c, n) for c, n in found if _luma(c) < 0.55]
    ink = max(dark, key=lambda cn: cn[1])[0] if dark else found[0][0]
    chromatic = [c for c, _ in found if not _is_grey(c) and _luma(c) < 0.85]
    accent = chromatic[0] if chromatic else "#000000"
    accent_2 = chromatic[1] if len(chromatic) > 1 else accent
    return {"ink": ink, "accent": accent, "accent_2": accent_2}


def layout_from_pdf(data: bytes) -> dict:
    """Column geometry, read off the page rather than guessed at.

    Two things give a sidebar away and both are in the file: text x-positions
    fall into two clusters with a wide gap between them, and a panel sidebar is
    a single large filled rectangle down one edge. Neither needs a model.

    Returns {} when the page is a single column, which is the common case.
    """
    import collections
    import io

    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    reader = PdfReader(io.BytesIO(data))
    page = reader.pages[0]
    width = float(page.mediabox.width) or 612.0
    height = float(page.mediabox.height) or 792.0

    # A filled panel down one edge is the strongest signal there is, and it
    # carries the geometry with it. Checked before the text clustering, which
    # is a heuristic and was bailing out before this ever ran.
    panel = _panel(data, width, height)
    if panel:
        px, pw, colour = panel
        return {"layout": "two-column",
                "sidebar_side": "left" if px < width / 2 else "right",
                "sidebar_width": max(22, min(45, round(100 * pw / width))),
                "sidebar_bg": colour}

    xs = []

    def visit(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            # Weighted by how much text sits there, not by how many runs. A
            # single right-floated date is two runs and twenty-six characters;
            # a sidebar is a few hundred. Counting runs called a plain
            # single-column resume two-column.
            xs.append((float(tm[4]), len(text.strip())))

    try:
        page.extract_text(visitor_text=visit)
    except Exception:
        return {}
    # Runs sitting outside the page are floats the renderer placed oddly; they
    # say nothing about where the columns are.
    xs = [(x, n) for x, n in xs if 0 <= x <= width]
    if len(xs) < 12:
        return {}

    buckets = collections.Counter()
    for x, n in xs:
        buckets[int(x // 20) * 20] += n
    used = sorted(buckets)
    gaps = [(a, b) for a, b in zip(used, used[1:]) if b - a >= 60]
    if not gaps:
        return {}
    # The widest gap is the gutter. More than one means a table, not a sidebar.
    gutter = max(gaps, key=lambda ab: ab[1] - ab[0])
    split = (gutter[0] + gutter[1]) / 2
    left = sum(n for b, n in buckets.items() if b < split)
    right = sum(n for b, n in buckets.items() if b >= split)
    # A sidebar carries contact details, skills and education -- always a
    # substantial share of the page. Floated dates carry a handful of
    # characters and must not be mistaken for one.
    if min(left, right) < 0.15 * (left + right) or min(left, right) < 120:
        return {}

    # No panel: a sidebar set on plain white, inferred from where the text is.
    side = "left" if left <= right else "right"
    pct = round(100 * (split / width if side == "left" else 1 - split / width))
    return {"layout": "two-column", "sidebar_side": side,
            "sidebar_width": max(22, min(45, pct)), "sidebar_bg": ""}


def _panel(data: bytes, width: float, height: float):
    """(x, width, hex) of a large filled block down one edge, if there is one."""
    import io

    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    reader = PdfReader(io.BytesIO(data))
    try:
        stream = ContentStream(reader.pages[0].get_contents(), reader)
    except Exception:
        return None
    # Rectangles are in user space and the page has a transform on it -- for a
    # browser-generated PDF, 0.75 to turn CSS pixels into points. Comparing an
    # untransformed width against the page width reported a 34% sidebar as 45%.
    fill, pending, found = None, [], []
    ctm = (1.0, 1.0, 0.0, 0.0)      # sx, sy, tx, ty
    stack = []
    for operands, op in stream.operations:
        name = op.decode() if isinstance(op, bytes) else str(op)
        try:
            if name == "q":
                stack.append(ctm)
            elif name == "Q" and stack:
                ctm = stack.pop()
            elif name == "cm" and len(operands) == 6:
                a, _b, _c, d, e, f = (float(v) for v in operands)
                sx, sy, tx, ty = ctm
                ctm = (sx * a, sy * d, tx + sx * e, ty + sy * f)
            elif name == "rg" and len(operands) == 3:
                fill = tuple(round(float(x) * 255) for x in operands)
            elif name == "g" and len(operands) == 1:
                v = round(float(operands[0]) * 255)
                fill = (v, v, v)
            elif name == "re" and len(operands) == 4:
                pending.append(tuple(float(x) for x in operands))
            elif name in ("f", "f*", "F"):
                sx, sy, tx, _ty = ctm
                for x, y, w, h in pending:
                    x = tx + sx * x
                    w, h = abs(sx * w), abs(sy * h)
                    # Tall, narrow, and against an edge: a sidebar. A full-page
                    # rectangle is the page background, not a column.
                    if (h > 0.6 * height and 0.15 * width < w < 0.55 * width
                            and (x < 0.05 * width or x + w > 0.95 * width)):
                        found.append((x, w, fill))
                pending = []
            elif name in ("S", "s", "n", "B", "b"):
                pending = []
        except (ValueError, TypeError, IndexError):
            pending = []
    if not found:
        return None
    x, w, colour = max(found, key=lambda f: f[1])
    return x, w, ("#%02X%02X%02X" % colour if colour else "")


DESCRIBE = """Describe only the VISUAL STYLE of this resume. Ignore what it says.

The fonts actually embedded in the file are: {fonts}
Name body_font and display_font from that list. If the list is empty or none of
them fit what you see, name the family you actually see. Do not invent a font
that is not there -- a wrong family changes every line break in the output.

Return ONLY this JSON:
{{
  "chrome": "plain" | "designed",
  "sidebar_sections": ["contact" and/or "skills" and/or "education"],
  "accent": "#rrggbb",
  "accent_2": "#rrggbb",
  "ink": "#rrggbb",
  "header_align": "left" | "center",
  "density": "normal" | "compact",
  "body_font": "family name",
  "display_font": "family name the person's NAME is set in",
  "heading_font": "family name SECTION HEADINGS and job titles are set in"
}}

- "plain": black text on white, no colour blocks, no cards, no rules beyond a
  thin line under section headings. Most Word and Google Docs resumes.
- "designed": colour used deliberately, tinted backgrounds, boxes, sidebars,
  or a visual device like a timeline.
- Colour is read out of the file separately and is not your job. The values
  you return for it are only a fallback for when that fails.
- "ink": the body text colour, usually near-black.
- "density": "compact" if it is packed to fit a page, "normal" otherwise.
- "sidebar_sections": ONLY if this resume has a sidebar column. List which of
  contact, skills and education sit in it. Return an empty list if there is no
  sidebar, or if none of those three are in it. Whether a sidebar exists at all
  is measured separately -- this is only about what is in it."""


def describe(client, data: bytes, filename: str, model: str) -> dict:
    """Look at the document and judge its style. PDFs only.

    A DOCX has its typography in the file already and nothing worth looking at
    beyond that, so it takes the deterministic path alone.
    """
    import base64

    import tailor_resume as tr
    import usage as usage_mod

    if not filename.lower().endswith(".pdf"):
        return {}
    fonts = fonts_from_pdf(data)
    response = tr.send(
        client, model=model, max_tokens=1200,
        messages=[{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.b64encode(data).decode()}},
            {"type": "text",
             "text": DESCRIBE.format(fonts=", ".join(fonts) or "(none embedded)")},
        ]}])
    usage_mod.record(response)
    return tr.extract_json(response)


def detect(client, data: bytes, filename: str, model: str) -> dict:
    """The style spec for an uploaded document. Never raises.

    Style is cosmetic; failing to read it should cost you the house template,
    not the resume you paid to have tailored.
    """
    try:
        names = (fonts_from_pdf(data) if filename.lower().endswith(".pdf")
                 else fonts_from_docx(data))
    except Exception:
        names = []

    try:
        judged = describe(client, data, filename, model)
    except Exception:
        judged = {}

    # Font roles come from the judgment, resolved against the real families
    # the file embeds. The model is good at "which of these is the body text"
    # and bad at naming a font it was not given.
    ordered = [judged.get(k) for k in ("body_font", "display_font", "heading_font")
               if judged.get(k)]
    spec = from_fonts(ordered or names)
    if judged.get("body_font"):
        body, gf_body = resolve(judged["body_font"])
        display, gf_display = resolve(judged.get("display_font") or judged["body_font"])
        heading, gf_heading = resolve(judged.get("heading_font") or judged.get("display_font")
                                      or judged["body_font"])
        body = body or spec["font_body"]
        # Anything monospaced in the document, whether or not the model
        # mentioned it. A resume set in one font must stay set in one font.
        # The monospace needs requesting too. Declaring 'Roboto Mono' in the
        # stylesheet without asking Google for it just falls back to whatever
        # monospace the renderer has, which is a different font at a different
        # width -- the dates stop lining up and the page height changes.
        mono_name = next((n for n in names if "mono" in clean_font_name(n)), "")
        mono, gf_mono = resolve(mono_name) if mono_name else (body, None)
        spec.update(font_body=body,
                    font_display=display or spec["font_display"],
                    font_heading=heading or display or spec["font_heading"],
                    font_mono=mono or body,
                    google_fonts=list(dict.fromkeys(
                        g for g in (gf_body, gf_display, gf_heading, gf_mono) if g))
                    or spec["google_fonts"])
    # Colour comes from the file, not from looking at it. Model judgment is
    # kept only as the fallback for a document the parser cannot read.
    try:
        exact = colours_for(data) if filename.lower().endswith(".pdf") else {}
    except Exception:
        exact = {}
    try:
        geometry = layout_from_pdf(data) if filename.lower().endswith(".pdf") else {}
    except Exception:
        geometry = {}
    merged = merge(spec, {**judged, **exact})
    # Geometry is measured, so it wins outright. If there is no sidebar, any
    # sections the model wanted to put in one are dropped with it.
    if geometry:
        merged.update(geometry)
        if not merged.get("sidebar_sections"):
            merged["sidebar_sections"] = ["contact", "skills", "education"]
    else:
        merged["layout"] = "single"
        merged["sidebar_sections"] = []
    return merged
