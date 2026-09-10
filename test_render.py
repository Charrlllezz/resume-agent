#!/usr/bin/env python3
"""Does the page actually look the way the style says? Run: python test_render.py

Free and offline like test_qa.py, but it needs a browser, so it is separate to
keep that suite instant.

This exists because three style bugs got through a passing test suite and were
caught by a person looking at a screenshot. All three were invisible to the
tests because the tests grepped the CSS *source*:

  - the plain overlay was emitted before the sheet it overrides, so every rule
    lost on order while still being present in the text a test searched
  - a serif font was declared with a sans-serif fallback
  - a one-font resume rendered its dates in a monospace the original never had

A string in a stylesheet is not evidence that a rule applied. So these assert
computed style on a rendered page: what the browser decided, after the cascade.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import tailor_resume as tr

RESUME = json.loads((Path(__file__).parent / "master_resume.example.json").read_text())

PLAIN_SERIF = {
    "chrome": "plain", "accent": "#000000", "ink": "#111111",
    "header_align": "center", "density": "normal",
    "font_body": "Tinos, 'Times New Roman', serif",
    "font_display": "Tinos, 'Times New Roman', serif",
    "font_mono": "Tinos, 'Times New Roman', serif",
    "google_fonts": ["Tinos:wght@400;700"],
}

failures = []


def expect(condition, label):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def sample(style: dict | None) -> tuple:
    resume = dict(RESUME)
    if style:
        resume["style"] = style
    else:
        resume.pop("style", None)
    tailored = {
        "headline": resume["headlines"]["gtm"][0],
        "experience": [{"company": r["company"], "title": r["titles"]["gtm"],
                        "location": r["location"], "dates": r["dates"],
                        "bullets": r["bullets"]["gtm"][:3]}
                       for r in resume["experience"][:2]],
        "skills": {k: list(v)[:5] for k, v in list(resume["skills"]["gtm"].items())[:2]},
    }
    return tr.render_html(tailored, resume), resume


# Every distinct font-family declared anywhere on the rendered page. The
# generic check: a page may only use type its style actually asked for. This
# catches a whole class of bug rather than one element at a time.
FONTS_USED = """() => {
  const seen = new Set();
  document.querySelectorAll('body *').forEach(el => {
    if (!el.textContent.trim()) return;
    seen.add(getComputedStyle(el).fontFamily);
  });
  seen.add(getComputedStyle(document.body).fontFamily);
  return [...seen];
}"""

LOOK = """() => {
  const cs = s => { const e = document.querySelector(s); return e ? getComputedStyle(e) : null; };
  const card = cs('.card'), tl = cs('.timeline::before') || null;
  const tlEl = document.querySelector('.timeline');
  const before = tlEl ? getComputedStyle(tlEl, '::before') : null;
  return {
    body_bg_image: getComputedStyle(document.body).backgroundImage,
    card_border: card ? card.borderTopWidth : null,
    card_radius: card ? card.borderTopLeftRadius : null,
    card_shadow: card ? card.boxShadow : null,
    timeline_before: before ? before.display : null,
    hero_align: cs('.hero') ? cs('.hero').textAlign : null,
    accent: cs('.card-head .at') ? cs('.card-head .at').color : null,
    pill_border: cs('.pill') ? cs('.pill').borderTopWidth : null,
    metric_bg: cs('.bullets .m') ? cs('.bullets .m').backgroundColor : null,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}"""


def normalise(stack: str) -> str:
    return stack.replace('"', "'").replace(", ", ",").lower()


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()

        print("plain style (a Word resume must come back looking like one):")
        html, _ = sample(PLAIN_SERIF)
        page = browser.new_page(viewport={"width": 860, "height": 1100})
        page.set_content(html, wait_until="networkidle")
        look = page.evaluate(LOOK)
        fonts = page.evaluate(FONTS_USED)

        allowed = {normalise(PLAIN_SERIF[k]) for k in
                   ("font_body", "font_display", "font_mono")}
        stray = [f for f in fonts if normalise(f) not in allowed]
        expect(not stray, f"every font on the page was asked for (stray: {stray})")
        expect(all("serif" in normalise(f) for f in fonts),
               "a serif style renders serif everywhere")
        expect("mono" not in " ".join(fonts).lower(),
               "no monospace appears when the document had none")

        expect(look["body_bg_image"] == "none", "plain has no patterned canvas")
        expect(look["card_border"] == "0px", "plain has no card borders")
        expect(look["card_radius"] == "0px", "plain has square corners")
        expect(look["card_shadow"] in ("none", None), "plain has no card shadows")
        expect(look["timeline_before"] == "none", "plain hides the timeline rule")
        expect(look["pill_border"] == "0px", "plain has no skill pills")
        expect(look["metric_bg"] == "rgba(0, 0, 0, 0)", "plain does not tint metrics")
        expect(look["hero_align"] == "center", "a centred header is centred")
        expect(look["overflow"] <= 0, "nothing overflows the page width")

        print("\nhouse style (unchanged by any of the above):")
        html, _ = sample(None)
        page = browser.new_page(viewport={"width": 860, "height": 1100})
        page.set_content(html, wait_until="networkidle")
        look = page.evaluate(LOOK)
        expect(look["card_radius"] == "10px", "cards keep their corners")
        expect(look["body_bg_image"].startswith("radial-gradient"), "the dotted canvas is back")
        expect(look["timeline_before"] != "none", "the timeline is visible")
        expect(look["accent"] == "rgb(31, 110, 74)", "the house accent is unchanged")
        expect(look["hero_align"] == "left", "the header is left aligned")
        expect(look["overflow"] <= 0, "nothing overflows the page width")

        print("\naccent colour actually reaches the page:")
        html, _ = sample({**PLAIN_SERIF, "accent": "#B4341F"})
        page = browser.new_page(viewport={"width": 860, "height": 1100})
        page.set_content(html, wait_until="networkidle")
        heading = page.evaluate(
            "() => getComputedStyle(document.querySelector('.section-label')).color")
        expect(heading == "rgb(180, 52, 31)",
               f"a detected accent reaches the page in plain chrome (got {heading})")

        # And black stays black, rather than a tint creeping in.
        html, _ = sample(PLAIN_SERIF)
        page = browser.new_page(viewport={"width": 860, "height": 1100})
        page.set_content(html, wait_until="networkidle")
        heading = page.evaluate(
            "() => getComputedStyle(document.querySelector('.section-label')).color")
        expect(heading == "rgb(0, 0, 0)",
               f"a black-and-white resume stays black and white (got {heading})")


        print("\nevery declared font is actually requested:")
        for spec_name, spec_val in [("plain serif", PLAIN_SERIF)]:
            html, resume = sample(spec_val)
            declared = {tr.style_for(resume)[k] for k in
                        ("font_body", "font_display", "font_heading", "font_mono")}
            requested = " ".join(tr.style_for(resume)["google_fonts"]).replace("+", " ").lower()
            missing = [d for d in declared
                       if d.split(",")[0].strip("'\" ").lower() not in requested
                       and d.split(",")[0].strip("'\" ").lower()
                       not in ("serif", "sans-serif", "monospace", "ui-monospace")]
            expect(not missing,
                   f"{spec_name}: no font is declared without being loaded ({missing})")


        print("\ncolour comes out of the file, not out of a guess:")
        import style as style_mod
        swatch = """<!doctype html><meta charset=utf-8>
          <style>body{font-family:Arial;color:#2B2B2B;margin:0;padding:30px}
          h1{color:#0B3C5D}.a{color:#B85C1E}.b{color:#1F7A4C}</style>
          <h1>Heading in navy that carries some length to it</h1>
          <p>Body copy in near-black, deliberately the longest run of text here
             so that it wins on weight and is read as the ink colour.</p>
          <p class=a>A rust coloured line of text</p>
          <p class=b>A green coloured line of text</p>"""
        page = browser.new_page()
        page.set_content(swatch, wait_until="networkidle")
        page.pdf(path="/tmp/_swatch.pdf", format="Letter", print_background=True)
        found = style_mod.colours_for(Path("/tmp/_swatch.pdf").read_bytes())
        expect(found.get("ink") == "#2B2B2B",
               f"the ink colour is exact, not approximated (got {found.get('ink')})")
        expect({found.get("accent"), found.get("accent_2")} <= {"#0B3C5D", "#B85C1E", "#1F7A4C"},
               f"both accents are real colours from the file (got {found})")
        expect("#FFFFFF" not in found.values(), "the page colour is not mistaken for an accent")

        mono_page = """<!doctype html><meta charset=utf-8>
          <style>body{font-family:Arial;color:#000;margin:0;padding:30px}</style>
          <h1>Entirely black and white</h1><p>No colour anywhere on this page at all.</p>"""
        page = browser.new_page()
        page.set_content(mono_page, wait_until="networkidle")
        page.pdf(path="/tmp/_mono.pdf", format="Letter", print_background=True)
        found = style_mod.colours_for(Path("/tmp/_mono.pdf").read_bytes())
        expect(found.get("accent") == "#000000",
               f"a black and white resume gets no invented accent (got {found.get('accent')})")


        print("\nan uploaded resume never inherits this project's decoration:")
        # "designed" means the document used colour, not that it used a dotted
        # canvas and a timeline. A two-column resume lost its sidebar and
        # gained a timeline it never had, which is the failure this feature
        # exists to prevent.
        html, _ = sample({"chrome": "designed", "accent": "#C1662F",
                          "accent_2": "#1F3A5F", "ink": "#222222",
                          "font_body": "'Lato', sans-serif",
                          "google_fonts": ["Lato:wght@400;700"]})
        page = browser.new_page(viewport={"width": 880, "height": 1000})
        page.set_content(html, wait_until="networkidle")
        look = page.evaluate(LOOK)
        expect(look["body_bg_image"] == "none", "no dotted canvas on an uploaded resume")
        expect(look["timeline_before"] == "none", "no timeline on an uploaded resume")
        expect(look["card_border"] == "0px", "no cards on an uploaded resume")
        heading = page.evaluate(
            "() => getComputedStyle(document.querySelector('.section-label')).color")
        rule = page.evaluate(
            "() => getComputedStyle(document.querySelector('.section-label'), '::after').backgroundColor")
        expect(heading == "rgb(193, 102, 47)", f"their first colour is used (got {heading})")
        expect(rule == "rgb(31, 58, 95)", f"their second colour is used (got {rule})")


        print("\nlayout detection (measured, not guessed):")
        import style as style_mod

        two_col = """<!doctype html><meta charset=utf-8><style>
          body{font-family:Arial;margin:0;color:#222}
          .w{display:grid;grid-template-columns:34% 66%;min-height:100vh}
          .s{background:#1F3A5F;color:#fff;padding:30px 20px}
          .m{padding:30px 26px}</style>
          <div class=w><div class=s>
            <h3>Contact</h3><p>(555) 010-9911<br>someone@example.com<br>Austin, TX</p>
            <h3>Skills</h3><p>Salesforce, Marketo, SQL and Looker, Territory design</p>
            <h3>Education</h3><p>BS Economics, University of Texas, 2018</p></div>
          <div class=m><h1>Dana Whitfield</h1><h2>Experience</h2>
            <p>Revenue Operations Manager, Northgate Systems, 2023 to 2026</p>
            <ul><li>Rebuilt territory and quota planning for a 55-person sales
            organization, cutting planning cycle time from six weeks to nine days.</li></ul>
          </div></div>"""
        page = browser.new_page()
        page.set_content(two_col, wait_until="networkidle")
        page.pdf(path="/tmp/_two.pdf", format="Letter", print_background=True)
        got = style_mod.layout_from_pdf(Path("/tmp/_two.pdf").read_bytes())
        expect(got.get("layout") == "two-column", f"a sidebar is detected (got {got})")
        expect(got.get("sidebar_side") == "left", "on the correct side")
        expect(abs(got.get("sidebar_width", 0) - 34) <= 3,
               f"at the right width (got {got.get('sidebar_width')}, want 34)")
        expect(got.get("sidebar_bg") == "#1F3A5F",
               f"in the exact colour (got {got.get('sidebar_bg')})")

        # Right-floated dates are two runs and a few characters. Counting runs
        # rather than characters called a plain resume two-column.
        floated = """<!doctype html><meta charset=utf-8><style>
          body{font-family:Arial;margin:0;padding:40px;color:#222}
          .d{float:right;color:#777}</style>
          <h1>Someone Ordinary</h1>
          <p><span class=d>2023 - 2026</span><b>Senior Manager, Acme</b></p>
          <ul><li>A long single-column bullet that carries most of the characters
          on this page so that the column split has nothing to find here at all.</li>
          <li>Another substantial bullet of ordinary single-column body text.</li></ul>
          <p><span class=d>2020 - 2023</span><b>Manager, Other Company</b></p>
          <ul><li>More body text, again occupying the full width of the page.</li></ul>"""
        page = browser.new_page()
        page.set_content(floated, wait_until="networkidle")
        page.pdf(path="/tmp/_float.pdf", format="Letter", print_background=True)
        expect(not style_mod.layout_from_pdf(Path("/tmp/_float.pdf").read_bytes()),
               "right-floated dates are not mistaken for a sidebar")

        print("\ntwo-column rendering:")
        html, _ = sample({"layout": "two-column", "sidebar_side": "left",
                          "sidebar_width": 34, "sidebar_bg": "#1F3A5F",
                          "sidebar_sections": ["contact", "skills", "education"],
                          "accent": "#C1662F", "accent_2": "#1F3A5F", "ink": "#222222",
                          "font_body": "'Lato', sans-serif",
                          "google_fonts": ["Lato:wght@400;700"]})
        page = browser.new_page(viewport={"width": 844, "height": 900})
        page.set_content(html, wait_until="networkidle")
        laid = page.evaluate("""() => {
          const s = document.querySelector('.side'), m = document.querySelector('.main');
          const p = document.querySelector('.page');
          if (!s || !m) return null;
          const sb = s.getBoundingClientRect(), pb = p.getBoundingClientRect();
          return {pct: Math.round(100 * sb.width / pb.width),
                  bg: getComputedStyle(s).backgroundColor,
                  ink: getComputedStyle(s.querySelector('.section-label')).color,
                  left: sb.left < m.getBoundingClientRect().left,
                  full: sb.height >= pb.height - 2,
                  skills_in_side: !!s.querySelector('.skills'),
                  experience_in_main: !!m.querySelector('.timeline')};
        }""")
        expect(laid is not None, "a two-column style produces a sidebar in the markup")
        if laid:
            expect(laid["pct"] == 34, f"the sidebar is the detected width (got {laid['pct']}%)")
            expect(laid["bg"] == "rgb(31, 58, 95)", "in the detected colour")
            expect(laid["ink"] == "rgb(255, 255, 255)", "with legible text on a dark panel")
            expect(laid["left"], "on the detected side")
            expect(laid["full"], "running the full height of the page")
            expect(laid["skills_in_side"], "the assigned sections are in the sidebar")
            expect(laid["experience_in_main"], "experience stays in the main column")

        browser.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
