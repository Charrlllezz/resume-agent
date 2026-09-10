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

import pdfread

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
    """[(hex, weight)] for text fill colours. Thin wrapper over pdfread."""
    try:
        return pdfread.read(data).get("colours") or []
    except Exception:
        return []


def _is_grey(hex_colour: str, tolerance: int = 18) -> bool:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b) <= tolerance


def _luma(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def colours_for(data: bytes) -> dict:
    """ink and up to two accents, from the file's own numbers."""
    try:
        return _ink_and_accents(pdfread.read(data).get("colours") or [])
    except Exception:
        return {}


def layout_from_pdf(data: bytes) -> dict:
    """Column geometry, or {} for a single column. Measured, not judged.

    A sidebar is a tall filled block down one edge, and pdfplumber reports it
    in page coordinates already. This used to walk the content stream by hand
    and track the transformation matrix through q/Q/cm, because an
    untransformed rectangle width reported a 34% sidebar as 45%.
    """
    try:
        facts = pdfread.read(data)
    except Exception:
        return {}
    panel = facts.get("panel")
    if not panel:
        return {}
    width = (facts.get("page") or (612.0, 792.0))[0]
    return {"layout": "two-column", "sidebar_side": panel["side"],
            "sidebar_width": max(22, min(45, round(100 * panel["width"] / width))),
            "sidebar_bg": panel["colour"]}


def fonts_from_pdf(data: bytes) -> list:
    """The font families the file embeds, most-used first."""
    try:
        return list(dict.fromkeys(
            f for f, _s, _n in pdfread.read(data).get("fonts") or []))
    except Exception:
        return []


# The model is asked two things now. Everything else about how a resume looks
# is a number in the file, and reading it beats judging it: asked for the
# colours of a resume set in #0B3C5D and #B85C1E, it returned #1b3a5c and
# #c05a26 -- close enough to look right, wrong enough to be someone else's
# brand colour.
DESCRIBE = """Describe only the VISUAL STYLE of this resume. Ignore what it says.

Return ONLY this JSON:
{{
  "chrome": "plain" | "designed",
  "density": "normal" | "compact"
}}

- "plain": black text on white, no colour blocks, no cards, no rules beyond a
  thin line under section headings. Most Word and Google Docs resumes.
- "designed": colour used deliberately, tinted backgrounds, boxes, sidebars,
  or a visual device of some kind.
- "density": "compact" if it is packed to fit a page, "normal" otherwise.

For reference, the fonts embedded in the file are: {fonts}"""


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


def roles_from_fonts(fonts: list) -> dict:
    """Assign the four type roles from [(family, size, chars)].

    Size and usage decide it, which is a fact about the page. This was the
    model's job and it was the weakest part of the whole feature: asked which
    face the headings were in, it answered with the name's face and a resume
    lost one of its typefaces outright.

    The body is the most-used. The name is the largest. Headings are whatever
    is left, preferring something set between the two.
    """
    if not fonts:
        return {}
    body_family, body_size, _ = max(fonts, key=lambda f: f[2])
    display_family, display_size, _ = max(fonts, key=lambda f: (f[1], f[2]))

    def is_mono(family):
        return any(m in family for m in ("mono", "courier", "consol"))

    mono = next((f for f, _s, _n in fonts if is_mono(f)), "")

    between = [f for f in fonts
               if f[0] not in (body_family, display_family)
               and not is_mono(f[0]) and body_size < f[1] <= display_size]
    pool = between or [f for f in fonts
                       if f[0] not in (body_family, display_family) and not is_mono(f[0])]
    heading_family = max(pool, key=lambda f: f[2])[0] if pool else display_family
    return {"body": body_family, "display": display_family,
            "heading": heading_family, "mono": mono}


def _ink_and_accents(colours: list) -> dict:
    """ink plus up to two accents, from [(hex, chars)] ordered by use."""
    if not colours:
        return {}
    dark = [(c, n) for c, n in colours if _luma(c) < 0.55]
    ink = max(dark, key=lambda cn: cn[1])[0] if dark else colours[0][0]
    chromatic = [c for c, _ in colours if not _is_grey(c) and _luma(c) < 0.85]
    accent = chromatic[0] if chromatic else "#000000"
    return {"ink": ink, "accent": accent,
            "accent_2": chromatic[1] if len(chromatic) > 1 else accent}


def detect(client, data: bytes, filename: str, model: str) -> dict:
    """The style spec for an uploaded document. Never raises.

    Almost all of it is measured. The model is asked two questions only --
    whether the resume reads as plain or designed, and whether it is packed
    tight -- because those are judgments about a whole page and the rest are
    not.
    """
    is_pdf = filename.lower().endswith(".pdf")
    facts = {}
    if is_pdf:
        try:
            facts = pdfread.read(data)
        except Exception:
            facts = {}

    spec = dict(DEFAULTS)
    roles = roles_from_fonts(facts.get("fonts") or [])
    if roles:
        body, gf_body = resolve(roles["body"])
        display, gf_display = resolve(roles["display"])
        heading, gf_heading = resolve(roles["heading"])
        mono, gf_mono = resolve(roles["mono"]) if roles["mono"] else (body, gf_body)
        spec.update(font_body=body, font_display=display,
                    font_heading=heading, font_mono=mono or body,
                    google_fonts=list(dict.fromkeys(
                        g for g in (gf_body, gf_display, gf_heading, gf_mono) if g)))
    elif not is_pdf:
        try:
            spec = from_fonts(fonts_from_docx(data))
        except Exception:
            pass

    spec.update(_ink_and_accents(facts.get("colours") or []))

    panel = facts.get("panel")
    if panel:
        width = (facts.get("page") or (612.0, 792.0))[0]
        spec.update(layout="two-column", sidebar_side=panel["side"],
                    sidebar_width=max(22, min(45, round(100 * panel["width"] / width))),
                    sidebar_bg=panel["colour"],
                    sidebar_ink=facts.get("sidebar_ink") or "")
        sections = facts.get("sections") or {}
        in_side = [k for k, v in sections.items() if v == "side"
                   and k in ("contact", "skills", "education")]
        spec["sidebar_sections"] = in_side or ["contact", "skills", "education"]
    else:
        spec.update(layout="single", sidebar_sections=[])

    if facts.get("title_align"):
        spec["header_align"] = facts["title_align"]

    try:
        judged = describe(client, data, filename, model)
    except Exception:
        judged = {}
    # Only the two things that are not in the file as a number.
    return merge(spec, {k: v for k, v in (judged or {}).items()
                        if k in ("chrome", "density")})
