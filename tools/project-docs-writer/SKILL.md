---
name: project-docs-writer
description: Build clear, structured documentation and PDF files from the Markdown files that already exist in a project. Use when asked to write, update, or reorganize documentation, reports, READMEs, summaries, or scientific / non-technical write-ups, or to export them as PDF. Includes rules for Persian (RTL) documents and PDFs.
---

# Project Docs Writer

## Purpose

Create documentation and PDF files **for the project owner's own use**: project notes, research write-ups, status reports, and reference documents. Everything is built from what the project already contains (its Markdown files), never from guesses.

Deliverables, in this order of priority:

1. **Markdown** – the source of truth. Always created first.
2. **PDF** – built *from* the Markdown with `scripts/build_pdf.py`. Never write the PDF by hand or let it drift from the Markdown.

Style goal: **simple, structured, readable**.

Two writing modes:

- **Scientific** – precise, neutral, evidence-based (methods, experiments, results).
- **Non-technical** – plain language, outcome first (overviews, summaries, status reports).

If the mode is not stated, choose by audience. If still unclear, ask one short question.

---

## Core Principles

1. **Ground everything in the sources.** Never invent features, numbers, results, or file names.
2. **Keep it simple.** Short sentences, one idea per paragraph, no filler.
3. **Keep it structured.** Predictable headings and section order.
4. **Be consistent.** One term per concept across all documents.
5. **Mark unknowns.** Write `TODO:` with what is missing instead of assuming.
6. **Write for the owner.** The main reader is the project owner. Skip onboarding boilerplate, badges, and marketing text unless asked.

---

## Workflow

### 1. Discover sources
- List the project's Markdown files (`README.md`, `docs/`, `notes/`, `CHANGELOG.md`, subfolder `*.md`). Start with `README.md` or an index.
- Read only what is relevant to the request.
- Use configs, folder names, and entry points only to confirm facts the Markdown mentions.
- Ignore `node_modules/`, `.venv/`, `dist/`, `build/`, `.git/`, and other generated folders.

### 2. Map sources to sections
Note which file supports which section, where files contradict each other (report it, do not silently pick one), and what is missing.

### 3. Pick a template
Use a template below. Remove sections that would be empty.

### 4. Write the Markdown
Follow the style rules. Link to source files with relative links.

### 5. Review
- [ ] Every fact traces back to a project file
- [ ] Headings are hierarchical, one `#` title
- [ ] Terms, names, numbers are consistent
- [ ] Links and paths are correct
- [ ] No empty sections; only intentional `TODO:` items remain
- [ ] Persian documents pass the RTL checklist

### 6. Build the PDF (when requested, or when the document is a report/deliverable)
See **PDF Build** below.

### 7. Report back
State briefly: files created or changed, sources used, conflicts, open `TODO:` items, and the PDF path if one was built.

---

## Style Rules

### General
- Open with 2–4 lines: what this document is and what it is for.
- Paragraphs of about 3–5 lines.
- Use lists only for real lists; use prose for reasoning.
- Tables for comparisons, settings, and metrics.
- Code, commands, paths, and identifiers in backticks; multi-line code in fenced blocks with a language tag.
- Define abbreviations on first use.
- Give numbers with units (`32 GB`, `0.85 F1`, `120 s`).
- Bold only for key terms on first use.

### Scientific style
- Neutral and precise: what was done, how, and what was observed.
- Separate **observation** from **interpretation**.
- Give enough setup to reproduce: data, versions, parameters, environment.
- Do not overclaim; use "suggests" when evidence is limited.
- State limitations explicitly.

### Non-technical style
- Lead with the outcome and why it matters.
- Replace jargon with plain words; explain unavoidable terms in one sentence.
- Use short examples where they help.
- Move details to an appendix or link to the technical document.

---

## Templates

### A. Project overview
```markdown
# Project Name

What it is, what problem it solves, who uses it.

## Goals
## Scope
## Structure
## How to Run / Use
## Current Status
## Related Documents
```

### B. Scientific / technical report
```markdown
# Title

## Summary
## Background and Motivation
## Method
## Experimental Setup
## Results
## Discussion
## Limitations
## Conclusion
## References
```

### C. Non-technical status report
```markdown
# Title

## Summary
## What Was Done
## Key Results
## Open Issues
## Next Steps
```

### D. Component or module guide
```markdown
# Component Name

## Role
## Inputs and Outputs
## Main Files
## Configuration
## Usage Example
## Notes
```

---

## PDF Build

Use `scripts/build_pdf.py` (next to this file). It converts Markdown to an A4 PDF through Chromium, which handles Persian shaping and mixed Persian/English text correctly. It auto-detects RTL.

### One-time setup
```powershell
python -m pip install markdown playwright
python -m playwright install chromium
```
If the script is not in the project yet, copy it to `tools/build_pdf.py` (or the project's existing scripts folder).

### Build
```powershell
python tools/build_pdf.py docs/overview.md
python tools/build_pdf.py docs/overview.fa.md -o docs/pdf/overview.fa.pdf
python tools/build_pdf.py docs/notes.md --title "Project Notes"
python tools/build_pdf.py docs/x.md --rtl      # force RTL
```

### Rules
- Build the PDF **from the final Markdown**. If the content changes, change the Markdown and rebuild.
- Keep the PDF next to its source (`overview.pdf`) or in `docs/pdf/`. Follow the project's existing layout first.
- Use the same base name as the Markdown file (`overview.fa.md` → `overview.fa.pdf`).
- Use relative image paths in Markdown so images appear in the PDF.
- Do not commit the temporary `.build.html` file; the script deletes it automatically.

### Verify the PDF
After building, check:
- The file exists and has a sensible page count.
- Tables do not overflow the page; long code lines wrap.
- Headings are not stranded at the bottom of a page.
- **Persian PDFs:** text is connected (not isolated letters), reads right-to-left, punctuation sits on the correct side, and English terms and code stay intact.

If tools are available (e.g. `pdftoppm`, PyMuPDF), render the first page to an image and look at it. If the rendering is wrong, fix the Markdown or the CSS in the script, then rebuild. If the tools are not available, say so instead of claiming it was checked.

### Fonts
- Preferred Persian font: **Vazirmatn** (install it on the machine).
- Fallbacks already in the CSS: Segoe UI, Tahoma. Both render Persian on Windows.
- Do not download fonts from the internet during the build without telling the user.

### Fallback if Chromium is unavailable
Pandoc with XeLaTeX also works:
```powershell
pandoc docs/overview.fa.md -o docs/overview.fa.pdf --pdf-engine=xelatex -V dir=rtl -V lang=fa -V mainfont="Vazirmatn" -V monofont="Consolas"
```
This needs Pandoc and a LaTeX distribution. Mixed Persian/English layout is usually less reliable than with Chromium, so verify it carefully.

---

## Persian Documents (RTL)

### 1. Direction and language

Markdown has no native RTL switch, so declare direction explicitly. Wrap the Persian content in a `div` and **leave a blank line after the opening tag and before the closing tag**, otherwise Markdown inside will not render.

```markdown
<div dir="rtl" lang="fa">

# عنوان سند

متن فارسی در اینجا نوشته می‌شود.

</div>
```

- One wrapper for the whole document, unless it deliberately mixes languages.
- Persian files: `README.fa.md` or `docs/fa/overview.fa.md`. Keep English versions separate.
- Do not translate file names, code identifiers, or commands.
- The PDF script reads this wrapper (or the `.fa.` file name, or the share of Persian text) and sets RTL automatically.

### 2. Code and LTR content
- Code blocks, commands, paths, URLs, and formulas stay **left-to-right** in fenced blocks or backticks.
- Product and library names (PyTorch, Neo4j) stay in Latin letters, written as plain text.
- Do not break a Persian sentence around an English term; rephrase if the line becomes visually messy.

### 3. Typography and punctuation
- Persian punctuation: `،` `؛` `؟`. No Latin comma or question mark in Persian sentences.
- Correct half-space (ZWNJ, U+200C): `می‌شود`، `نمی‌کند`، `کتاب‌ها`، `ارزیابی‌های`.
- Persian `ی` and `ک` only, never Arabic `ي` and `ك`.
- No space before `،` `؛` `؟` `.`; one space after.
- Quotations with `«…»`.
- One digit style per document: Persian (۱۲۳) or Latin (123). For metrics, versions, and code prefer Latin digits. Follow the user's stated preference.
- No kashida (ـ) stretching.

### 4. Terminology
- Use the established Persian term when one exists; keep the English term when the Persian one is unclear or rarely used.
- First use: «شبکه عصبی گرافی (Graph Neural Network)». After that, one form only.
- Add a small glossary table for term-heavy documents.

### 5. Structure in RTL
- Tables work in RTL; the first column appears on the right. Keep cells short.
- Lists at most two levels deep.
- Avoid long mixed Persian/English lines.

### 6. Tone
- Clear, standard, formal-neutral Persian. No colloquial forms in scientific documents.
- Short sentences; avoid long chains of «که… که…».
- Avoid literal translation from English; rephrase so it reads naturally.
- Prefer active voice when it keeps the sentence clear.

### 7. RTL checklist
- [ ] Content wrapped in `<div dir="rtl" lang="fa">` with blank lines inside
- [ ] Code, paths, URLs in fenced blocks or backticks
- [ ] Persian punctuation and correct half-spaces
- [ ] Persian `ی` / `ک` only
- [ ] One digit style across the document
- [ ] English terms introduced once in parentheses, then consistent
- [ ] PDF checked: connected letters, RTL order, intact code

---

## Do and Don't

**Do**
- Reuse the project's own names, terms, and structure.
- Keep each file focused on one purpose.
- Link to other documents instead of duplicating content.
- Update the index or README when adding a document.

**Don't**
- Invent results, metrics, or features.
- Copy large parts of existing files; summarize and link.
- Overwrite existing documents without telling the user.
- Edit a PDF directly; always rebuild from Markdown.
- Mix scientific and casual styles in one document.

---

## Output Conventions

- Markdown: lowercase, hyphenated English file names (`system-overview.md`); `.fa` suffix for Persian (`system-overview.fa.md`).
- Documents go in `docs/` unless the project already uses another convention.
- Add a short table of contents for documents longer than about five sections.
- End with **Related Documents** or **References** when sources exist.
- Suggested layout:

```text
project-docs-writer/
├── SKILL.md
└── scripts/
    └── build_pdf.py
```
