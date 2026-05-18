#!/usr/bin/env bash
# Convert Obsidian markdown to professional e-ink PDF (headless, no GUI)
#
# Usage:
#   obsidian-to-pdf.sh <input.md> [output.pdf]
#   obsidian-to-pdf.sh file1.md file2.md file3.md [-o output.pdf]
#   obsidian-to-pdf.sh --toc <input.md>
#
# Arguments:
#   input.md     One or more Obsidian-flavored markdown files
#   -o file.pdf  Explicit output path
#   --toc        Add visible TOC page (default: auto — on for multi-file, off for single)
#
# Multi-file mode:
#   Multiple .md files are combined into one PDF with page breaks between files.
#   Each file becomes a section. Order follows argument order.
#   Output defaults to {first-file}-collection.pdf
#
# Dependencies:
#   pandoc       Markdown to HTML conversion
#   weasyprint   HTML to PDF rendering
#   python3      Preprocessing (shared lib)
#   pip: pymupdf PDF metadata (optional — works without it)
#
# Features:
#   - Obsidian syntax preprocessing (wiki links, callouts, dataview, frontmatter)
#   - Mermaid diagram rendering (requires mmdc)
#   - Math rendering via SVG (LaTeX → codecogs)
#   - Colored checkboxes (green ✓, red ○, amber ◐)
#   - e-ink optimized styling (high contrast, no gradients)
#   - TOC with page numbers (WeasyPrint target-counter)
#
# Examples:
#   obsidian-to-pdf.sh "My Notes/learning-review.md"
#   obsidian-to-pdf.sh --toc chapter1.md chapter2.md -o book.pdf
#   obsidian-to-pdf.sh file.md ~/Desktop/output.pdf

set -euo pipefail

# --- Config ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_DIR="${OBSIDIAN_VAULT:-$HOME/vault}"
PAGE_SIZE="A4"
MARGIN="20mm"

# --- Help ---
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,/^$/p' "$0"
  exit 0
fi

# --- Args ---
TOC_FLAG="auto"
POSITIONAL=()
OUTPUT_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --no-toc) TOC_FLAG="" ;;
    --toc)    TOC_FLAG="--toc --toc-depth=2" ;;
    -o)       OUTPUT_FLAG="next" ;;
    *)
      if [[ "$OUTPUT_FLAG" == "next" ]]; then
        OUTPUT_FLAG="$arg"
      else
        POSITIONAL+=("$arg")
      fi
      ;;
  esac
done

if [[ ${#POSITIONAL[@]} -eq 0 ]]; then
  echo "Usage: obsidian-to-pdf.sh [--toc|--no-toc] [-o output.pdf] <input.md> [input2.md ...]" >&2
  exit 1
fi

# Multi-file mode: strip trailing .pdf arg (backward compat), then check count
MULTI_FILE=false
if [[ ${#POSITIONAL[@]} -gt 1 ]]; then
  LAST="${POSITIONAL[${#POSITIONAL[@]}-1]}"
  if [[ "$LAST" == *.pdf ]]; then
    OUTPUT_FLAG="$LAST"
    unset 'POSITIONAL[${#POSITIONAL[@]}-1]'
  fi
fi
if [[ ${#POSITIONAL[@]} -gt 1 ]]; then
  MULTI_FILE=true
fi

# Resolve input(s) to absolute paths
INPUTS=()
for f in "${POSITIONAL[@]}"; do
  [[ "$f" != /* ]] && f="$(pwd)/$f"
  INPUTS+=("$f")
done
INPUT="${INPUTS[0]}"

# Output path
if [[ -n "$OUTPUT_FLAG" && "$OUTPUT_FLAG" != "next" ]]; then
  OUTPUT="$OUTPUT_FLAG"
elif [[ "$MULTI_FILE" == true ]]; then
  OUTPUT="${INPUTS[0]%.md}-collection.pdf"
else
  OUTPUT="${INPUT%.md}.pdf"
fi
[[ "$OUTPUT" != /* ]] && OUTPUT="$(pwd)/$OUTPUT"

# --- Dependency check ---
check_dep() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }; }
check_dep pandoc
check_dep weasyprint
check_dep python3

# --- Validate ---
for f in "${INPUTS[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: file not found: $f" >&2
    exit 1
  fi
done

# --- Temp dir per invocation (collision-safe for concurrent runs) ---
tmp_dir="$(mktemp -d /tmp/eink-XXXXXX)"
tmp_md="$tmp_dir/input.md"
tmp_html="$tmp_dir/output.html"
tmp_css="$tmp_dir/style.css"
tmp_postprocess="$tmp_dir/postprocess.py"
cleanup() { rm -rf "$tmp_dir"; }
trap cleanup EXIT
trap 'echo "ERROR: interrupted" >&2; exit 130' INT TERM

# --- Multi-file: concatenate inputs with page break separator ---
if [[ "$MULTI_FILE" == true ]]; then
  tmp_combined="$tmp_dir/combined.md"
  for f in "${INPUTS[@]}"; do
    cat "$f" >> "$tmp_combined"
    printf '\n\n---\n\n' >> "$tmp_combined"
  done
  INPUT="$tmp_combined"
fi

# --- Auto-TOC: enable for multi-file, disable for single ---
if [[ "$TOC_FLAG" == "auto" ]]; then
  if [[ "$MULTI_FILE" == true ]]; then
    TOC_FLAG="--toc --toc-depth=2"
  else
    TOC_FLAG=""
  fi
fi

# --- Preprocessor: shared lib + PDF-specific checkbox markers ---
SCRIPT_LIB="$SCRIPT_DIR/lib"
tmp_pdf_post="$tmp_dir/pdf_post.py"
cat > "$tmp_pdf_post" << 'PYEOF'
"""PDF-specific transforms: convert checkboxes to markers for HTML postprocessor."""
import sys
text = sys.stdin.read()
text = text.replace('- [x]', '- {done}')
text = text.replace('- [/]', '- {partial}')
text = text.replace('- [ ]', '- {todo}')
print(text)
PYEOF

# --- Write CSS ---
cat > "$tmp_css" << 'CSSEOF'
@page {
  size: A4;
  margin: 14mm 14mm 16mm 14mm;
  @bottom-right { content: counter(page); font-size: 9pt; color: #6b7280; }
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: "Helvetica", "ChillHuoFangSong", "LXGW WenKai GB Screen", sans-serif;
  font-size: 10.5pt;
  line-height: 1.45;
  color: #3e3e3e;
  background: #ffffff;
}

h1, h2, h3, h4, h5, h6 {
  color: #111827;
  margin-top: 0.8em;
  margin-bottom: 0.2em;
  page-break-after: avoid;
}
h1 { font-size: 1.3em; font-weight: 800; border-bottom: 2px solid #111827; padding-bottom: 0.1em; }
h2 { font-size: 1.15em; font-weight: 700; border-bottom: 1px solid #d1d5db; padding-bottom: 0.1em; }
h3 { font-size: 1.05em; font-weight: 700; border-bottom: 1px solid #d1d5db; padding-bottom: 0.1em; }
h4 { font-size: 1em; font-weight: 600; border-left: 2px solid #d1d5db; padding-left: 0.5em; }
h5 { font-size: 1em; font-weight: 600; font-style: italic; }
h6 { font-size: 0.9em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; }

p { margin: 0.2em 0; }
a { color: #111827; font-weight: 700; text-decoration: underline; text-underline-offset: 2px; }
strong { color: #111827; font-weight: 700; }
em { font-style: italic; }

ul { padding-left: 1.2em; margin: 0.15em 0; list-style: none; }
ol { padding-left: 1.5em; margin: 0.15em 0; }
li { margin: 0.15em 0; }
li > ul, li > ol { margin: 0; }

code {
  font-family: "Fira Code", "JetBrainsMono Nerd Font Mono", monospace;
  font-size: 0.85em;
  font-weight: 500;
  background: #f3f4f6;
  padding: 0.1em 0.2em;
}
pre {
  background: #f3f4f6;
  border-left: 3px solid #d1d5db;
  padding: 0.6em 0.8em;
  margin: 0.4em 0;
  page-break-inside: avoid;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
}
pre code {
  background: none;
  padding: 0;
  font-size: 0.8em;
  white-space: pre-wrap;
  word-wrap: break-word;
}

blockquote {
  border-left: 2px dashed #6b7280;
  padding-left: 0.8em;
  margin: 0.4em 0;
  font-style: italic;
  color: #6b7280;
}
blockquote strong { color: #111827; font-style: normal; }
blockquote p { margin: 0.1em 0; }

table {
  border: 2px solid #d1d5db;
  border-collapse: collapse;
  width: 100%;
  margin: 0.4em 0;
  page-break-inside: avoid;
  font-size: 0.95em;
}
th { border-bottom: 2px solid #d1d5db; padding: 0.2em 0.4em; background: #f7f7f7; font-weight: 700; text-align: left; }
td { border-bottom: 1px solid #9ca3af; padding: 0.2em 0.4em; }

hr { border: 0; border-top: 1px solid #d1d5db; margin: 0.6em 0; }
img { max-width: 100%; border: 1px solid #d1d5db; }
img.math { border: none; vertical-align: middle; }
img.math.display { display: block; margin: 0.4em auto; }

/* Table of Contents — academic plain style (--toc flag)
   Each row: [title]............[Pillar]  [page]
*/
nav#TOC {
  page-break-after: always;
  margin-bottom: 2em;
}
nav#TOC::before {
  content: "Contents";
  display: block;
  font-size: 1.05em;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #111827;
  border-bottom: 1px solid #111827;
  padding-bottom: 0.3em;
  margin-bottom: 0.7em;
}
nav#TOC ul { list-style: none; padding-left: 0; margin: 0; }
nav#TOC ul ul { padding-left: 1.4em; }
nav#TOC li { margin: 0.35em 0; }
/* Full row: flex, title left, pillar+page right */
nav#TOC a {
  display: flex;
  align-items: baseline;
  text-decoration: none;
  color: #111827;
  font-weight: 400;
  font-size: 0.95em;
}
/* Pillar spans: push to right via margin-left: auto, italic */
nav#TOC a .pillar-wealth,
nav#TOC a .pillar-rel,
nav#TOC a .pillar-health {
  margin-left: auto;
  padding-left: 1em;
  font-style: italic;
  font-size: 0.85em;
  color: #6b7280;
  white-space: nowrap;
  flex-shrink: 0;
}
/* Page number: rightmost, tabular numerals */
nav#TOC a::after {
  content: target-counter(attr(href url), page);
  padding-left: 0.6em;
  font-variant-numeric: tabular-nums;
  font-size: 0.9em;
  color: #374151;
  white-space: nowrap;
  min-width: 2em;
  text-align: right;
  flex-shrink: 0;
}
/* Body headings — colored pillar badges (in the actual content pages) */
h2 .pillar-wealth, h3 .pillar-wealth { font-size: 0.65em; font-weight: 700; color: #166534; background: #dcfce7; padding: 0.1em 0.35em; border-radius: 2px; margin-left: 0.4em; vertical-align: middle; font-style: normal; }
h2 .pillar-rel,    h3 .pillar-rel    { font-size: 0.65em; font-weight: 700; color: #1e40af; background: #dbeafe; padding: 0.1em 0.35em; border-radius: 2px; margin-left: 0.4em; vertical-align: middle; font-style: normal; }
h2 .pillar-health, h3 .pillar-health { font-size: 0.65em; font-weight: 700; color: #9a3412; background: #ffedd5; padding: 0.1em 0.35em; border-radius: 2px; margin-left: 0.4em; vertical-align: middle; font-style: normal; }

h1, h2, h3 { page-break-after: avoid; }
pre, table, blockquote { page-break-inside: avoid; }
CSSEOF

# --- Write postprocessor ---
cat > "$tmp_postprocess" << 'PYEOF'
import re, sys

path = sys.argv[1]
html = open(path).read()


# Checkbox symbols — fixed-width so all align regardless of glyph size
CB = 'display: inline-block; width: 1em; text-align: center; font-weight: 700;'

# {done} -> green checkmark
html = re.sub(
    r'\{done\}',
    f'<span style="{CB} color: #166534;">\u2713</span>',
    html
)

# {todo} -> red open circle
html = re.sub(
    r'\{todo\}',
    f'<span style="{CB} color: #991b1b;">\u25cb</span>',
    html
)

# {partial} -> amber half circle
html = re.sub(
    r'\{partial\}',
    f'<span style="{CB} color: #92400e;">\u25d0</span>',
    html
)

# Add dash prefix to regular list items (no checkbox marker)
DASH = '\u2013'
html = re.sub(
    r'<li>(?!<p>)(?!\s*<span)(?!\s*<ul)(?!\s*<ol)(\s*[^<])',
    f'<li>{DASH} \\1',
    html
)
html = re.sub(
    r'<li><p>(?!\s*<span)(?!\s*<ul)(?!\s*<ol)(\s*[^<])',
    f'<li><p>{DASH} \\1',
    html
)

# Remove empty list items (including those with nested empty tags or just whitespace/dash)
for _ in range(5):
    html = re.sub(r'<li>\s*</li>', '', html)
    html = re.sub(r'<li>\s*<p>\s*</p>\s*</li>', '', html)
    html = re.sub(r'<li>\s*<ul>\s*</ul>\s*</li>', '', html)
    html = re.sub(r'<li>\s*<ol>\s*</ol>\s*</li>', '', html)
    html = re.sub(r'<li>\s*\u2013?\s*</li>', '', html)
    html = re.sub(r'<li><p>\s*\u2013?\s*</p>\s*</li>', '', html)
    html = re.sub(r'<ul>\s*</ul>', '', html)
    html = re.sub(r'<ol>\s*</ol>', '', html)

# Pillar badge tags: [Wealth] [Relationship] [Health] anywhere in headings or TOC links
PILLARS = [
    ('[Wealth]',       'pillar-wealth',   'Wealth'),
    ('[Relationship]', 'pillar-rel',      'Relationship'),
    ('[Health]',       'pillar-health',   'Health'),
]
for tag, cls, label in PILLARS:
    badge = f'<span class="{cls}">{label}</span>'
    html = html.replace(tag, badge)

open(path, 'w').write(html)
PYEOF

# --- Build pipeline ---

# 1. Preprocess: shared Obsidian stripping → PDF-specific checkbox markers
VAULT_DIR="${OBSIDIAN_VAULT:-$HOME/vault}" \
  python3 "$SCRIPT_LIB/obsidian-to-standard-md.py" --resolve-embeds --mermaid \
  < "$INPUT" | python3 "$tmp_pdf_post" > "$tmp_md"

# 2. Markdown -> HTML
# shellcheck disable=SC2086
pandoc "$tmp_md" \
  -f markdown+autolink_bare_uris+hard_line_breaks \
  -t html5 \
  --standalone \
  --css "$tmp_css" \
  --embed-resources \
  --katex \
  --metadata title="" \
  --metadata author="${PDF_AUTHOR:-}" \
  $TOC_FLAG \
  -o "$tmp_html" 2>/dev/null

# 3. Colorize task states in HTML
python3 "$tmp_postprocess" "$tmp_html"

# 4. HTML -> PDF
weasyprint "$tmp_html" "$OUTPUT" 2>/dev/null

# 5. Set PDF metadata (title + author + timestamp for Kindle bookshelf)
python3 - "$OUTPUT" "${INPUTS[0]}" "$MULTI_FILE" << 'METAEOF'
import sys, re, os
from datetime import datetime

pdf_path = sys.argv[1]
input_path = sys.argv[2]
multi_file = sys.argv[3] == 'true'

# Extract title from first H1 in source markdown
with open(input_path) as f:
    content = f.read()
title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
if multi_file:
    title = f"Collection ({os.path.basename(input_path)})"
else:
    title = title_match.group(1).strip() if title_match else os.path.basename(input_path)

timestamp = os.environ.get('PDF_DATE', datetime.now().strftime('%Y-%m-%d %H:%M'))
author = os.environ.get('PDF_AUTHOR', '')
full_title = f"{title} ({timestamp})"

try:
    import fitz  # pymupdf
    doc = fitz.open(pdf_path)
    doc.set_metadata({
        'title': full_title,
        'author': author,
        'subject': '',
        'keywords': '',
        'creator': 'obsidian-to-pdf',
        'producer': 'weasyprint + pymupdf',
    })
    doc.save(pdf_path, incremental=True, encryption=0)
    doc.close()
except ImportError:
    pass  # pymupdf not available, metadata won't be set
METAEOF

# Report
size=$(stat -f%z "$OUTPUT" 2>/dev/null || stat -c%s "$OUTPUT" 2>/dev/null)
echo "$OUTPUT ($(( size / 1024 ))KB)"
