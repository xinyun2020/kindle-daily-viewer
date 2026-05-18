#!/usr/bin/env python3
"""Convert Obsidian-flavored markdown to standard markdown.

Strips Obsidian-specific syntax (wiki links, callouts, dataview, embeds,
frontmatter, inline fields, comments) and outputs clean markdown that
pandoc or any standard renderer can handle.

Usage:
    cat input.md | python3 obsidian-to-standard-md.py [--resolve-embeds] [--github-links]
    python3 obsidian-to-standard-md.py --help

Environment:
    VAULT_DIR       Path to Obsidian vault root (for embed resolution and link paths)
    GITHUB_REPO     GitHub blob URL base (for wiki link hyperlinks)

Options:
    --resolve-embeds  Resolve ![[embed]] syntax by inlining file content (requires VAULT_DIR)
    --github-links    Convert [[wiki links]] to GitHub hyperlinks (requires GITHUB_REPO)
    --highlights      Convert ==text== to <mark> tags (for HTML-based output)
    --mermaid         Render ```mermaid blocks to SVG images (requires mmdc)

Callers:
    obsidian-to-epub.sh — EPUB conversion (uses all options)
    obsidian-to-pdf.sh  — PDF conversion (uses --resolve-embeds)
    ⚠️  Changes here affect both. Test both after modifying.
"""

import re
import sys
import os
import glob as globmod
import subprocess
import tempfile


def build_file_index(vault_dir):
    """Build filename -> full path index for the vault."""
    index = {}
    if vault_dir:
        for path in globmod.glob(os.path.join(vault_dir, '**', '*.md'), recursive=True):
            fname = os.path.basename(path)[:-3]
            index[fname] = path
    return index


def read_file_content(filepath):
    """Read file, strip its frontmatter, return content."""
    try:
        with open(filepath) as f:
            content = f.read()
        content = re.sub(r'^---\n.*?\n---\n', '', content, count=1, flags=re.DOTALL)
        return content.strip()
    except (IOError, OSError):
        return ''


def extract_section(content, section_name):
    """Extract a specific section (## Section) from content."""
    pattern = rf'^(#{1,6})\s+{re.escape(section_name)}\s*\n(.*?)(?=^\1\s|\Z)'
    m = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return m.group(2).strip() if m else ''


def resolve_embed(match, file_index, vault_dir):
    """Resolve ![[note]] or ![[note#section]] — inline content or image."""
    content = match.group(1)
    section = None
    if '#' in content:
        note_name, section = content.split('#', 1)
    else:
        note_name = content

    # Image embeds — resolve path, output markdown image syntax
    img_exts = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp')
    if note_name.lower().endswith(img_exts):
        # Block absolute paths to prevent vault escape
        if os.path.isabs(note_name):
            return ''
        if vault_dir:
            matches = globmod.glob(os.path.join(vault_dir, '**', note_name), recursive=True)
            # Ensure resolved path stays within vault
            vault_real = os.path.realpath(vault_dir)
            matches = [m for m in matches if os.path.realpath(m).startswith(vault_real + os.sep)]
            if matches:
                return f'\n\n![{note_name}]({matches[0]})\n\n'
        return ''

    # Markdown note embeds
    if note_name in file_index:
        file_content = read_file_content(file_index[note_name])
        if section and file_content:
            file_content = extract_section(file_content, section)
        if file_content:
            return f'\n\n{file_content}\n\n'
    return ''


def convert_wikilink(match, file_index, vault_dir, github_repo):
    """Convert [[wiki link]] to GitHub hyperlink or bold text."""
    content = match.group(1)
    if '|' in content:
        name = content.split('|')[-1]
        note = content.split('|')[0].split('#')[0]
    else:
        name = content.split('#')[0]
        note = name

    if not github_repo:
        return f'**{name}**'

    # Direct link if file found, search fallback otherwise
    if note in file_index:
        rel = file_index[note][len(vault_dir)+1:]
        url = f"{github_repo}/{rel.replace(' ', '%20')}"
    else:
        search_base = github_repo.replace('/blob/main', '/search')
        url = f"{search_base}?q={note.replace(' ', '+')}"
    return f'[**{name}**]({url})'


def render_mermaid(text, output_dir=''):
    """Render ```mermaid code blocks to SVG images.

    Requires mmdc (mermaid CLI): npm install -g @mermaid-js/mermaid-cli
    Falls back to leaving as code block if mmdc is unavailable.
    """
    import shutil
    if not shutil.which('mmdc'):
        return text

    if not output_dir:
        output_dir = tempfile.mkdtemp(prefix='mermaid-')

    counter = [0]

    def replace_mermaid(match):
        diagram = match.group(1).strip()
        counter[0] += 1
        mmd_path = os.path.join(output_dir, f'diagram_{counter[0]}.mmd')
        png_path = os.path.join(output_dir, f'diagram_{counter[0]}.png')

        try:
            with open(mmd_path, 'w') as f:
                f.write(diagram)
            # PNG output with 2x scale for crisp text on e-ink/retina
            # SVG uses foreignObject which WeasyPrint/Kindle can't render text from
            result = subprocess.run(
                ['mmdc', '-i', mmd_path, '-o', png_path,
                 '-b', 'white', '-w', '1200', '--scale', '3'],
                capture_output=True, timeout=15
            )
            if result.returncode == 0 and os.path.exists(png_path):
                return f'\n\n![diagram]({png_path})\n\n'
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"WARNING: mermaid render failed: {e}", file=sys.stderr)

        # Fallback: keep as code block
        return match.group(0)

    return re.sub(r'```mermaid\n(.*?)```', replace_mermaid, text, flags=re.DOTALL)


def strip_obsidian(text, resolve_embeds=False, github_links=False,
                   highlights=False, mermaid=False, vault_dir='', github_repo=''):
    """Main conversion: Obsidian markdown → standard markdown.

    Args:
        text: Input Obsidian-flavored markdown
        resolve_embeds: If True, resolve ![[embeds]] by inlining content
        github_links: If True, convert [[wiki links]] to GitHub URLs
        highlights: If True, convert ==text== to <mark> tags
        mermaid: If True, render ```mermaid blocks to SVG images
        vault_dir: Path to vault root (needed for embeds and direct links)
        github_repo: GitHub blob URL base (needed for wiki links)

    Returns:
        Clean standard markdown string
    """
    file_index = build_file_index(vault_dir) if (resolve_embeds or github_links) else {}

    # Render mermaid diagrams to SVG before any other processing
    if mermaid:
        text = render_mermaid(text)

    # Resolve embeds FIRST (before stripping other syntax from inlined content)
    if resolve_embeds:
        for _ in range(3):  # max 3 levels deep
            new_text = re.sub(
                r'!\[\[([^\]]+)\]\]',
                lambda m: resolve_embed(m, file_index, vault_dir),
                text
            )
            if new_text == text:
                break
            text = new_text

    # Strip YAML frontmatter
    text = re.sub(r'^---\n.*?\n---\n', '', text, count=1, flags=re.DOTALL)

    # Strip dataview/dataviewjs blocks
    text = re.sub(r'`{3}dataview\w*\n.*?`{3}', '', text, flags=re.DOTALL)

    # Strip Obsidian templater/inline JS: `$= ...`
    text = re.sub(r'`\$=.*?`', '', text)

    # Strip Obsidian comments %%...%%
    text = re.sub(r'%%.*?%%', '', text, flags=re.DOTALL)

    # Convert ==highlights== to <mark> if requested
    if highlights:
        text = re.sub(r'==(.+?)==', r'<mark>\1</mark>', text)

    # Strip remaining embed syntax (unresolved or if resolve_embeds=False)
    text = re.sub(r'^!\[\[[^\]]+\]\]\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'!\[\[([^\]]+)\]\]', '', text)

    # Strip Obsidian callout syntax > [!type]+/- -> blockquote header
    text = re.sub(r'> \[!\s*(\w+)\s*\][+-]?\s*(.*)', r'> **\1** \2', text)

    # Strip inline fields: "key:: value" -> "value", "key::" (empty) -> remove line
    text = re.sub(r'^\s*-\s+\w+::\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'(\w+)::\s*', '', text)

    # Strip lines that are just "> >" or empty blockquote nesting
    text = re.sub(r'^>\s*>\s*$', '', text, flags=re.MULTILINE)

    # Strip remaining empty blockquote lines
    text = re.sub(r'^>\s*$', '', text, flags=re.MULTILINE)

    # Strip "Daily Review" section
    text = re.sub(r'^###?\s+Daily Review\s*\n(?:(?!^##\s).*\n?)*', '', text, flags=re.MULTILINE)

    # Strip "Reading + Sleep" line
    text = re.sub(r'^.*Reading \+ Sleep.*$', '', text, flags=re.MULTILINE)

    # Convert wiki links
    if github_links:
        text = re.sub(
            r'\[\[([^\]]+)\]\]',
            lambda m: convert_wikilink(m, file_index, vault_dir, github_repo),
            text
        )
    else:
        # Plain text conversion (no hyperlinks)
        def plain_wikilink(m):
            content = m.group(1)
            if '|' in content:
                return content.split('|')[-1]
            return content.split('#')[0]
        text = re.sub(r'\[\[([^\]]+)\]\]', plain_wikilink, text)

    # Fix blockquote eating next list items
    lines = text.split('\n')
    result = []
    for i, line in enumerate(lines):
        result.append(line)
        if line.startswith('>') and i + 1 < len(lines) and re.match(r'^- ', lines[i + 1]):
            result.append('')
    text = '\n'.join(result)

    # Remove empty list items
    text = re.sub(r'^\s*-\s*$', '', text, flags=re.MULTILINE)

    # Clean up excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +$', '', text, flags=re.MULTILINE)

    return text.strip()


if __name__ == '__main__':
    args = sys.argv[1:]
    opts = {
        'resolve_embeds': '--resolve-embeds' in args,
        'github_links': '--github-links' in args,
        'highlights': '--highlights' in args,
        'mermaid': '--mermaid' in args,
        'vault_dir': os.environ.get('VAULT_DIR', ''),
        'github_repo': os.environ.get('GITHUB_REPO', ''),
    }

    if '--help' in args or '-h' in args:
        print(__doc__)
        sys.exit(0)

    text = sys.stdin.read()
    print(strip_obsidian(text, **opts))
