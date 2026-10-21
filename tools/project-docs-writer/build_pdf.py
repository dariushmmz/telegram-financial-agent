#!/usr/bin/env python3
"""Build a clean A4 PDF from a Markdown file (Persian / RTL aware).

Renders through Chromium (Playwright), which shapes Persian text and mixed
Persian/English lines correctly.

Setup (once):
    python -m pip install markdown playwright
    python -m playwright install chromium

Usage:
    python build_pdf.py docs/overview.fa.md
    python build_pdf.py docs/report.md -o out/report.pdf --title "Report"
    python build_pdf.py docs/x.md --rtl     # force right-to-left
    python build_pdf.py docs/x.md --ltr     # force left-to-right
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("Missing dependency. Run: python -m pip install markdown playwright")

CSS = """
@page { size: A4; }
html { font-size: 11pt; }
body {
  font-family: "Vazirmatn", "Segoe UI", Tahoma, "DejaVu Sans", sans-serif;
  line-height: 1.8; color: #1a1a1a; margin: 0;
}
body.rtl { direction: rtl; text-align: right; }
body.ltr { direction: ltr; text-align: left; }
h1, h2, h3, h4 { line-height: 1.4; page-break-after: avoid; }
h1 { font-size: 1.8em; margin: 0 0 .6em; padding-bottom: .3em; border-bottom: 2px solid #333; }
h2 { font-size: 1.4em; margin: 1.6em 0 .5em; border-bottom: 1px solid #ccc; padding-bottom: .2em; }
h3 { font-size: 1.15em; margin: 1.3em 0 .4em; }
p, li { orphans: 3; widows: 3; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .95em; }
th, td { border: 1px solid #bbb; padding: 6px 10px; vertical-align: top; }
th { background: #f0f0f0; }
tr { page-break-inside: avoid; }
pre, code {
  direction: ltr; text-align: left; unicode-bidi: isolate;
  font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
}
code { background: #f3f3f3; padding: 1px 5px; border-radius: 3px; font-size: .9em; }
pre {
  background: #f6f6f6; border: 1px solid #ddd; border-radius: 4px;
  padding: 10px 12px; font-size: .85em; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; page-break-inside: avoid;
}
pre code { background: none; padding: 0; }
blockquote { margin: 1em 0; padding: .2em 1em; color: #444; background: #fafafa; }
body.rtl blockquote { border-right: 4px solid #999; }
body.ltr blockquote { border-left: 4px solid #999; }
img { max-width: 100%; }
a { color: #1a4fa0; text-decoration: none; }
hr { border: 0; border-top: 1px solid #ccc; margin: 1.5em 0; }
"""

TEMPLATE = """<!doctype html>
<html lang="__LANG__" dir="__DIR__">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body class="__DIR__">
__BODY__
</body>
</html>
"""

WRAP_OPEN = re.compile(r'^\s*<div\s+[^>]*dir\s*=\s*"rtl"[^>]*>\s*$', re.I | re.M)


def strip_rtl_wrapper(text: str) -> tuple[str, bool]:
    """Remove the <div dir="rtl"> wrapper used in the .md source (the PDF
    sets direction on <body> instead)."""
    m = WRAP_OPEN.search(text)
    if not m:
        return text, False
    text = text[: m.start()] + text[m.end():]
    idx = text.rfind("</div>")
    if idx != -1:
        text = text[:idx] + text[idx + len("</div>"):]
    return text, True


def persian_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    fa = sum(1 for c in letters if "\u0600" <= c <= "\u06ff")
    return fa / len(letters)


def first_heading(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


def build_html(md_text: str, rtl: bool | None, title: str | None, name_hint: str = "") -> str:
    md_text, had_wrapper = strip_rtl_wrapper(md_text)
    if rtl is None:
        rtl = had_wrapper or ".fa." in name_hint or persian_ratio(md_text) > 0.3
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list", "footnotes"],
        output_format="html5",
    )
    body = (
        body.replace("<li>[ ] ", "<li>\u2610 ")
        .replace("<li>[x] ", "<li>\u2611 ")
        .replace("<li>[X] ", "<li>\u2611 ")
    )
    title = title or first_heading(md_text, "Document")
    return (
        TEMPLATE.replace("__LANG__", "fa" if rtl else "en")
        .replace("__DIR__", "rtl" if rtl else "ltr")
        .replace("__TITLE__", title)
        .replace("__CSS__", CSS)
        .replace("__BODY__", body)
    )


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Missing Playwright. Run:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium"
        )
    footer = (
        '<div style="font-family: Tahoma, Arial, sans-serif; font-size: 9px; '
        'width: 100%; text-align: center; color: #666;">'
        '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=footer,
            margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
        )
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Markdown to PDF (RTL/Persian aware)")
    ap.add_argument("input", type=Path, help="Markdown file")
    ap.add_argument("-o", "--output", type=Path, help="PDF path (default: next to input)")
    ap.add_argument("--title", help="Document title (default: first # heading)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--rtl", action="store_true", help="force right-to-left")
    grp.add_argument("--ltr", action="store_true", help="force left-to-right")
    ap.add_argument("--keep-html", action="store_true", help="keep the intermediate HTML")
    args = ap.parse_args()

    src = args.input.resolve()
    if not src.is_file():
        sys.exit(f"Input not found: {src}")
    pdf = (args.output or src.with_suffix(".pdf")).resolve()
    rtl = True if args.rtl else False if args.ltr else None

    html = build_html(src.read_text(encoding="utf-8"), rtl, args.title, src.name)
    tmp = src.with_name(f".{src.stem}.build.html")  # same folder so relative images resolve
    tmp.write_text(html, encoding="utf-8")
    try:
        render_pdf(tmp, pdf)
    finally:
        if not args.keep_html:
            tmp.unlink(missing_ok=True)
    print(f"PDF written: {pdf}")


if __name__ == "__main__":
    main()
