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
