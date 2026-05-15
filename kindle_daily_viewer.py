#!/usr/bin/env python3
"""
Kindle Daily Viewer — serves Obsidian daily notes as plain HTML for e-ink browsers.

Designed for Kindle, Kobo, and other e-ink devices that can't run JavaScript.
Pure Python 3 stdlib — no external dependencies.

Features:
- Markdown to HTML (no JS, no external deps)
- Day navigation (M T W T F S S), period buttons (W/M/Q/Y)
- Git diff view (status + diff across all changed files)
- Active Obsidian file button (reads .obsidian/workspace.json)
- Checkbox toggle (tap to flip [ ]/[x] in the actual markdown file)
- Wiki link navigation (tap opens in viewer + activates in Obsidian)
- Page-flip anchor links (pure HTML, no JS)
- Password auth with SHA256 cookie (LAN-safe)
- Strips dataview, dataviewjs, custom-frames blocks

Config: ~/.config/kdv/config.env (KDV_* environment variables)
Usage: kdv [--port 8080]
Deps: Python 3.8+ stdlib only
"""
import html, argparse, time, os, re, urllib.parse, hashlib, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

# =============================================================================
# CONFIG
# =============================================================================
# Load config from file first, then env vars override, then defaults.
# Priority: env var > config file > default

CONFIG_DIR = Path(os.environ.get("KDV_CONFIG_DIR", Path.home() / ".config" / "kdv"))
CONFIG_FILE = CONFIG_DIR / "config.env"


def _load_config_file():
    """Load KDV_* variables from config.env (KEY=VALUE format, # comments)."""
    config = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key.startswith("KDV_"):
                    config[key] = value
    return config


_file_config = _load_config_file()


def _cfg(key, default=None):
    """Get config value: env var > config file > default."""
    return os.environ.get(key) or _file_config.get(key) or default


# Required config — KDV_VAULT must be set (no hardcoded default)
VAULT = _cfg("KDV_VAULT")
if not VAULT:
    print("Error: KDV_VAULT is not set.")
    print(f"Create {CONFIG_FILE} with:\n  KDV_VAULT=/path/to/your/obsidian/vault")
    print("Or set the KDV_VAULT environment variable.")
    raise SystemExit(1)

DAILY_DIR = _cfg("KDV_DAILY_DIR", os.path.join(VAULT, "log"))
PORT = int(_cfg("KDV_PORT", "8080"))
DIFF_TRUNCATE_BYTES = int(_cfg("KDV_DIFF_TRUNCATE", "50000"))
ACTIVE_FILE_MAX_CHARS = 20
FEED_DIR = _cfg("KDV_FEED_DIR", os.path.join(VAULT, "feed"))
WORKTREE_DIR = _cfg("KDV_WORKTREE_DIR", "")
EXTRA_REPOS = [p.strip() for p in _cfg("KDV_EXTRA_REPOS", "").split(",") if p.strip()]
GITHUB_DESKTOP = _cfg("KDV_GITHUB_DESKTOP", "").lower() in ("true", "1", "yes")

# Auth — password login via HTML form + SHA256 cookie.
# Cookie is not reversible from network sniffing. Fine for LAN.
AUTH_PASS = _cfg("KDV_PASSWORD", "password")
AUTH_COOKIE = hashlib.sha256(AUTH_PASS.encode()).hexdigest()[:16]

# =============================================================================
# HTML TEMPLATES
# =============================================================================

LOGIN_PAGE = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body { margin: 40px; background: #fff; color: #000; font-family: Georgia, serif; font-size: 16px; }
input { font-size: 16px; padding: 8px; margin: 4px 0; }
button { font-size: 16px; padding: 8px 16px; background: #cfc; border: 1px solid #090; }
</style>
</head><body>
<h2>Daily Notes</h2>
<form method="POST" action="/login">
<input type="password" name="pass" placeholder="Password"><br>
<button type="submit">Enter</button>
</form>
</body></html>
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; max-width: 100vw; }
body { margin: 0; padding: 0; background: #fff; color: #000; font-family: Georgia, serif; font-size: 15px; line-height: 1.5; overflow-x: hidden; word-wrap: break-word; overflow-wrap: break-word; }
.nav { background: #eee; padding: 4px 8px; font-family: monospace; font-size: 0.75rem; position: sticky; top: 0; left: 0; right: 0; z-index: 99; height: 28px; }
.nav a { margin-right: 4px; text-decoration: none; color: #06c; padding: 2px 4px; }
.nav a.refresh { background: #cfc; font-weight: bold; border: 1px solid #090; }
h1 { font-size: 20px; margin: 8px 0; word-wrap: break-word; overflow-wrap: break-word; }
h2 { font-size: 17px; margin: 8px 0 4px; border-bottom: 1px solid #ccc; word-wrap: break-word; overflow-wrap: break-word; }
h3 { font-size: 15px; margin: 6px 0 4px; word-wrap: break-word; overflow-wrap: break-word; }
h4 { font-size: 14px; margin: 0; word-wrap: break-word; overflow-wrap: break-word; position: sticky; top: 28px; background: #f5f5f5; z-index: 8; padding: 2px 4px; border-left: 3px solid #06c; }
.diff-section { position: relative; }
.page-btns { position: fixed; bottom: 10px; right: 10px; z-index: 100; display: flex; flex-direction: column; gap: 6px; }
.pg-btn { display: block; width: 40px; height: 40px; line-height: 40px; text-align: center; font-size: 20px; background: transparent; border: 1px solid #999; border-radius: 4px; text-decoration: none; color: #333; }
pre { background: #f5f5f5; padding: 6px; font-size: 12px; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word; overflow-x: hidden; max-width: 100%; }
.diff-add { background: #d4edda; color: #155724; display: block; margin: 0 -6px; padding: 0 6px; word-wrap: break-word; overflow-wrap: break-word; }
.diff-del { background: #f8d7da; color: #721c24; display: block; margin: 0 -6px; padding: 0 6px; word-wrap: break-word; overflow-wrap: break-word; }
.diff-hunk { color: #6a737d; font-style: italic; display: block; background: #f1f8ff; margin: 0 -6px; padding: 0 6px; word-wrap: break-word; overflow-wrap: break-word; }
code { background: #f0f0f0; padding: 1px 4px; font-size: 13px; word-wrap: break-word; overflow-wrap: break-word; }
ul, ol { padding-left: 20px; }
li { margin: 2px 0; }
.checkbox { font-family: monospace; }
.toggle { text-decoration: none; color: #06c; padding: 4px; }
.done { color: #666; text-decoration: line-through; }
blockquote { border-left: 3px solid #ccc; margin: 8px 0; padding: 4px 12px; color: #555; }
a { color: #06c; }
.wikilink { color: #8b5cf6; font-style: italic; }
</style>
</head><body>
<div class="nav">
<a href="/?t={timestamp}" class="refresh">{time_str}</a>
{day_buttons}
{period_buttons}
{diff_button}
{active_button}
</div>
{content}
<div class="page-btns">
<a href="#pg{prev_page}" class="pg-btn">&uarr;</a>
<a href="#pg{next_page}" class="pg-btn">&darr;</a>
</div>
</body></html>
"""


# =============================================================================
# DATA SOURCES
# =============================================================================

def get_obsidian_active_file():
    """Get the currently active file in Obsidian from workspace.json."""
    import json
    workspace_path = os.path.join(VAULT, ".obsidian/workspace.json")
    try:
        with open(workspace_path, "r") as f:
            data = json.load(f)
        active_id = data.get("active", "")

        def find_leaves(node):
            if isinstance(node, dict):
                if node.get("type") == "leaf":
                    state = node.get("state", {}).get("state", {})
                    yield (node.get("id", ""), state.get("file", ""))
                for v in node.values():
                    yield from find_leaves(v)
            elif isinstance(node, list):
                for item in node:
                    yield from find_leaves(item)

        for leaf_id, filepath in find_leaves(data):
            if leaf_id == active_id and filepath:
                return filepath
    except Exception:
        pass
    return None


def _slug(text):
    """Generate anchor slug matching markdown_to_html's heading id logic."""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _parse_file_diffs(diff_text):
    """Parse diff text into list of (filename, chunk) tuples."""
    if not diff_text:
        return []
    if len(diff_text) > DIFF_TRUNCATE_BYTES:
        diff_text = diff_text[:DIFF_TRUNCATE_BYTES] + "\n... (truncated)"
    file_diffs = re.split(r'(?=^diff --git )', diff_text, flags=re.MULTILINE)
    results = []
    for chunk in file_diffs:
        if not chunk.strip():
            continue
        m = re.match(r'diff --git a/(.*?) b/(.*)', chunk.split('\n')[0])
        fname = m.group(2) if m else None
        results.append((fname, chunk))
    return results


def get_git_diff(repo_path=None):
    """Get git status and diff. Uses VAULT by default, or specified repo path."""
    repo = repo_path or VAULT
    repo_name = os.path.basename(repo)
    prefix = _slug(repo_name)
    try:
        status = subprocess.run(
            ["git", "-C", repo, "status", "--short"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()

        full_diff = subprocess.run(
            ["git", "-C", repo, "diff", "--no-ext-diff", "HEAD"],
            capture_output=True, text=True, timeout=10
        ).stdout

        unpushed_commits = []
        try:
            # Try upstream comparison first
            unpushed_log = subprocess.run(
                ["git", "-C", repo, "log", "--format=%H %s", "@{u}..HEAD"],
                capture_output=True, text=True, timeout=10
            )
            if unpushed_log.returncode != 0:
                # No upstream — show all commits on current branch as unpushed
                unpushed_log = subprocess.run(
                    ["git", "-C", repo, "log", "--format=%H %s", "--max-count=20"],
                    capture_output=True, text=True, timeout=10
                )
            log_text = unpushed_log.stdout.strip()
            if log_text:
                for line in log_text.split("\n"):
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        unpushed_commits.append((parts[0], parts[1]))
        except Exception:
            pass

        output = []

        unstaged_files = _parse_file_diffs(full_diff)
        if status:
            output.append("### Changed Files\n")
            for fname, _ in unstaged_files:
                if fname:
                    anchor = f"{prefix}-unstaged-{_slug(fname)}"
                    output.append(f"- [{fname}](#{anchor})")
            diff_fnames = {f for f, _ in unstaged_files if f}
            for line in status.split("\n"):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    fname = parts[1].strip('"')
                    if fname not in diff_fnames:
                        output.append(f"- [{parts[0]}] {fname}")
            output.append("")

        if unpushed_commits:
            output.append(f"### Unpushed Commits ({len(unpushed_commits)})\n")
            for sha, msg in unpushed_commits:
                short = sha[:7]
                anchor = f"{prefix}-{short}"
                output.append(f"- [{short}](#{anchor}) {msg}")
            output.append("")

        if unstaged_files:
            output.append("### Unstaged Changes\n")
            for fname, chunk in unstaged_files:
                if fname:
                    heading = f"{prefix}-unstaged-{_slug(fname)}"
                    output.append(f'<div class="diff-section"><h4 id="{heading}">{html.escape(fname)}</h4>')
                    output.append(f"```\n{chunk}\n```\n")
                    output.append("</div>")
                else:
                    output.append(f"```\n{chunk}\n```\n")

        if unpushed_commits:
            for sha, msg in unpushed_commits:
                short = sha[:7]
                commit_anchor = f"{prefix}-{short}"
                output.append(f'<h3 id="{commit_anchor}">{short} {html.escape(msg)}</h3>\n')
                # Try parent diff; fall back to root diff for first commit
                result = subprocess.run(
                    ["git", "-C", repo, "diff", "--no-ext-diff", f"{sha}~1..{sha}"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0 or not result.stdout:
                    result = subprocess.run(
                        ["git", "-C", repo, "diff-tree", "-p", "--root", sha],
                        capture_output=True, text=True, timeout=10
                    )
                commit_diff = result.stdout
                commit_files = _parse_file_diffs(commit_diff)
                if commit_files:
                    for fname, _ in commit_files:
                        if fname:
                            file_anchor = f"{prefix}-{short}-{_slug(fname)}"
                            output.append(f"- [{fname}](#{file_anchor})")
                    output.append("")
                    for fname, chunk in commit_files:
                        if fname:
                            file_anchor = f"{prefix}-{short}-{_slug(fname)}"
                            output.append(f'<div class="diff-section"><h4 id="{file_anchor}">{fname}</h4>')
                            output.append(f"```\n{chunk}\n```\n")
                            output.append("</div>")
                        else:
                            output.append(f"```\n{chunk}\n```\n")
                else:
                    output.append("*No diff (merge commit or empty)*\n")

        if not output:
            return "No changes, fully pushed."

        return "\n".join(output)
    except Exception as e:
        return f"Error: {e}"


def get_ticket_id_from_active():
    """Extract ticket ID from active Obsidian file."""
    active = get_obsidian_active_file()
    if not active:
        return None
    basename = os.path.basename(active).replace(".md", "")
    ticket_id = basename.split(",")[0].strip()
    if re.match(r'^[A-Za-z]+-\d+$', ticket_id):
        return ticket_id
    return None


def find_worktrees_for_ticket(ticket_id):
    """Find all worktree dirs that contain the ticket ID (case-insensitive)."""
    if not WORKTREE_DIR or not os.path.isdir(WORKTREE_DIR):
        return []
    ticket_lower = ticket_id.lower()
    matches = []
    for name in sorted(os.listdir(WORKTREE_DIR)):
        if ticket_lower in name.lower():
            full_path = os.path.join(WORKTREE_DIR, name)
            if os.path.isdir(full_path):
                matches.append((name, full_path))
    return matches


def _get_github_desktop_active_repo():
    """Detect the currently selected repo in GitHub Desktop (best-effort).

    Reads GitHub Desktop's LevelDB state on macOS:
    1. Local Storage: last-selected-repository-id (integer)
    2. IndexedDB: repo records with path + ID encoded as IEEE 754 double
    Returns the repo path or None.
    """
    import glob as globmod
    import struct

    gd_dir = os.path.join(Path.home(), "Library", "Application Support", "GitHub Desktop")

    # Step 1: Get last-selected-repository-id from Local Storage
    ls_dir = os.path.join(gd_dir, "Local Storage", "leveldb")
    if not os.path.isdir(ls_dir):
        return None
    selected_id = None
    for f in sorted(globmod.glob(os.path.join(ls_dir, "*.log")) +
                     globmod.glob(os.path.join(ls_dir, "*.ldb"))):
        try:
            data = open(f, "rb").read()
            for m in re.finditer(rb'last-selected-repository-id\x04\x01(\d+)', data):
                selected_id = int(m.group(1))
        except Exception:
            continue
    if selected_id is None:
        return None

    # Step 2: Scan IndexedDB for repo paths with their auto-increment IDs
    # Chrome encodes IDBKey integers as: 0x03 type byte + 8-byte little-endian IEEE 754 double
    # This appears in the bytes before the "path" field in each repo record
    idb_dir = os.path.join(gd_dir, "IndexedDB", "file__0.indexeddb.leveldb")
    if not os.path.isdir(idb_dir):
        return None

    target_bytes = struct.pack("<d", float(selected_id))
    for f in sorted(globmod.glob(os.path.join(idb_dir, "*.ldb")) +
                     globmod.glob(os.path.join(idb_dir, "*.log"))):
        try:
            data = open(f, "rb").read()
            # Find the target ID bytes near a path field
            idx = 0
            while True:
                idx = data.find(target_bytes, idx)
                if idx == -1:
                    break
                # Look ahead for a file path within the record
                ahead = data[idx:idx + 300]
                path_match = re.search(rb'(/Users/[\x20-\x7e]+)', ahead)
                if path_match:
                    path = path_match.group(1).decode("ascii", errors="ignore").rstrip('"')
                    if os.path.isdir(os.path.join(path, ".git")):
                        return path
                idx += 1
        except Exception:
            continue
    return None


def get_diff_view():
    """Build diff view: vault diff + worktree diffs + active GitHub Desktop repo."""
    output = []
    ticket_id = get_ticket_id_from_active()
    worktrees = find_worktrees_for_ticket(ticket_id) if ticket_id else []

    # Collect extra repos (explicit config + GitHub Desktop active repo)
    extra_repos = []
    seen_paths = {os.path.realpath(VAULT)}
    for wt_name, wt_path in worktrees:
        seen_paths.add(os.path.realpath(wt_path))

    for repo_path in EXTRA_REPOS:
        real = os.path.realpath(repo_path)
        if real not in seen_paths and os.path.isdir(os.path.join(repo_path, ".git")):
            extra_repos.append((os.path.basename(repo_path), repo_path))
            seen_paths.add(real)

    if GITHUB_DESKTOP:
        active = _get_github_desktop_active_repo()
        if active:
            real = os.path.realpath(active)
            if real not in seen_paths:
                extra_repos.append((os.path.basename(active), active))
                seen_paths.add(real)

    # Table of contents
    output.append("### Repos\n")
    output.append(f"- [Obsidian](#repo-vault)")
    for wt_name, wt_path in worktrees:
        output.append(f"- [{wt_name}](#{_slug(wt_name)})")
    for name, path in extra_repos:
        output.append(f"- [{name}](#{_slug(name)})")
    output.append("")

    if ticket_id and worktrees:
        output.append(f"*Ticket: {ticket_id} — {len(worktrees)} worktree(s)*\n")
    elif ticket_id:
        output.append(f"*Ticket: {ticket_id} — no matching worktrees*\n")

    # Vault diff
    output.append(f"## Obsidian\n")
    output.append(get_git_diff(VAULT))
    output.append("")

    # Worktree diffs
    for wt_name, wt_path in worktrees:
        output.append(f"## {wt_name}\n")
        output.append(get_git_diff(wt_path))
        output.append("")

    # Extra repo diffs
    for name, path in extra_repos:
        output.append(f"## {name}\n")
        output.append(get_git_diff(path))
        output.append("")

    return "\n".join(output)


def get_daily_file(offset=0):
    """Get path to daily note for today + offset days."""
    target_date = date.today() + timedelta(days=offset)
    return os.path.join(DAILY_DIR, f"{target_date.strftime('%Y-%m-%d')}.md"), target_date


def get_week_days():
    """Get the 7 days of the current week (Mon-Sun) with offsets from today."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    days = []
    day_names = ["M", "T", "W", "T", "F", "S", "S"]
    for i in range(7):
        d = monday + timedelta(days=i)
        offset = (d - today).days
        days.append((day_names[i], offset, d))
    return days


def get_period_files():
    """Get current week, month, quarter, year filenames."""
    today = date.today()
    week_num = today.isocalendar()[1]
    month_num = today.month
    quarter = (today.month - 1) // 3 + 1
    year = today.year
    return [
        ("W", f"{year}-W{week_num:02d}.md"),
        ("M", f"{year}-M{month_num:02d}.md"),
        ("Q", f"{year}-Q{quarter}.md"),
        ("Y", f"{year}-Y.md"),
    ]


# =============================================================================
# MARKDOWN RENDERING
# =============================================================================

def _preserve_line_count(match):
    """Replace matched block with same number of blank lines to keep line numbers stable."""
    return "\n" * match.group(0).count("\n")


def strip_obsidian_dynamic(text):
    """Remove blocks that won't render on e-ink: dataview, custom-frames, embeds.
    Preserves line count so line numbers stay aligned with the original file."""
    original_count = text.count("\n")
    text = re.sub(r'```dataview\n.*?```', _preserve_line_count, text, flags=re.DOTALL)
    text = re.sub(r'```dataviewjs\n.*?```', _preserve_line_count, text, flags=re.DOTALL)
    text = re.sub(r'```custom-frames\n.*?```', _preserve_line_count, text, flags=re.DOTALL)
    text = re.sub(r'`\$=.*?`', '', text)
    text = re.sub(r'!\[\[([^\]]+)\]\]', r'(embed: \1)', text)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() in (">", "> "):
            lines[i] = ""
    result = "\n".join(lines)
    if result.count("\n") != original_count:
        import sys
        print("warning: strip_obsidian_dynamic changed line count", file=sys.stderr)
    return result


def markdown_to_html(text, file_path=None, line_offset=0):
    """Convert markdown to HTML. No JS needed.

    Args:
        text: Markdown content (frontmatter already stripped).
        file_path: Vault-relative path for checkbox toggle links.
        line_offset: Lines stripped before this text (frontmatter) so
            action URLs reference correct line numbers in the original file.

    Returns:
        (html_string, total_pages) tuple.
    """
    text = strip_obsidian_dynamic(text)
    lines = text.split("\n")
    output = []
    in_code_block = False
    in_list = False
    list_type = None
    page_counter = 0
    lines_since_page = 0
    PAGE_INTERVAL = 20

    output.append('<a id="pg0"></a>')

    for line_idx, line in enumerate(lines, 1):
        line_num = line_idx + line_offset
        lines_since_page += 1
        if not in_code_block and lines_since_page >= PAGE_INTERVAL:
            page_counter += 1
            output.append(f'<a id="pg{page_counter}"></a>')
            lines_since_page = 0

        if line.strip().startswith("```"):
            if in_code_block:
                output.append("</pre>")
                in_code_block = False
            else:
                output.append("<pre>")
                in_code_block = True
            continue

        if in_code_block:
            escaped = html.escape(line)
            if line.startswith("+") and not line.startswith("+++"):
                output.append(f'<span class="diff-add">{escaped}</span>')
            elif line.startswith("-") and not line.startswith("---"):
                output.append(f'<span class="diff-del">{escaped}</span>')
            elif line.startswith("@@"):
                output.append(f'<span class="diff-hunk">{escaped}</span>')
            else:
                output.append(escaped)
            continue

        if in_list and not re.match(r'^(\s*[-*+]|\s*\d+\.)\s', line) and line.strip():
            output.append(f"</{list_type}>")
            in_list = False

        if line.startswith("# "):
            slug = _slug(line[2:])
            output.append(f'<h1 id="{slug}">{process_inline(line[2:])}</h1>')
        elif line.startswith("## "):
            slug = _slug(line[3:])
            output.append(f'<h2 id="{slug}">{process_inline(line[3:])}</h2>')
        elif line.startswith("### "):
            slug = _slug(line[4:])
            output.append(f'<h3 id="{slug}">{process_inline(line[4:])}</h3>')
        elif line.startswith("> "):
            output.append(f"<blockquote>{process_inline(line[2:])}</blockquote>")
        elif re.match(r'^(\s*)- \[x\]\s*(.*)', line):
            m = re.match(r'^(\s*)- \[x\]\s*(.*)', line)
            if not in_list:
                output.append("<ul>")
                in_list = True
                list_type = "ul"
            anchor_id = f"ln{line_num}"
            if file_path:
                enc_file = urllib.parse.quote(file_path)
                toggle_url = f"/?action=toggle&amp;file={enc_file}&amp;line={line_num}&amp;anchor={anchor_id}"
                checkbox_html = f'<a href="{toggle_url}" class="checkbox toggle">[x]</a>'
            else:
                checkbox_html = '<span class="checkbox">[x]</span>'
            output.append(f'<li id="{anchor_id}" class="done">{checkbox_html} {process_inline(m.group(2))}</li>')
        elif re.match(r'^(\s*)- \[ \]\s*(.*)', line):
            m = re.match(r'^(\s*)- \[ \]\s*(.*)', line)
            if not in_list:
                output.append("<ul>")
                in_list = True
                list_type = "ul"
            anchor_id = f"ln{line_num}"
            if file_path:
                enc_file = urllib.parse.quote(file_path)
                toggle_url = f"/?action=toggle&amp;file={enc_file}&amp;line={line_num}&amp;anchor={anchor_id}"
                checkbox_html = f'<a href="{toggle_url}" class="checkbox toggle">[ ]</a>'
            else:
                checkbox_html = '<span class="checkbox">[ ]</span>'
            output.append(f'<li id="{anchor_id}">{checkbox_html} {process_inline(m.group(2))}</li>')
        elif re.match(r'^(\s*)[-*+]\s+(.*)', line):
            m = re.match(r'^(\s*)[-*+]\s+(.*)', line)
            if not in_list:
                output.append("<ul>")
                in_list = True
                list_type = "ul"
            output.append(f"<li>{process_inline(m.group(2))}</li>")
        elif re.match(r'^(\s*)\d+\.\s+(.*)', line):
            m = re.match(r'^(\s*)\d+\.\s+(.*)', line)
            if not in_list:
                output.append("<ol>")
                in_list = True
                list_type = "ol"
            output.append(f"<li>{process_inline(m.group(2))}</li>")
        elif re.match(r'^</?(?:h[1-6]|div|p|ul|ol|li|hr|blockquote|pre|table|tr|td|th)[ >/]', line):
            # Escape raw HTML to prevent XSS — Kindle doesn't need raw HTML passthrough
            output.append(html.escape(line))
        elif re.match(r'^---+$', line):
            output.append("<hr>")
        elif not line.strip():
            if in_list:
                output.append(f"</{list_type}>")
                in_list = False
            output.append("")
        else:
            output.append(f"<p>{process_inline(line)}</p>")

    if in_list:
        output.append(f"</{list_type}>")
    if in_code_block:
        output.append("</pre>")

    return "\n".join(output), page_counter


def process_inline(text):
    """Process inline markdown (bold, italic, code, links, wiki links)."""
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    def _wikilink_replace(m):
        note = m.group(1)
        display = note.split("|")[-1] if "|" in note else note
        link_target = note.split("|")[0]
        enc = urllib.parse.quote(link_target)
        return f'<a href="/?action=open&amp;note={enc}" class="wikilink">[[{html.escape(display)}]]</a>'
    text = re.sub(r'\[\[([^\]]+)\]\]', _wikilink_replace, text)
    def _safe_link(m):
        label, url = m.group(1), m.group(2)
        url_raw = html.unescape(url)
        if not re.match(r'^(https?://|obsidian://|#)', url_raw):
            return f'{label}'
        return f'<a href="{url}">{label}</a>'
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _safe_link, text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', text)
    text = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', text)
    return text


# =============================================================================
# VAULT FILE RESOLVER
# =============================================================================

_vault_index = {}


def _build_vault_index():
    """Walk the vault and index all .md files by basename (no extension)."""
    _vault_index.clear()
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                name = f[:-3]
                rel = os.path.relpath(os.path.join(root, f), VAULT)
                _vault_index[name.lower()] = rel


def resolve_note(note_ref):
    """Resolve a wiki link reference to a vault-relative file path."""
    note = note_ref.split("#")[0].strip()
    if not _vault_index:
        _build_vault_index()
    result = _vault_index.get(note.lower())
    if result:
        return result
    if "/" in note:
        candidate = note if note.endswith(".md") else note + ".md"
        if os.path.exists(os.path.join(VAULT, candidate)):
            return candidate
    return None


# =============================================================================
# HTTP SERVER
# =============================================================================

VAULT_NAME = os.path.basename(VAULT)


_vault_real = os.path.realpath(VAULT)


def _safe_vault_path(file_param):
    """Resolve file_param to an absolute path inside VAULT.
    Returns None if path escapes vault, targets hidden dirs, or isn't a .md file."""
    filepath = os.path.realpath(os.path.join(VAULT, file_param))
    if not filepath.startswith(_vault_real + os.sep) and filepath != _vault_real:
        return None
    # Block hidden directories (.git, .obsidian, etc.)
    rel = os.path.relpath(filepath, _vault_real)
    if any(part.startswith(".") for part in rel.split(os.sep)):
        return None
    # Only allow markdown files
    if not filepath.endswith(".md"):
        return None
    return filepath


class Handler(BaseHTTPRequestHandler):
    MAX_BODY = 4096  # max POST body size

    def check_auth(self):
        cookie_header = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie(cookie_header)
        except Exception:
            cookies = SimpleCookie()
        cookie = cookies.get("auth")
        return cookie and cookie.value == AUTH_COOKIE

    def _handle_toggle(self, query):
        """Toggle a markdown checkbox on the specified line."""
        file_param = query.get("file", [None])[0]
        try:
            line_num = int(query.get("line", ["0"])[0])
        except (ValueError, TypeError):
            line_num = 0
        if not file_param or not line_num:
            self._redirect_back(query)
            return
        filepath = _safe_vault_path(file_param)
        if not filepath or not os.path.exists(filepath):
            self._redirect_back(query)
            return
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if line_num < 1 or line_num > len(lines):
            self._redirect_back(query)
            return
        idx = line_num - 1
        if "- [ ] " in lines[idx]:
            lines[idx] = lines[idx].replace("- [ ] ", "- [x] ", 1)
        elif "- [x] " in lines[idx]:
            lines[idx] = lines[idx].replace("- [x] ", "- [ ] ", 1)
        else:
            self._redirect_back(query)
            return
        import tempfile
        dir_ = os.path.dirname(filepath)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_, delete=False) as tmp:
            tmp.writelines(lines)
            tmp_path = tmp.name
        os.replace(tmp_path, filepath)
        self._redirect_back(query)

    def _handle_open(self, query):
        """Open a wiki-linked note: show in viewer + open in Obsidian on Mac."""
        note_ref = query.get("note", [None])[0]
        if not note_ref:
            self._redirect_back(query)
            return
        rel_path = resolve_note(note_ref)
        if not rel_path:
            self._redirect_back(query)
            return
        if not _safe_vault_path(rel_path):
            self._redirect_back(query)
            return
        file_for_obsidian = rel_path.replace(".md", "")
        obsidian_uri = f"obsidian://open?vault={urllib.parse.quote(VAULT_NAME)}&file={urllib.parse.quote(file_for_obsidian)}"
        subprocess.Popen(["open", obsidian_uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.send_response(302)
        self.send_header("Location", f"/?file={urllib.parse.quote(rel_path)}")
        self.end_headers()

    def _redirect_back(self, query):
        """Redirect back, preserving scroll position via anchor fragment."""
        raw_referer = self.headers.get("Referer", "/")
        # Validate same-origin: must start with / but not // (protocol-relative)
        # Strip control chars to prevent header injection
        referer = raw_referer if (raw_referer.startswith("/") and not raw_referer.startswith("//")) else "/"
        referer = re.sub(r'[\r\n]', '', referer)
        anchor = query.get("anchor", [None])[0]
        if anchor:
            anchor = re.sub(r'[\r\n]', '', anchor)
            base = referer.split("#")[0]
            if "scrolled=" not in base:
                sep = "&" if "?" in base else "?"
                base += f"{sep}scrolled=1"
            location = f"{base}#{anchor}"
        else:
            location = referer
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0
        if content_length < 0 or content_length > self.MAX_BODY:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(content_length).decode()
        params = urllib.parse.parse_qs(body)
        password = params.get("pass", [""])[0]

        if password == AUTH_PASS:
            self.send_response(302)
            self.send_header("Set-Cookie", f"auth={AUTH_COOKIE}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(LOGIN_PAGE.encode())

    def do_GET(self):
        if not self.check_auth():
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(LOGIN_PAGE.encode())
            return

        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)

        action = query.get("action", [None])[0]
        if action == "toggle":
            self._handle_toggle(query)
            return
        elif action == "open":
            self._handle_open(query)
            return

        try:
            offset = int(query.get("day", ["0"])[0])
        except (ValueError, TypeError):
            offset = 0
        file_param = query.get("file", [None])[0]
        view = query.get("view", [None])[0]
        has_anchor = "scrolled" in query

        if view == "diff":
            content, total_pages = markdown_to_html(get_diff_view())
            note_date = "Git Diff"
        else:
            if file_param and "/" in file_param:
                filepath = _safe_vault_path(file_param)
                if not filepath:
                    self.send_response(403)
                    self.end_headers()
                    return
                note_date = os.path.basename(file_param).replace(".md", "")
            elif file_param:
                filepath = os.path.realpath(os.path.join(DAILY_DIR, file_param))
                if not filepath.startswith(os.path.realpath(DAILY_DIR) + os.sep):
                    self.send_response(403)
                    self.end_headers()
                    return
                note_date = file_param.replace(".md", "")
            else:
                filepath, target_date = get_daily_file(offset)
                note_date = target_date.strftime("%Y-%m-%d")

            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    raw = f.read()
                fm_lines = 0
                if raw.startswith("---"):
                    end = raw.find("---", 3)
                    if end != -1:
                        fm_section = raw[:end + 3]
                        fm_lines = fm_section.count("\n")
                        raw = raw[end + 3:]
                        stripped = raw.lstrip("\n")
                        fm_lines += len(raw) - len(stripped)
                        raw = stripped
                today_str = date.today().strftime("%Y-%m-%d")
                if f"### [[{today_str}]]" in raw or f"## [[{today_str}]]" in raw:
                    scroll_anchor = _slug(today_str)
                elif "### Next Actions" in raw or "## Next Actions" in raw:
                    scroll_anchor = "next-actions"
                else:
                    scroll_anchor = ""
                if not has_anchor and scroll_anchor:
                    q_parts = []
                    if file_param:
                        q_parts.append(f"file={urllib.parse.quote(file_param)}")
                    elif offset != 0:
                        q_parts.append(f"day={offset}")
                    q_parts.append("scrolled=1")
                    redirect_url = "/?" + "&".join(q_parts) + f"#{scroll_anchor}"
                    self.send_response(302)
                    self.send_header("Location", redirect_url)
                    self.end_headers()
                    return
                rel_path = os.path.relpath(filepath, VAULT)
                content, total_pages = markdown_to_html(raw, file_path=rel_path, line_offset=fm_lines)
                feed_path = os.path.join(FEED_DIR, f"{note_date}_daily-feed.md")
                if os.path.exists(feed_path):
                    with open(feed_path, "r", encoding="utf-8") as ff:
                        feed_raw = ff.read()
                    feed_fm_lines = 0
                    if feed_raw.startswith("---"):
                        end_fm = feed_raw.find("---", 3)
                        if end_fm != -1:
                            feed_fm_section = feed_raw[:end_fm + 3]
                            feed_fm_lines = feed_fm_section.count("\n")
                            feed_raw = feed_raw[end_fm + 3:]
                            feed_stripped = feed_raw.lstrip("\n")
                            feed_fm_lines += len(feed_raw) - len(feed_stripped)
                            feed_raw = feed_stripped
                    feed_rel = os.path.relpath(feed_path, VAULT)
                    feed_html, feed_pages = markdown_to_html(feed_raw, file_path=feed_rel, line_offset=feed_fm_lines)
                    content += '<hr><h2 id="daily-feed">Daily Feed</h2>' + feed_html
                    total_pages += feed_pages
            else:
                content = f"<p>No note for {html.escape(note_date)}</p>"
                total_pages = 0

        week_days = get_week_days()
        day_buttons_parts = []
        for name, day_offset, d in week_days:
            is_active = (not file_param and view != "diff" and day_offset == offset)
            style = 'style="background:#cfc;font-weight:bold;"' if is_active else ""
            label = f"{name} {d.day}"
            day_buttons_parts.append(f'<a href="/?day={day_offset}" {style}>{label}</a>')
        day_buttons = " ".join(day_buttons_parts)

        period_files = get_period_files()
        period_parts = []
        for label, filename in period_files:
            is_active = (file_param == filename)
            style = 'style="background:#cfc;font-weight:bold;"' if is_active else 'style="background:#eef;"'
            period_parts.append(f'<a href="/?file={filename}" {style}>{label}</a>')
        period_buttons = " ".join(period_parts)

        is_diff_view = (view == "diff")
        diff_style = 'style="background:#cfc;font-weight:bold;"' if is_diff_view else 'style="background:#ffe;"'
        diff_button = f'<a href="/?view=diff" {diff_style}>Diff</a>'

        active_file = get_obsidian_active_file()
        if active_file:
            active_name = os.path.basename(active_file).replace(".md", "")
            display_name = active_name[:ACTIVE_FILE_MAX_CHARS] + "..." if len(active_name) > ACTIVE_FILE_MAX_CHARS else active_name
            is_showing_active = (file_param == active_file)
            active_style = 'style="background:#fcf;font-weight:bold;"' if is_showing_active else 'style="background:#fef;"'
            active_button = f'<a href="/?file={urllib.parse.quote(active_file)}" {active_style}>{html.escape(display_name)}</a>'
        else:
            active_button = ""

        now_ts = int(time.time())
        time_str = time.strftime("%H:%M")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()

        try:
            cur_page = int(query.get("pg", ["0"])[0]) if "pg" in query else 0
        except (ValueError, TypeError):
            cur_page = 0
        prev_page = max(0, cur_page - 1)
        next_page = min(total_pages, cur_page + 1)

        page = (HTML_TEMPLATE
            .replace("{timestamp}", str(now_ts))
            .replace("{time_str}", time_str)
            .replace("{diff_button}", diff_button)
            .replace("{active_button}", active_button)
            .replace("{day_buttons}", day_buttons)
            .replace("{period_buttons}", period_buttons)
            .replace("{content}", content)
            .replace("{prev_page}", str(prev_page))
            .replace("{next_page}", str(next_page))
        )
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(
        prog="kdv",
        description="Kindle Daily Viewer — serve Obsidian notes for e-ink browsers"
    )
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    args = parser.parse_args()

    import socket
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        address_family = socket.AF_INET
    server = ReusableHTTPServer(("0.0.0.0", args.port), Handler)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    print(f"Kindle Daily Viewer running on port {args.port}")
    print(f"Vault: {VAULT}")
    print(f"Daily dir: {DAILY_DIR}")
    print(f"\nAccess: http://{local_ip}:{args.port}/")
    if not _cfg("KDV_PASSWORD"):
        print(f"WARNING: Using default password '{AUTH_PASS}'. Set KDV_PASSWORD in config.env.")
    else:
        print("Password: (set via KDV_PASSWORD)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
