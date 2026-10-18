"""Publish a Shepherd's Warning issue to the site.

An issue is a Markdown file with a small front matter block:

    ---
    date: 2026-10-21
    title: One wallet printed a billion dollars
    summary: One line for the archive list.
    kind: weekly            # or daily
    ---
    # Heading ...

Every Markdown table in the body becomes a sheet in the issue's CSV (one CSV per issue, tables separated by a line
naming the table), the body becomes an HTML fragment the site renders inline, and a PDF is made from that fragment.
The archive index site/warning/issues.json is rewritten from the files present, newest first.

Usage: python scripts/publish_issue.py path/to/issue.md
"""
import os, sys, re, json, csv, io, html, subprocess, tempfile, shutil

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "site", "warning")

def front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m: sys.exit("no front matter")
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in meta.items()}, m.group(2)

def inline(md):
    s = html.escape(md, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    return s

def to_html_and_tables(body):
    """Markdown subset: #/## headings, paragraphs, - lists, | tables |, blank-line separated."""
    out, tables = [], []
    blocks = re.split(r"\n\s*\n", body.strip())
    for b in blocks:
        lines = b.strip().splitlines()
        if lines[0].startswith("#"):
            level = min(len(lines[0]) - len(lines[0].lstrip("#")), 3)
            out.append(f"<h{level + 1}>{inline(lines[0].lstrip('#').strip())}</h{level + 1}>")
            rest = "\n".join(lines[1:]).strip()
            if rest: out.append(f"<p>{inline(rest)}</p>")
        elif all(l.lstrip().startswith("- ") for l in lines):
            out.append("<ul>" + "".join(f"<li>{inline(l.lstrip()[2:])}</li>" for l in lines) + "</ul>")
        elif lines[0].startswith("|") and len(lines) >= 2:
            rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines if not re.match(r"^\|?\s*:?-{2,}", l)]
            head, data = rows[0], rows[1:]
            name = f"table {len(tables) + 1}"
            tables.append((name, head, data))
            out.append('<div class="tblwrap"><table class="stats"><thead><tr>' + "".join(f"<th>{inline(h)}</th>" for h in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in data) + "</tbody></table></div>")
        else:
            out.append(f"<p>{inline(' '.join(l.strip() for l in lines))}</p>")
    return "\n".join(out), tables

def write_csv(path, tables, meta):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Shepherd's Warning", meta["date"], meta["title"]]); w.writerow([])
    for name, head, data in tables:
        w.writerow([name]); w.writerow(head)
        for r in data: w.writerow(r)
        w.writerow([])
    open(path, "w", newline="").write(buf.getvalue())

def write_pdf(path, fragment, meta):
    """A plain PDF from the fragment: headings, paragraphs, lists and tables, nothing clever."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem
    except ImportError:
        return False
    st = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=st["BodyText"], fontName="Times-Roman", fontSize=10.5, leading=14.5, spaceAfter=7)
    h1 = ParagraphStyle("h1", parent=st["Heading1"], fontName="Times-Bold", fontSize=20, leading=24, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=st["Heading2"], fontName="Times-Bold", fontSize=13.5, leading=17, spaceBefore=12, spaceAfter=6)
    meta_s = ParagraphStyle("m", parent=body, textColor=colors.HexColor("#666666"), fontSize=9)
    cell = ParagraphStyle("c", parent=body, fontSize=8.2, leading=10.5, spaceAfter=0)
    def clean(h):  # the fragment's inline html is a subset reportlab's Paragraph understands, bar <code>
        return h.replace("<code>", "<font face='Courier'>").replace("</code>", "</font>")
    story = [Paragraph(f"Shepherd's Warning · {html.escape(meta['date'])} · Tethera Labs · tetheralabs.com", meta_s),
             Paragraph(html.escape(meta["title"]), h1)]
    for m in re.finditer(r"<(h2|h3|h4|p|ul|div class=\"tblwrap\")>(.*?)</(?:h2|h3|h4|p|ul|div)>", fragment, re.S):
        tag, inner = m.group(1), m.group(2)
        if tag in ("h2", "h3", "h4"):
            story.append(Paragraph(clean(inner), h2 if tag != "h2" else h1 if False else h2))
        elif tag == "p":
            story.append(Paragraph(clean(inner), body))
        elif tag == "ul":
            items = re.findall(r"<li>(.*?)</li>", inner, re.S)
            story.append(ListFlowable([ListItem(Paragraph(clean(i), body), leftIndent=12) for i in items], bulletType="bullet", start="•", leftIndent=14))
        else:
            rows = re.findall(r"<tr>(.*?)</tr>", inner, re.S)
            data = [[Paragraph(clean(c), cell) for c in re.findall(r"<t[hd]>(.*?)</t[hd]>", r, re.S)] for r in rows]
            if not data: continue
            ncol = max(len(r) for r in data); width = (A4[0] - 30 * mm) / ncol
            t = Table(data, colWidths=[width] * ncol, repeatRows=1)
            t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#bbbbbb")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#666666")), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]))
            story += [t, Spacer(1, 8)]
    story.append(Paragraph("Measured on Robinhood Chain mainnet. Every number is checkable against the chain; the pool and transaction ids are in the text.", meta_s))
    try:
        SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                          title=meta["title"], author="Tethera Labs").build(story)
        return True
    except Exception:
        if os.path.exists(path): os.remove(path)
        return False

def main(src):
    meta, body = front_matter(open(src).read())
    slug = meta["date"] + ("-daily" if meta.get("kind") == "daily" else "")
    os.makedirs(OUT, exist_ok=True)
    fragment, tables = to_html_and_tables(body)
    open(os.path.join(OUT, slug + ".html"), "w").write(fragment)
    write_csv(os.path.join(OUT, slug + ".csv"), tables, meta)
    pdf_ok = write_pdf(os.path.join(OUT, slug + ".pdf"), fragment, meta)
    shutil.copy(src, os.path.join(OUT, slug + ".md"))
    issues = []
    for f in sorted(os.listdir(OUT), reverse=True):
        if f.endswith(".md"):
            m, _ = front_matter(open(os.path.join(OUT, f)).read()); s = f[:-3]
            issues.append({"slug": s, "date": m["date"], "title": m["title"], "summary": m.get("summary", ""), "kind": m.get("kind", "weekly"),
                           "html": f"warning/{s}.html", "csv": f"warning/{s}.csv", "pdf": f"warning/{s}.pdf" if os.path.exists(os.path.join(OUT, s + ".pdf")) else None,
                           "md": f"warning/{s}.md"})
    json.dump({"issues": issues}, open(os.path.join(OUT, "issues.json"), "w"), indent=1)
    print(f"published {slug}: html, csv{', pdf' if pdf_ok else ' (pdf failed)'}; {len(issues)} issue(s) in the index")

if __name__ == "__main__":
    main(sys.argv[1])
