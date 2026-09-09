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
    "font_body": "'IBM Plex Sans', sans-serif",
    "font_mono": "'IBM Plex Mono', monospace",
    "google_fonts": ["Space+Grotesk:wght@700",
                     "IBM+Plex+Sans:wght@400;500;600",
                     "IBM+Plex+Mono:wght@400;500;600"],
    "accent": "#1F6E4A",
    "ink": "#171D1A",
    "header_align": "left",     # left | center
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
    for prefix, (family, spec, generic) in SELF_HOSTED.items():
        if key.startswith(prefix) or prefix in key:
            return f"'{family}', {generic}", spec
    for prefix, (_family, stack, spec) in FONT_MAP.items():
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
    # A plain Word resume is set in exactly one font. Falling back to a generic
    # monospace for dates and taglines puts type on the page that the original
    # never had, which is the opposite of matching it. Only use a monospace if
    # the document actually contains one.
    mono = next((s for s in stacks if "mono" in s.lower()), body)
    spec.update(font_body=body, font_display=display, font_mono=mono,
                google_fonts=google)
    return spec


def merge(base: dict, judged: dict) -> dict:
    """Model judgment on top of the deterministic base, keys we allow only."""
    out = dict(base)
    for key in ("chrome", "accent", "ink", "header_align", "density"):
        value = (judged or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if out["chrome"] not in ("plain", "designed"):
        out["chrome"] = "designed"
    if out["header_align"] not in ("left", "center"):
        out["header_align"] = "left"
    if out["density"] not in ("normal", "compact"):
        out["density"] = "normal"
    for key in ("accent", "ink"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", out.get(key, "")):
            out[key] = DEFAULTS[key]
    return out


DESCRIBE = """Describe only the VISUAL STYLE of this resume. Ignore what it says.

The fonts actually embedded in the file are: {fonts}
Name body_font and display_font from that list. If the list is empty or none of
them fit what you see, name the family you actually see. Do not invent a font
that is not there -- a wrong family changes every line break in the output.

Return ONLY this JSON:
{{
  "chrome": "plain" | "designed",
  "accent": "#rrggbb",
  "ink": "#rrggbb",
  "header_align": "left" | "center",
  "density": "normal" | "compact",
  "body_font": "family name",
  "display_font": "family name for the name and section headings"
}}

- "plain": black text on white, no colour blocks, no cards, no rules beyond a
  thin line under section headings. Most Word and Google Docs resumes.
- "designed": colour used deliberately, tinted backgrounds, boxes, sidebars,
  or a visual device like a timeline.
- "accent": the one colour used for emphasis. If the resume is entirely black
  and grey, return "#000000" -- do not invent an accent it does not have.
- "ink": the body text colour, usually near-black.
- "density": "compact" if it is packed to fit a page, "normal" otherwise."""


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
    ordered = [judged.get(k) for k in ("body_font", "display_font") if judged.get(k)]
    spec = from_fonts(ordered or names)
    if judged.get("body_font"):
        body, gf_body = resolve(judged["body_font"])
        display, gf_display = resolve(judged.get("display_font") or judged["body_font"])
        body = body or spec["font_body"]
        # Anything monospaced in the document, whether or not the model
        # mentioned it. A resume set in one font must stay set in one font.
        mono = next((resolve(n)[0] for n in names if "mono" in clean_font_name(n)), body)
        spec.update(font_body=body,
                    font_display=display or spec["font_display"],
                    font_mono=mono,
                    google_fonts=list(dict.fromkeys(
                        g for g in (gf_body, gf_display) if g)) or spec["google_fonts"])
    return merge(spec, judged)
