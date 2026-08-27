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
import html, argparse, time, os, re, urllib.parse, hashlib, subprocess, threading, smtplib
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
    """Load KDV_* variables from config.env (KEY=VALUE format, # comments).
    Supports ${VAR} expansion from environment variables and ~/.env file."""
    # Load ~/.env first for variable expansion
    env_extras = {}
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env_extras[k.strip()] = v.strip().strip('"').strip("'")
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
                # Expand ${VAR} references
                import re as _re
                for match in _re.findall(r'\$\{(\w+)\}', value):
                    value = value.replace(f"${{{match}}}", env_extras.get(match, os.environ.get(match, "")))
                if key.startswith("KDV_"):
                    config[key] = value
    return config


def _detect_vault():
    """Auto-detect Obsidian vault path. Tries (in order):
    1. Obsidian's vault registry (obsidian.json — lists all known vaults)
    2. Common directory scan (~/, ~/Documents/, ~/Documents/GitHub/)
    Returns the first valid vault path found, or None.
    """
    import json as _json

    # Strategy 1: Read Obsidian's vault registry
    obsidian_config = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if not obsidian_config.exists():
        obsidian_config = Path.home() / ".config" / "obsidian" / "obsidian.json"
    if obsidian_config.exists():
        try:
            data = _json.loads(obsidian_config.read_text())
            vaults = data.get("vaults", {})
            candidates = sorted(
                ((v.get("path", ""), v.get("ts", 0)) for v in vaults.values() if v.get("path")),
                key=lambda x: x[1], reverse=True,
            )
            for vault_path, _ in candidates:
                if os.path.isdir(os.path.join(vault_path, ".obsidian")):
                    return vault_path
        except Exception:
            pass

    # Strategy 2: Scan common locations for .obsidian/ directories
    home = Path.home()
    for search_dir in [home, home / "Documents", home / "Documents" / "GitHub"]:
        if not search_dir.is_dir():
            continue
        for child in sorted(search_dir.iterdir()):
            if child.is_dir() and (child / ".obsidian").is_dir():
                return str(child)

    return None


def _auto_create_config(vault_path):
    """Create default config.env with detected vault path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        f"# Kindle Daily Viewer configuration (auto-generated)\n"
        f"# Edit values below or set KDV_* environment variables.\n\n"
        f"# Path to your Obsidian vault (auto-detected)\n"
        f"KDV_VAULT={vault_path}\n\n"
        f"# Daily notes directory (default: $KDV_VAULT/log)\n"
        f"# KDV_DAILY_DIR=\n\n"
        f"# Server port (default: 8080)\n"
        f"# KDV_PORT=8080\n\n"
        f"# Login password (default: password — change this!)\n"
        f"# KDV_PASSWORD=your-password-here\n"
    )
    print(f"Auto-created config: {CONFIG_FILE}")
    print(f"  Detected vault: {vault_path}")
    print(f"  Edit {CONFIG_FILE} to customize.\n")


_file_config = _load_config_file()


def _cfg(key, default=None):
    """Get config value: env var > config file > default."""
    return os.environ.get(key) or _file_config.get(key) or default


# Required config — KDV_VAULT must be set.
# If missing, try auto-detection and create config.env on first run.
VAULT = _cfg("KDV_VAULT")
if not VAULT:
    detected = _detect_vault()
    if detected:
        _auto_create_config(detected)
        _file_config = _load_config_file()
        VAULT = _cfg("KDV_VAULT")
    else:
        print("Error: KDV_VAULT is not set and no Obsidian vault found.")
        print(f"Create {CONFIG_FILE} with:\n  KDV_VAULT=/path/to/your/obsidian/vault")
        print("Or set the KDV_VAULT environment variable.")
        raise SystemExit(1)

DAILY_DIR = _cfg("KDV_DAILY_DIR", os.path.join(VAULT, "log"))
PORT = int(_cfg("KDV_PORT", "8080"))
DIFF_TRUNCATE_BYTES = int(_cfg("KDV_DIFF_TRUNCATE", "50000"))
ACTIVE_FILE_MAX_CHARS = 20
FEED_DIR = _cfg("KDV_FEED_DIR", os.path.join(VAULT, "feed"))
WORKTREE_DIR = _cfg("KDV_WORKTREE_DIR", "")
# Optional owner/prefix segment to strip from worktree dir names in the picker (e.g. if your
# worktrees are named "repo_<owner>_<ticket>_<desc>", set this to "_<owner>_" for a cleaner
# label on a narrow e-ink screen). Empty = show the raw dir name.
WORKTREE_OWNER_SEGMENT = _cfg("KDV_WORKTREE_OWNER_SEGMENT", "")
EXTRA_REPOS = [p.strip() for p in _cfg("KDV_EXTRA_REPOS", "").split(",") if p.strip()]
GITHUB_DESKTOP = _cfg("KDV_GITHUB_DESKTOP", "").lower() in ("true", "1", "yes")
KINDLE_EMAIL = _cfg("KDV_KINDLE_EMAIL", "")
SMTP_USER = _cfg("KDV_SMTP_USER", "")
SMTP_PASSWORD = _cfg("KDV_SMTP_PASSWORD", "")
PDF_AUTHOR = _cfg("KDV_PDF_AUTHOR", "")

# Auth — password login via HTML form + SHA256 cookie.
# Cookie is not reversible from network sniffing. Fine for LAN.
AUTH_PASS = _cfg("KDV_PASSWORD", "password")
AUTH_COOKIE = hashlib.sha256(AUTH_PASS.encode()).hexdigest()[:16]

# =============================================================================
# HTML TEMPLATES
# =============================================================================

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — KDV</title>
<style>
body { margin: 40px; background: #fff; color: #000; font-family: Georgia, serif; font-size: 16px; }
input { font-size: 16px; padding: 8px; margin: 4px 0; }
button { font-size: 16px; padding: 8px 16px; background: #eee; border: 2px solid #333; color: #000; }
</style>
</head><body>
<h2>Daily Notes</h2>
<form method="POST" action="/login">
<label for="pass">Password</label><br>
<input type="password" id="pass" name="pass" placeholder="Password"><br>
<button type="submit">Enter</button>
</form>
</body></html>
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_title}</title>
<style>
* { box-sizing: border-box; max-width: 100vw; }
body { margin: 0; padding: 0; background: #fff; color: #000; font-family: Georgia, serif; font-size: 15px; line-height: 1.5; overflow-x: hidden; word-wrap: break-word; overflow-wrap: break-word; }
/* Nav is sticky (in normal flow), NOT fixed: when the buttons wrap to 2-3 rows on a
   narrow e-ink screen, the content flows below the real nav height instead of being
   hidden under a fixed bar with a guessed-wrong 40px padding. */
.nav { background: #eee; padding: 6px 8px; font-family: monospace; font-size: 12px; border-bottom: 2px solid #000; position: sticky; top: 0; left: 0; right: 0; z-index: 99; line-height: 2; }
.content { padding-top: 4px; }
.nav a { margin-right: 4px; text-decoration: none; color: #000; padding: 8px; display: inline-block; }
.nav a.refresh { background: #ddd; font-weight: bold; border: 1px solid #333; }
h1, h2, h3 { scroll-margin-top: 50px; }
h1 { font-size: 20px; margin: 8px 0; word-wrap: break-word; overflow-wrap: break-word; }
h2 { font-size: 17px; margin: 8px 0 4px; border-bottom: 1px solid #ccc; word-wrap: break-word; overflow-wrap: break-word; }
h3 { font-size: 15px; margin: 6px 0 4px; word-wrap: break-word; overflow-wrap: break-word; }
h4 { font-size: 14px; margin: 0; word-wrap: break-word; overflow-wrap: break-word; padding: 2px 4px; border-left: 3px solid #000; }
.diff-section { position: relative; }
.top-right-btn { position: fixed; top: 40px; right: 10px; z-index: 100; }
.top-right-btn .pg-btn { margin-bottom: 8px; }
.page-btns { position: fixed; top: 50%; right: 10px; z-index: 100; margin-top: -48px; }
.page-btns .pg-btn { display: block; margin-bottom: 16px; }
.pg-btn { display: block; width: 44px; height: 44px; line-height: 44px; text-align: center; font-size: 20px; background: transparent; border: 1px solid #999; border-radius: 4px; text-decoration: none; color: #333; }
pre { background: #fff; padding: 0; font-size: 13px; line-height: 1.35; font-family: "SF Mono", Menlo, Consolas, monospace; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word; overflow-x: hidden; max-width: 100%; border: 1px solid #d0d7de; border-radius: 6px; margin: 6px 0; }
/* GitHub diff rows: CONTIGUOUS (no vertical gaps/margins between lines — that's what
   made it read "torn"/撕裂). Full-width row bg + inline gutter prefix. Rows are flush. */
.diff-add, .diff-del, .diff-ctx { display: block; color: #24292f; margin: 0; padding: 0; word-wrap: break-word; overflow-wrap: break-word; }
.diff-add { background: #e6ffec; }
.diff-del { background: #ffebe9; }
.diff-gut { display: inline-block; min-width: 2.4em; padding: 0 6px; margin: 0 6px 0 0; color: #57606a; background: #f6f8fa; text-align: right; -webkit-user-select: none; }
.diff-add .diff-gut { background: #ccffd8; color: #1a7f37; }
.diff-del .diff-gut { background: #ffd7d5; color: #cf222e; }
.diff-hunk { color: #57606a; display: block; background: #ddf4ff; padding: 2px 6px; margin: 0; font-weight: bold; word-wrap: break-word; overflow-wrap: break-word; }
/* Word-diff: mark ONLY changed words. Non-color cues (strikethrough / bold+underline)
   so it still reads on grayscale e-ink where red≈green. */
.wd-del { background: #ffd7d5; color: #82071e; text-decoration: line-through; }
.wd-add { background: #ccffd8; color: #1a7f37; font-weight: bold; text-decoration: underline; }
/* v3 GitHub-PR-style review: sticky per-file header, fold via <details>, reviewed-checkbox
   collapses the file body with pure CSS (:checked + sibling), no JS — works on e-ink. */
.rv-cb { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }  /* visually hidden but stays in place; label is the control */
.rv-cb:focus + .rv-file .rv-mark { outline: 2px solid #0969da; outline-offset: 2px; }
.rv-file { border: 1px solid #d0d7de; border-radius: 6px; margin: 8px 0; }
.rv-head { position: sticky; top: 34px; background: #f6f8fa; padding: 8px 10px; border-bottom: 1px solid #d0d7de; font-size: 15px; cursor: pointer; word-break: break-word; z-index: 5; }
.rv-mark { display: inline-block; min-width: 1.4em; height: 1.4em; line-height: 1.4em; text-align: center; border: 1px solid #999; border-radius: 4px; color: #767676; margin-right: 6px; text-decoration: none; }
.rv-cb:checked + .rv-file .rv-mark { background: #1a7f37; color: #fff; border-color: #1a7f37; }  /* reviewed = green check */
.rv-cb:checked + .rv-file .rv-head { background: #e6ffec; }
.rv-cb:checked + .rv-file > pre { display: none; }  /* fold the diff when reviewed */
.rv-cb:checked + .rv-file .rv-stat::after { content: " · reviewed ✓"; color: #1a7f37; }
.rv-stat { color: #57606a; font-weight: normal; font-size: 13px; }
code { background: #f0f0f0; padding: 1px 4px; font-size: 13px; word-wrap: break-word; overflow-wrap: break-word; }
img { max-width: 100%; height: auto; }
.review-pick { margin: 8px 0; }
.review-wt { display: block; padding: 12px 10px; margin: 0 0 6px; border: 1px solid #999; border-radius: 4px; font-size: 17px; font-weight: bold; text-decoration: none; color: #000; word-break: break-word; }
.review-meta { display: block; font-size: 13px; font-weight: normal; color: #555; margin-top: 3px; }
ul, ol { padding-left: 20px; }
li { margin: 2px 0; }
.checkbox { font-family: monospace; }
.toggle { text-decoration: none; color: #000; padding: 4px; font-weight: bold; }
.annotate { text-decoration: none; color: #000; font-size: 11px; margin-left: 4px; }
.line-pen { text-decoration: none; color: #000; font-size: 14px; padding: 4px 8px; margin-left: 4px; }
.done { color: #000; text-decoration: line-through; }
blockquote { border-left: 3px solid #000; margin: 8px 0; padding: 4px 12px; color: #000; }
a { color: #000; }
.wikilink { color: #000; font-weight: bold; text-decoration: none; }
</style>
</head><body>
<nav class="nav" aria-label="Navigation">
<a href="/?t={timestamp}" class="refresh">{time_str}</a>
{day_buttons}
{period_buttons}
{diff_button}
{review_button}
{active_button}
</nav>
<div id="top"></div>
<main class="content">{content}</main>
<div id="bottom"></div>
<div class="top-right-btn">{kindle_button}</div>
<div class="page-btns">
<a href="{up_url}" class="pg-btn" aria-label="Scroll to top">&uarr;</a>
{nav_button}
<a href="{down_url}" class="pg-btn" aria-label="Scroll to bottom">&darr;</a>
</div>
</body></html>
"""

_CONTENT_SENTINEL = "\x00KDV_CONTENT\x00"
_CHROME_TOKEN_RE = re.compile(r'\{[a-z_]+\}')


def _render_page(content, **chrome):
    """Fill HTML_TEMPLATE with chrome values in ONE non-recursive pass, then inject
    content LAST.

    A chained .replace() per placeholder re-scans the growing output on every step,
    so an EARLIER substitution's inserted text (e.g. a chrome value that happens to
    contain the literal string "{content}" — say, an active-file button whose title
    IS "{content}") gets corrupted by a LATER placeholder's replace() call. Doing all
    chrome substitution in one regex pass over the ORIGINAL template avoids that: the
    replacement text is never rescanned for further matches. {content} is swapped in
    via a control-character sentinel (never present in chrome HTML we generate) so the
    final content substitution can't accidentally also match literal "{content}" text
    that arrived via a chrome value.
    """
    page = HTML_TEMPLATE.replace("{content}", _CONTENT_SENTINEL)
    page = _CHROME_TOKEN_RE.sub(lambda m: chrome.get(m.group(0)[1:-1], m.group(0)), page)
    return page.replace(_CONTENT_SENTINEL, content)


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


def _is_git_repo(path):
    """True if path has a .git dir (normal repo) or .git file (worktree/submodule)."""
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def _clamp_ctx(ctx_lines, default=1, max_val=10):
    """Parse+clamp a &ctx= query value to [0, max_val], falling back to default."""
    try:
        return max(0, min(int(ctx_lines), max_val)) if ctx_lines is not None else default
    except (ValueError, TypeError):
        return default


def _scroll_anchor(raw_text, today_str, default=""):
    """Pick the anchor to auto-jump to: today's dated section, else Next Actions, else default."""
    if not raw_text:
        return default
    if f"### [[{today_str}]]" in raw_text or f"## [[{today_str}]]" in raw_text:
        return _slug(today_str)
    if "### Next Actions" in raw_text or "## Next Actions" in raw_text:
        return "next-actions"
    return default


def _short_worktree_name(name):
    """Shorten a worktree dir name for the phone picker.

    If KDV_WORKTREE_OWNER_SEGMENT is set (e.g. "_owner_"), drop everything up to and
    including that segment so the ticket + description read cleanly on a narrow e-ink
    screen. Falls back to the raw name if unset or the segment isn't present.
    """
    if not WORKTREE_OWNER_SEGMENT:
        return name
    parts = name.split(WORKTREE_OWNER_SEGMENT, 1)
    return parts[1] if len(parts) == 2 and parts[1] else name


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
                    output.append(f"<pre>\n{chunk}\n</pre>\n")
                    output.append("</div>")
                else:
                    output.append(f"<pre>\n{chunk}\n</pre>\n")

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
                            output.append(f'<div class="diff-section"><h4 id="{file_anchor}">{html.escape(fname)}</h4>')
                            output.append(f"<pre>\n{chunk}\n</pre>\n")
                            output.append("</div>")
                        else:
                            output.append(f"<pre>\n{chunk}\n</pre>\n")
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


def _git(repo, *args, timeout=10):
    """Run a git command in repo, return stdout ('' on failure). Never raises."""
    ok, out, _ = _git_ex(repo, *args, timeout=timeout)
    return out if ok else ""


def _git_ex(repo, *args, timeout=10, ok_codes=(0,)):
    """Run a git command, returning (ok, stdout, err).

    ok is False on an unexpected exit code, timeout, or exception — so callers
    can tell a genuine empty result (clean repo) apart from a FAILURE. Treating a
    failed git call as '' silently rendered 'No changes', a lie when the diff
    actually stalled or the path wasn't a repo.

    ok_codes lists the exit codes that count as success AND whose stdout is kept.
    `git diff --no-index` exits 1 when the files differ, so that caller passes
    ok_codes=(0, 1) to keep the diff instead of dropping it as a "failure".
    """
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                            capture_output=True, text=True, timeout=timeout,
                            errors="replace")  # odd byte content in diffs must not crash decode
        if r.returncode in ok_codes:
            return True, r.stdout, ""
        return False, "", (r.stderr or "").strip() or f"git exited {r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "", f"git timed out after {timeout}s"
    except Exception as e:
        return False, "", str(e)


_WT_CACHE = {"ts": 0.0, "rows": []}
_WT_CACHE_TTL = int(_cfg("KDV_REVIEW_CACHE_TTL", "60"))  # seconds — picker is expensive over 50+ worktrees


def list_review_worktrees(force=False):
    """All worktrees under WORKTREE_DIR, sorted by last-commit recency (newest first).

    Returns list of dicts: {name, path, last_ts, last_rel, unpushed, dirty}.
    Powers the /review picker — 'recently-active first' so the worktree you are
    working on floats to the top of a long (50+) list.

    PERF (phone/Kindle over LAN): building this is expensive — 2 git calls per
    worktree × 50+ = seconds. So the result is CACHED for KDV_REVIEW_CACHE_TTL
    seconds. Repeat picker loads within the window are instant; the cache refreshes
    lazily on the first request after it expires. Git calls also trimmed 4→2 per
    worktree (recency in one `log`, dirty+ahead in one `status -sb`).
    """
    now = time.time()
    if not force and _WT_CACHE["rows"] and (now - _WT_CACHE["ts"]) < _WT_CACHE_TTL:
        return _WT_CACHE["rows"]
    if not WORKTREE_DIR or not os.path.isdir(WORKTREE_DIR):
        return []
    rows = []
    for name in os.listdir(WORKTREE_DIR):
        path = os.path.join(WORKTREE_DIR, name)
        if not _is_git_repo(path):
            continue
        # 1 call: last-commit epoch + relative time
        log_line = _git(path, "log", "-1", "--format=%ct%x1f%cr").strip()
        last_ts, rel = 0, "unknown"
        if "\x1f" in log_line:
            ts_raw, rel_raw = log_line.split("\x1f", 1)
            if ts_raw.isdigit():
                last_ts = int(ts_raw)
            rel = rel_raw or "unknown"
        # 1 call: status -sb gives dirty (file lines) AND ahead count (branch line)
        sb = _git(path, "status", "--short", "--branch")
        lines = sb.splitlines()
        dirty = any(l and not l.startswith("##") for l in lines)
        unpushed = 0
        if lines and lines[0].startswith("##"):
            m = re.search(r"ahead (\d+)", lines[0])
            if m:
                unpushed = int(m.group(1))
        rows.append({"name": name, "path": path, "last_ts": last_ts,
                     "last_rel": rel, "unpushed": unpushed, "dirty": dirty})
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    _WT_CACHE["rows"] = rows
    _WT_CACHE["ts"] = now
    return rows


def _review_repo_from_param(repo_param):
    """Resolve a ?repo= value to a safe absolute path under WORKTREE_DIR or VAULT.

    Prevents path traversal — only allows the vault itself or a direct child of
    WORKTREE_DIR. Returns absolute path or None.
    """
    if not repo_param:
        return None
    if repo_param == "vault":
        return VAULT
    if not WORKTREE_DIR:
        return None
    # Reject anything but a bare directory name — no separators, no '.'/'..' traversal,
    # no absolute paths. (Codex: repo=. or a non-git dir let `git -C` walk up to a parent repo.)
    if "/" in repo_param or "\\" in repo_param or repo_param in (".", "..") or repo_param.startswith("."):
        return None
    candidate = os.path.realpath(os.path.join(WORKTREE_DIR, repo_param))
    wt_root = os.path.realpath(WORKTREE_DIR)
    # Must be a DIRECT child of the worktree root AND an actual git repo (has .git).
    if os.path.dirname(candidate) != wt_root:
        return None
    if not os.path.isdir(candidate):
        return None
    if not _is_git_repo(candidate):
        return None
    return candidate


def _file_diff_chunk(repo, diff_specs, path, ctx, word_diff, untracked=False):
    """Return (chunk, err) — the diff text for ONE file across the scope's specs.

    chunk is '' with err set when a git call FAILED (so the caller can show the
    error instead of a misleading 'No diff'); chunk is '' with err '' means the
    file genuinely has no diff in this scope.

    Falls back to `diff-tree --root` for a first commit (no parent). For an
    untracked regular file show its content as an all-additions diff via
    `diff --no-index /dev/null <file>` so brand-new files are reviewable.
    """
    base_args = ["diff", "--no-ext-diff", f"-U{ctx}"]
    if word_diff:
        base_args += ["--word-diff=plain", "--word-diff-regex=\\w+|[^[:space:]]"]
    first_err = None
    is_first_commit = lambda rev: len(rev) == 1 and "~1.." in rev[0]
    for section, rev in diff_specs:
        ok, c, err = _git_ex(repo, *base_args, *rev, "--", path)
        if not ok:
            first_err = first_err or err
            c = ""  # don't skip the root fallback below — a first commit's `sha~1..sha`
                    # legitimately fails (no parent), and diff-tree --root recovers it.
        if not c.strip() and is_first_commit(rev):
            sha = rev[0].split("~1..")[-1]
            dt_args = ["diff-tree", "-p", f"-U{ctx}"] + (["--word-diff=plain"] if word_diff else [])
            _, c, _ = _git_ex(repo, *dt_args, "--root", sha, "--", path)
        if c.strip():
            return c, None
    if untracked:
        # Only diff a regular file — `?? dir/` from git status can't be shown as content.
        # islink guard: os.path.isfile follows symlinks, so an untracked symlink to a
        # regular file would otherwise be diffed as its target's content.
        abspath = os.path.join(repo, path)
        if os.path.isfile(abspath) and not os.path.islink(abspath):
            # `git diff --no-index` exits 1 when files differ (always, vs /dev/null),
            # so accept exit code 1 and KEEP its stdout — that IS the diff.
            ok, c, err = _git_ex(repo, "diff", "--no-ext-diff", f"-U{ctx}",
                                 "--no-index", "--", os.devnull, path, ok_codes=(0, 1))
            if ok and c.strip():
                return c, None
            first_err = first_err or err
    return "", first_err


def get_review_view(repo_param=None, scope=None, file_only=None, history_n=50, ctx_lines=None, word_diff=False):
    """Phone code-review, GitHub-PR-style but PRE-PUSH. Optimized for e-ink (every
    tap = a full-page reload + screen flash, so minimize taps).

    1. No repo → worktree PICKER (recency-sorted, cached).
    2. repo → the diff loads STRAIGHT AWAY at the default scope (unpushed + local),
       files-summary first. Above it: SCOPE BUTTONS (one-tap presets: unpushed+local,
       local, last, unpushed+3) and the categorized commit list (local / unpushed /
       pushed, with date-time + branch base). Re-scope = one tap.
    3. &file=<path> → that one file's diff only (lazy per-file load).

    scope values: 'unpushed_local' (default), 'local', 'last', 'unpushed', 'unpushed3', 'all'.
    """
    # Stage 1: picker. Big tap targets, shortened names, status on its own line —
    # rendered as HTML (not a dense markdown list) so it's readable on e-ink.
    repo = _review_repo_from_param(repo_param)
    if not repo:
        rows = list_review_worktrees()
        out = ["## Review — pick a worktree\n"]
        out.append("**[Obsidian vault](/?view=review&repo=vault)**\n")
        if not rows:
            out.append("*No worktrees found (KDV_WORKTREE_DIR unset or empty).*")
            return "\n".join(out)
        # One worktree per block: bold tappable name (big), status on the next line.
        # Uses markdown links (rendered to <a> by process_inline) so the renderer
        # doesn't escape them — the raw-<a> approach got HTML-escaped as prose.
        for r in rows:
            enc = urllib.parse.quote(r["name"])
            short = _short_worktree_name(r["name"])
            status_bits = []
            if r["dirty"]:
                status_bits.append("🟡 local")
            if r["unpushed"]:
                status_bits.append(f"🔵 {r['unpushed']} unpushed")
            if not status_bits:
                status_bits.append("⚪ clean")
            status = " · ".join(status_bits) + f" · {r['last_rel']}"
            out.append(f"### [{short}](/?view=review&repo={enc})")
            out.append(f"{status}\n")
        return "\n".join(out)

    repo_name = os.path.basename(repo)
    enc_repo = urllib.parse.quote(repo_name if repo != VAULT else "vault")

    # Fetch recent commit history with date/time (used by stage 2 list + stage 3 labels)
    log_raw = _git(repo, "log", f"-{history_n}", "--format=%H%x1f%h%x1f%s%x1f%cr%x1f%cd", "--date=format:%Y-%m-%d %H:%M")
    commits = []
    for line in log_raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append({"sha": parts[0], "short": parts[1], "subj": parts[2],
                            "rel": parts[3], "dt": parts[4]})

    # Categorize: which commits are UNPUSHED (ahead of the branch's base) + find the base ref.
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"
    base_ref = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").strip()
    if not base_ref:
        for cand in ("origin/main", "origin/master", "main", "master"):
            if _git(repo, "rev-parse", "--verify", "--quiet", cand).strip():
                base_ref = cand
                break
    unpushed_shas = set()
    if base_ref:
        for s in _git(repo, "rev-list", f"{base_ref}..HEAD").split():
            unpushed_shas.add(s)
    dirty = bool(_git(repo, "status", "--short").strip())

    # Resolve scope → a git diff spec + human label. Default = unpushed + local.
    scope = scope or "unpushed_local"

    def _scope_url(sc):
        return f"/?view=review&repo={enc_repo}&scope={sc}"

    # Scope buttons row (one tap each). Active one marked ▶.
    def _btn(sc, label):
        return f"**[{label}]({_scope_url(sc)})**" if scope == sc else f"[{label}]({_scope_url(sc)})"
    buttons = " · ".join([
        _btn("unpushed_local", "unpushed+local"),
        _btn("local", "local"),
        _btn("last", "last"),
        _btn("unpushed", "unpushed"),
        _btn("all", "all vs base"),
    ])

    out = [f"## {repo_name}\n", f"[← worktrees](/?view=review)\n"]
    base_str = f"`{base_ref}`" if base_ref else "(no base found)"
    out.append(f"*branch `{branch}` vs {base_str} — {len(unpushed_shas)} unpushed" + (", local changes" if dirty else "") + "*")
    out.append(f"\nScope: {buttons}\n")

    # Build the git-diff ARGS for the chosen scope — NOT executed here. The file summary
    # uses --numstat (cheap, no content); the full diff runs only for the ONE tapped file.
    # (Codex High: previously the entire scope diff was captured into memory just to list
    #  filenames, then re-diffed on tap — a huge diff could OOM/stall, and a timeout showed
    #  a false "no changes".)
    diff_specs = []          # list of (section_label, [git args after 'diff'])
    if scope == "local":
        diff_specs.append(("Uncommitted local changes", ["HEAD"]))
    elif scope == "last":
        if commits:
            sha = commits[0]["short"]
            diff_specs.append((f"Last commit {sha}", [f"{sha}~1..{sha}"]))
    elif scope == "unpushed":
        if base_ref and unpushed_shas:
            diff_specs.append((f"Unpushed ({len(unpushed_shas)}) vs {base_ref}", [f"{base_ref}..HEAD"]))
    elif scope == "all":
        if base_ref:
            diff_specs.append((f"All vs {base_ref}", [f"{base_ref}..HEAD"]))
    else:  # unpushed_local (default)
        if base_ref and unpushed_shas:
            diff_specs.append((f"Unpushed ({len(unpushed_shas)}) vs {base_ref}", [f"{base_ref}..HEAD"]))
        if dirty:
            diff_specs.append(("Uncommitted local changes", ["HEAD"]))

    # Ordered file list via --numstat (cheap, no content) — used by the summary AND
    # by the single-file view's prev/next navigation.
    file_stats = []          # (path, added, deleted)
    seen = set()
    git_error = None         # first git failure — shown instead of a false "no changes"
    for section, rev in diff_specs:
        ok, ns, err = _git_ex(repo, "diff", "--numstat", "--no-ext-diff", *rev)
        if not ok:
            git_error = git_error or f"{section}: {err}"
            continue
        for l in ns.splitlines():
            parts = l.split("\t")
            if len(parts) == 3 and parts[2] and parts[2] not in seen:
                seen.add(parts[2])
                file_stats.append((parts[2], parts[0], parts[1]))
    # Untracked files never appear in `git diff` — surface them explicitly so a repo
    # whose only change is a brand-new file doesn't read as "No changes".
    if scope in ("local", "unpushed_local"):
        # --untracked-files=all so files inside a brand-new directory are listed
        # individually (default 'normal' collapses them to '?? dir/', which can't
        # be shown as content).
        ok, st, _ = _git_ex(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if ok:
            for entry in st.split("\x00"):
                if entry.startswith("?? "):
                    path = entry[3:]
                    if path and path not in seen:
                        seen.add(path)
                        file_stats.append((path, "?", "?"))  # unknown counts — untracked
    ordered_paths = [p for p, _, _ in file_stats]

    # Stage 3b: single-file view — run the diff for just this file (lazy, bounded).
    if file_only:
        out.append(f"### {html.escape(file_only)}\n")
        # -U1: show only the REAL changes + 1 line of context, not the whole file.
        # Real-use friction: "not all is diff, I just want the real diff,
        # not full-line check." Toggle to more context via &ctx=N if ever needed.
        ctx = _clamp_ctx(ctx_lines)

        def _file_url(path, c=None):
            u = f"/?view=review&repo={enc_repo}&scope={scope}&file={urllib.parse.quote(path)}"
            return u + (f"&ctx={c}" if c is not None else "")

        # Prev/next position in the ordered file list (review flow: finish one, tap next).
        pos = ordered_paths.index(file_only) if file_only in ordered_paths else -1
        nav_bits = [f"[← all files](/?view=review&repo={enc_repo}&scope={scope})"]
        if pos > 0:
            nav_bits.append(f"[◀ prev]({_file_url(ordered_paths[pos-1], ctx if ctx != 1 else None)})")
        if 0 <= pos < len(ordered_paths) - 1:
            nav_bits.append(f"[next ▶]({_file_url(ordered_paths[pos+1], ctx if ctx != 1 else None)})")
        counter = f"file {pos+1}/{len(ordered_paths)}" if pos >= 0 else ""
        out.append(" · ".join(nav_bits) + (f"  *{counter}*" if counter else ""))

        # WORD-diff mode: show shared text ONCE, mark only the changed words. This is
        # Core real-use friction — "that diff not [is] different, how can I see just the
        # diff part": on near-identical lines, line-diff makes you re-read the whole line.
        words_url = _file_url(file_only) + "&words=1"
        lines_url = _file_url(file_only)
        mode_bar = (f"**word-diff** · [line-diff]({lines_url})" if word_diff
                    else f"[word-diff]({words_url}) · **line-diff**")
        out.append(f"view: {mode_bar} · context: [tight]({_file_url(file_only, 0)}) [more]({_file_url(file_only, 5)})\n")

        untracked = any(p == file_only and a == "?" for p, a, _ in file_stats)
        chunk, err = _file_diff_chunk(repo, diff_specs, file_only, ctx, word_diff, untracked=untracked)
        if chunk.strip():
            out.append("<pre>")
            out.append(chunk)
            out.append("</pre>")
        elif err:
            out.append(f"*git error — {html.escape(err)}*")
        else:
            out.append("*No diff for this file in the current scope.*")
        # Bottom nav — so at the END of scrolling a file, next/prev is right there
        # (real-use ask: go to next file naturally when you reach the end).
        if pos >= 0:
            bottom = []
            if pos > 0:
                bottom.append(f"[◀ prev file]({_file_url(ordered_paths[pos-1], ctx if ctx != 1 else None)})")
            bottom.append(f"[↑ all files](/?view=review&repo={enc_repo}&scope={scope})")
            if pos < len(ordered_paths) - 1:
                bottom.append(f"[next file ▶]({_file_url(ordered_paths[pos+1], ctx if ctx != 1 else None)})")
            out.append("\n---\n" + " · ".join(bottom))
        return "\n".join(out)

    # v3: GitHub-PR-style — ALL files inline on one page. Each file is a <details> (native
    # fold, no JS) with a sticky header + a "reviewed" checkbox (:checked+CSS collapses the
    # body, no JS/reload). A TOC at top gives file-tree + progress.
    # Context tightness reuses the ctx toggle; default -U1 (real changes dominate).
    try:
        ctx = max(0, min(int(ctx_lines), 10)) if ctx_lines is not None else 1
    except (ValueError, TypeError):
        ctx = 1

    def _stat_label(added, deleted):
        if added == "-":
            return "binary"
        if added == "?":
            return "untracked"
        return f"+{added} −{deleted}"

    if git_error:
        # A git call FAILED (timeout, not-a-repo, bad rev). Say so — never render a
        # failure as "No changes", which would hide real work.
        out.append(f"*git error — {html.escape(git_error)}*")
    elif not file_stats:
        out.append("*No changes in this scope.*")
    else:
        # Table of contents / file tree with per-file anchor links + totals.
        total_add = sum(int(a) for _, a, _ in file_stats if a.isdigit())
        total_del = sum(int(d) for _, _, d in file_stats if d.isdigit())
        out.append(f"### {len(file_stats)} files · +{total_add} −{total_del} · tap ✓ to fold reviewed\n")
        # IDs are INDEX-based (f-0, f-1…), NOT _slug(path): _slug collapses . / _ and case to
        # '-', so app/foo.rb and app/foo_rb would collide → tapping ✓ folds the wrong file.
        for idx, (path, added, deleted) in enumerate(file_stats):
            stat = _stat_label(added, deleted)
            out.append(f"- [{html.escape(path)}](#f-{idx}) · {stat}")
        out.append("")
        # Word-diff / context controls apply to the whole page (reload with param).
        base_all = f"/?view=review&repo={enc_repo}&scope={scope}"
        wd_bar = (f"**word** · [line]({base_all})" if word_diff else f"[word]({base_all}&words=1) · **line**")
        out.append(f"view: {wd_bar} · context [tight]({base_all}&ctx=0) [1]({base_all}) [more]({base_all}&ctx=5)\n")

        # Each file: sticky-header <details>, checkbox to fold-when-reviewed.
        for idx, (path, added, deleted) in enumerate(file_stats):
            stat = _stat_label(added, deleted)
            anchor = f"f-{idx}"       # index-based = collision-proof (see TOC note above)
            cb_id = f"rv-{idx}"
            # diff for THIS file only (lazy per file — bounded), respecting word/ctx mode.
            chunk, err = _file_diff_chunk(repo, diff_specs, path, ctx, word_diff,
                                          untracked=(added == "?"))
            # checkbox + details. :checked collapses the file body (CSS handles it, no JS).
            out.append(f'<input type="checkbox" id="{cb_id}" class="rv-cb" aria-label="Mark {html.escape(path)} reviewed">')
            out.append(f'<details class="rv-file" id="{anchor}" open>')
            out.append(f'<summary class="rv-head"><label for="{cb_id}" class="rv-mark">✓</label> '
                       f'<b>{html.escape(path)}</b> <span class="rv-stat">{stat}</span></summary>')
            if chunk.strip():
                out.append("<pre>")
                out.append(chunk)
                out.append("</pre>")
            elif err:
                out.append(f"*git error — {html.escape(err)}*")
            else:
                out.append("*No diff for this file in the current scope.*")
            out.append("</details>")

    # Categorized commit list (local / unpushed / pushed) with date-time — below the diff summary
    out.append("### Commits\n")
    if dirty:
        out.append("- 🟡 **Local uncommitted changes**")
    for c in commits:
        if not base_ref:
            # No upstream/base found — can't know pushed vs not. Don't lie by calling them "pushed".
            icon, tag = "◦", "no base"
        elif c["sha"] in unpushed_shas:
            icon, tag = "🔵", "unpushed"
        else:
            icon, tag = "⚪", "pushed"
        out.append(f"- {icon} `{c['short']}` {html.escape(c['subj'])} — {tag} · {c['dt']}")
    return "\n".join(out)


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
                    if _is_git_repo(path):
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
        if real not in seen_paths and _is_git_repo(repo_path):
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
    output.append(f"- [Obsidian](#obsidian)")
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


def split_frontmatter(raw):
    """Split leading YAML frontmatter from body.

    Frontmatter is delimited by lines that are EXACTLY '---' (opening on line 1,
    the next such line closing it). Using a line match — not `raw.find('---')` —
    so a horizontal rule or a '---' inside YAML text can't be mistaken for the
    close, which would miscount `fm_lines` and push every checkbox/annotation
    write to the wrong source line.

    Returns (fm_inner, fm_lines, body):
        fm_inner: text between the delimiters ('' if no frontmatter)
        fm_lines: number of leading newlines consumed (delimiters + blank gap)
        body:     the remaining content
    """
    if not raw.startswith("---"):
        return "", 0, raw
    lines = raw.split("\n")
    # A YAML frontmatter delimiter is EXACTLY '---' at column 0 (only a trailing \r on
    # CRLF files is tolerated). Using .strip() would let an INDENTED '---' inside a YAML
    # block scalar close the frontmatter early, pushing later fields into the body.
    def _is_delim(s):
        return s.rstrip("\r") == "---"
    if not _is_delim(lines[0]):
        return "", 0, raw
    close = None
    for i in range(1, len(lines)):
        if _is_delim(lines[i]):
            close = i
            break
    if close is None:
        return "", 0, raw
    fm_inner = "\n".join(lines[1:close]).strip()
    body_lines = lines[close + 1:]
    body = "\n".join(body_lines)
    # Lines consumed = the two delimiter lines + everything between (close index +1),
    # plus any blank lines before real body content (kept out of line-number math).
    fm_lines = close + 1
    stripped = body.lstrip("\n")
    fm_lines += len(body) - len(stripped)
    return fm_inner, fm_lines, stripped


def _render_frontmatter(fm_text):
    """Render YAML frontmatter as compact HTML with clickable wiki links."""
    lines = fm_text.split("\n")
    out = ['<div style="font-size:12px;color:#000;border-bottom:1px solid #000;padding:4px 8px;margin-bottom:8px;">']
    # Only show fields that have wiki links or useful values
    link_fields = {"parent", "child", "sibling", "previous", "next", "concept", "opposite"}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Check for wiki links in this line
        if "[[" in stripped:
            # Extract field name and render wiki links
            def _fm_wikilink(m):
                note = m.group(1)
                note_unesc = html.unescape(note)
                display = note_unesc.split("|")[-1] if "|" in note_unesc else note_unesc
                link_target = note_unesc.split("|")[0]
                enc = urllib.parse.quote(link_target)
                return f'<a href="/?action=open&amp;note={enc}" class="wikilink">[[{html.escape(display)}]]</a>'
            rendered = re.sub(r'\[\[([^\]]+)\]\]', _fm_wikilink, html.escape(stripped))
            out.append(f'{rendered}<br>')
        elif ":" in stripped:
            key = stripped.split(":")[0].strip()
            value = stripped.split(":", 1)[1].strip()
            if key in link_fields and not value:
                continue  # Skip empty link fields
            if value and key not in ("aliases", "tags") and not stripped.startswith("- "):
                out.append(f'<span style="color:#333;">{html.escape(key)}</span>: {html.escape(value)}<br>')
    out.append('</div>')
    # Don't render if nothing useful
    if len(out) <= 2:
        return ""
    return "\n".join(out)


def markdown_to_html(text, file_path=None, line_offset=0, is_diff=False):
    """Convert markdown to HTML. No JS needed.

    Args:
        text: Markdown content (frontmatter already stripped).
        file_path: Vault-relative path for checkbox toggle links.
        line_offset: Lines stripped before this text (frontmatter) so
            action URLs reference correct line numbers in the original file.

    Returns:
        html_string
    """
    text = strip_obsidian_dynamic(text)
    lines = text.split("\n")
    output = []
    in_code_block = False
    fence_len = 0  # backtick count of the OPEN fence; a block closes only on a fence >= this
    # tag_stack interleaves open lists and their open items, e.g. ['ul','li','ul','li'],
    # so a child list can be emitted INSIDE its parent <li> (valid HTML) rather than as a
    # sibling <ul> of a closed <li> (invalid — Kindle mis-indents it). The parent <li> stays
    # open until the child list closes; that's the whole point of deferring </li>.
    tag_stack = []
    # Whitespace WIDTH at each currently-open list level (index i = level i's indent). A new
    # line's indent is compared against this stack to decide same-level / deeper / shallower —
    # so repeated 4-space children stay SIBLINGS, and the vault's mix of 2- and 4-space indent
    # both nest correctly (a lossy indent/2 heuristic over-nested the second same-indent line).
    indent_widths = []
    diff_old_ln = diff_new_ln = 0  # diff gutter line numbers (set from @@ hunk headers)

    def _indent_width(whitespace):
        """Whitespace width in columns — tabs expand to 4 so tab- and space-indented
        lists compare consistently."""
        return len(whitespace.replace("\t", "    "))

    def _close_all_lists():
        """Close every open item and list, innermost first (valid nesting order)."""
        while tag_stack:
            tag = tag_stack.pop()
            output.append(f"</{tag}>")
        indent_widths.clear()

    def _emit_item(kind, width, li_open_html):
        """Append an OPEN <li ...>content (no </li> — deferred until a sibling/shallower
        line or EOF closes it), nesting per the indent-width stack so a child list lives
        INSIDE its parent <li> (valid HTML) and same-indent lines are siblings."""
        # Shallower or equal: pop deeper levels (close their <li> then the list).
        while indent_widths and width < indent_widths[-1]:
            if tag_stack and tag_stack[-1] == "li":
                output.append("</li>"); tag_stack.pop()
            output.append(f"</{tag_stack.pop()}>")
            indent_widths.pop()
        if indent_widths and width == indent_widths[-1]:
            # Same level — close the prior sibling <li> before opening this one.
            if tag_stack and tag_stack[-1] == "li":
                output.append("</li>"); tag_stack.pop()
            # ul<->ol switch at this level: swap the list kind in place.
            if tag_stack and tag_stack[-1] in ("ul", "ol") and tag_stack[-1] != kind:
                output.append(f"</{tag_stack.pop()}>")
                output.append(f"<{kind}>"); tag_stack.append(kind)
        else:
            # Deeper (or first list): open a new list INSIDE the currently open <li>.
            output.append(f"<{kind}>"); tag_stack.append(kind)
            indent_widths.append(width)
        output.append(li_open_html)
        tag_stack.append("li")

    for line_idx, line in enumerate(lines, 1):
        line_num = line_idx + line_offset
        pre_len = len(output)

        stripped = line.strip()
        # In diff mode, code blocks are delimited by the exact <pre>/</pre> sentinels
        # injected upstream; a ``` here is diff CONTENT and must be escaped, not toggled.
        fence_match = (
            re.match(r'^(`{3,})', stripped)
            if (not is_diff and stripped.startswith("```"))
            else None
        )
        if fence_match:
            ticks = len(fence_match.group(1))
            if in_code_block:
                # A closing fence must be at least as long as the opener AND carry no
                # info string (per CommonMark). A shorter or info-bearing ``` line is
                # CONTENT — e.g. an inner ```ruby inside a ````` review wrapper — and
                # falls through to be escaped in the in_code_block branch below.
                if ticks >= fence_len and stripped == "`" * ticks:
                    output.append("</pre>")
                    in_code_block = False
                    fence_len = 0
                    continue
            else:
                output.append("<pre>")
                in_code_block = True
                fence_len = ticks
                continue
        # Handle raw <pre>/</pre> sentinels from diff view. EXACT match (not .strip()):
        # a real unchanged diff line " </pre>" would else prematurely close the block.
        if is_diff and line == "<pre>":
            output.append("<pre>")
            in_code_block = True
            diff_old_ln = diff_new_ln = 0  # reset gutter line numbers per block
            continue
        if is_diff and line == "</pre>":
            output.append("</pre>")
            in_code_block = False
            continue

        if in_code_block:
            escaped = html.escape(line)
            # Make wiki links clickable even in code blocks
            def _code_wikilink(m):
                ref = m.group(1)
                if "|" in ref:
                    target, display = ref.split("|", 1)
                else:
                    target = display = ref
                enc = urllib.parse.quote(target)
                return f'<a href="/?action=open&amp;note={enc}" class="wikilink">[[{html.escape(display)}]]</a>'
            escaped = re.sub(r'\[\[([^\]]+)\]\]', _code_wikilink, escaped)
            # WORD-DIFF markers (git --word-diff=plain): [-removed-]{+added+} inline. Render
            # removed as strikethrough (NOT color-only — e-ink maps red/green to near-identical
            # grays, per UX research), added as bold+underline. So shared text shows once and
            # only the changed WORDS are marked — the "see just the diff part" ask.
            has_word_markers = ("[-" in escaped and "-]" in escaped) or ("{+" in escaped and "+}" in escaped)
            if is_diff and has_word_markers:
                wd = re.sub(r'\[-(.*?)-\]', r'<span class="wd-del">\1</span>', escaped)
                wd = re.sub(r'\{\+(.*?)\+\}', r'<span class="wd-add">\1</span>', wd)
                # A word-diff changed line stands for one old + one new line, so it needs a
                # gutter and must advance BOTH counters — otherwise every line number after
                # the first modified line is off (verified: context after stayed at old no.).
                gut = f'<span class="diff-gut">{diff_new_ln}</span>' if (diff_old_ln or diff_new_ln) else ""
                if diff_old_ln:
                    diff_old_ln += 1
                if diff_new_ln:
                    diff_new_ln += 1
                rendered = f'<span class="diff-ctx">{gut}{wd}</span>'
                output.append(rendered + "\x00NONL")
                continue
            if is_diff:
                # GitHub-Desktop-style row: [gutter: old/new line no + sign] + full-row bg color.
                # Rows are display:block (own line break). They must be emitted with NO trailing
                # newline — a "\n" inside <pre> renders as an EXTRA blank line on top of the block
                # break, which is the "white line between every code line" issue. So we join
                # diff rows with "" (below), and the block display provides the single line break.
                m_hunk = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if m_hunk:
                    diff_old_ln = int(m_hunk.group(1))
                    diff_new_ln = int(m_hunk.group(2))
                    rendered = f'<span class="diff-hunk">{escaped}</span>'
                elif line.startswith("+") and not line.startswith("+++"):
                    gut = f'<span class="diff-gut">{diff_new_ln}</span>'
                    rendered = f'<span class="diff-add">{gut}+{escaped[1:]}</span>'
                    diff_new_ln += 1
                elif line.startswith("-") and not line.startswith("---"):
                    gut = f'<span class="diff-gut">{diff_old_ln}</span>'
                    rendered = f'<span class="diff-del">{gut}-{escaped[1:]}</span>'
                    diff_old_ln += 1
                elif line.startswith(("diff --git", "index ", "+++", "---")):
                    rendered = f'<span class="diff-hunk">{escaped}</span>'  # file header
                elif diff_old_ln or diff_new_ln:
                    # unchanged context line — advances both counters
                    gut = f'<span class="diff-gut">{diff_new_ln}</span>'
                    body = escaped[1:] if escaped.startswith(" ") else escaped
                    rendered = f'<span class="diff-ctx">{gut} {body}</span>'
                    diff_old_ln += 1
                    diff_new_ln += 1
                else:
                    rendered = escaped
                # Pen button for annotating a changed line (skip pure headers/context markers).
                if file_path and line.strip() and (line.startswith(("+", "-")) and not line.startswith(("+++", "---"))):
                    enc_file = urllib.parse.quote(file_path)
                    pen_url = f"/?action=annotate&amp;file={enc_file}&amp;line={line_num}"
                    rendered = f'{rendered}<a href="{pen_url}" class="line-pen" aria-label="Annotate this line">[+]</a>'
                # Emit diff rows with a trailing marker so the final join strips the newline
                # between them (block spans already break lines). Non-diff/plain lines keep \n.
                output.append(rendered + "\x00NONL")
                continue
            else:
                rendered = escaped
            # Add [+] pen button for non-empty code lines
            if file_path and line.strip():
                enc_file = urllib.parse.quote(file_path)
                pen_url = f"/?action=annotate&amp;file={enc_file}&amp;line={line_num}"
                rendered = f'{rendered}<a href="{pen_url}" class="line-pen" aria-label="Annotate this line">[+]</a>'
            output.append(rendered)
            continue

        if tag_stack and not re.match(r'^(\s*[-*+]|\s*\d+\.)\s', line) and line.strip():
            _close_all_lists()

        if line.startswith("# "):
            slug = _slug(line[2:])
            output.append(f'<h1 id="{slug}">{process_inline(line[2:])}</h1>')
        elif line.startswith("## "):
            slug = _slug(line[3:])
            output.append(f'<h2 id="{slug}">{process_inline(line[3:])}</h2>')
        elif line.startswith("### "):
            slug = _slug(line[4:])
            output.append(f'<h3 id="{slug}">{process_inline(line[4:])}</h3>')
        elif line.startswith("#### "):
            # h4 CSS exists (left-border label style); without this it fell through to <p>.
            output.append(f'<h4>{process_inline(line[5:])}</h4>')
        elif line.startswith("> "):
            output.append(f"<blockquote>{process_inline(line[2:])}</blockquote>")
        elif re.match(r'^(\s*)- \[x\]\s*(.*)', line):
            m = re.match(r'^(\s*)- \[x\]\s*(.*)', line)
            indent = _indent_width(m.group(1))
            anchor_id = f"ln{line_num}"
            if file_path:
                enc_file = urllib.parse.quote(file_path)
                toggle_url = f"/?action=toggle&amp;file={enc_file}&amp;line={line_num}&amp;anchor={anchor_id}"
                checkbox_html = f'<a href="{toggle_url}" class="checkbox toggle">[x]</a>'
            else:
                checkbox_html = '<span class="checkbox">[x]</span>'
            _emit_item("ul", indent, f'<li id="{anchor_id}" class="done">{checkbox_html} {process_inline(m.group(2))}')
        elif re.match(r'^(\s*)- \[ \]\s*(.*)', line):
            m = re.match(r'^(\s*)- \[ \]\s*(.*)', line)
            indent = _indent_width(m.group(1))
            anchor_id = f"ln{line_num}"
            if file_path:
                enc_file = urllib.parse.quote(file_path)
                toggle_url = f"/?action=toggle&amp;file={enc_file}&amp;line={line_num}&amp;anchor={anchor_id}"
                checkbox_html = f'<a href="{toggle_url}" class="checkbox toggle">[ ]</a>'
            else:
                checkbox_html = '<span class="checkbox">[ ]</span>'
            _emit_item("ul", indent, f'<li id="{anchor_id}">{checkbox_html} {process_inline(m.group(2))}')
        elif re.match(r'^(\s*)[-*+]\s+(.*)', line):
            m = re.match(r'^(\s*)[-*+]\s+(.*)', line)
            indent = _indent_width(m.group(1))
            _emit_item("ul", indent, f"<li>{process_inline(m.group(2))}")
        elif re.match(r'^(\s*)\d+\.\s+(.*)', line):
            m = re.match(r'^(\s*)\d+\.\s+(.*)', line)
            indent = _indent_width(m.group(1))
            _emit_item("ol", indent, f"<li>{process_inline(m.group(2))}")
        elif re.match(r'^</?(?:h[1-6]|div|p|ul|ol|li|hr|blockquote|pre|table|tr|td|th|details|summary|nav|section|label|input|form|button|a|span)[ >/]', line):
            if is_diff:
                # Trust HTML from diff view (internally generated) — includes v3 review-UI
                # tags (details/summary fold, sticky header divs, viewed-checkbox form/input).
                output.append(line)
            else:
                # Escape raw HTML to prevent XSS in user content
                output.append(html.escape(line))
        elif re.match(r'^---+$', line):
            output.append("<hr>")
        elif not line.strip():
            _close_all_lists()
            output.append("")
        else:
            output.append(f"<p>{process_inline(line)}</p>")

        # Add [+] pen button inside each non-empty line element — tap to annotate
        if file_path and line.strip() and len(output) > pre_len:
            last_idx = len(output) - 1
            enc_file = urllib.parse.quote(file_path)
            pen_url = f"/?action=annotate&amp;file={enc_file}&amp;line={line_num}"
            pen_btn = f'<a href="{pen_url}" class="line-pen" aria-label="Annotate this line">[+]</a>'
            # Insert pen before closing tag so it stays inline
            last_out = output[last_idx]
            # Match closing tags like </p>, </li>, </h1>, </h2>, </h3>, </blockquote>
            close_match = re.search(r'(</(?:p|li|h[1-3]|blockquote)>)$', last_out)
            if close_match:
                insert_pos = close_match.start()
                output[last_idx] = last_out[:insert_pos] + pen_btn + last_out[insert_pos:]
            else:
                output[last_idx] = last_out + pen_btn

    _close_all_lists()
    if in_code_block:
        output.append("</pre>")

    # Diff rows are marked with \x00NONL so the newline the join would add AFTER them is
    # removed — block-display rows already break the line, and an extra \n inside <pre>
    # rendered as a blank line between every row (the "white line" issue).
    joined = "\n".join(output)
    joined = joined.replace("\x00NONL\n", "").replace("\x00NONL", "")
    return joined


def process_inline(text):
    """Process inline markdown (bold, italic, code, links, wiki links, images)."""
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Images BEFORE links — ![alt](url) contains a [alt](url) the link rule would eat.
    # alt text is REQUIRED for accessibility (screen readers, and e-ink when the image
    # can't load). Only http(s) sources render as <img>; anything else stays as alt text.
    def _img_replace(m):
        alt, src = m.group(1), m.group(2)
        src_raw = html.unescape(src)
        if not re.match(r'^https?://', src_raw):
            return alt  # local/vault images aren't served — show alt (already escaped)
        return f'<img src="{src}" alt="{alt}" loading="lazy">'
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _img_replace, text)
    def _wikilink_replace(m):
        note = m.group(1)
        # Unescape HTML entities since html.escape() ran before this regex
        note_unesc = html.unescape(note)
        display = note_unesc.split("|")[-1] if "|" in note_unesc else note_unesc
        link_target = note_unesc.split("|")[0]
        enc = urllib.parse.quote(link_target)
        return f'<a href="/?action=open&amp;note={enc}" class="wikilink">[[{html.escape(display)}]]</a>'
    text = re.sub(r'\[\[([^\]]+)\]\]', _wikilink_replace, text)
    def _safe_link(m):
        label, url = m.group(1), m.group(2)
        url_raw = html.unescape(url)
        # Allow external schemes, anchors, AND internal relative KDV links (/?...)
        # — the review UI links are relative, and were previously stripped to plain text.
        if not re.match(r'^(https?://|obsidian://|#|/)', url_raw):
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


def _send_to_kindle(filepath):
    """Convert markdown to PDF via pandoc and email to Kindle."""
    import traceback
    try:
        return _send_to_kindle_impl(filepath)
    except Exception:
        with open("/tmp/kdv-kindle-error.log", "a") as f:
            f.write(f"\n--- {time.strftime('%H:%M:%S')} ---\n")
            traceback.print_exc(file=f)


def _send_to_kindle_impl(filepath):
    """Implementation of send-to-kindle."""
    import tempfile
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    # Ensure PATH includes common tool locations (pandoc, weasyprint)
    env = os.environ.copy()
    extra_paths = ["/opt/homebrew/bin", str(Path.home() / ".local/bin"), "/usr/local/bin"]
    env["PATH"] = ":".join(extra_paths) + ":" + env.get("PATH", "")
    env["OBSIDIAN_VAULT"] = VAULT
    if PDF_AUTHOR:
        env["PDF_AUTHOR"] = PDF_AUTHOR
    basename = os.path.basename(filepath).replace(".md", "")
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, f"{basename}.pdf")
        # Use bundled obsidian-to-pdf.sh for proper formatting
        script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
        pdf_script = os.path.join(script_dir, "obsidian-to-pdf.sh")
        ret = subprocess.run(
            [pdf_script, filepath, pdf_path],
            capture_output=True, env=env
        )
        if ret.returncode != 0 or not os.path.exists(pdf_path):
            return
        # Email the PDF
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = KINDLE_EMAIL
        msg["Subject"] = f"{basename} — KDV"
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", f"{basename}.pdf"))
        msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)


def _safe_vault_path(file_param, root=None):
    """Resolve file_param to an absolute path inside root (default VAULT).
    Returns None if path escapes root, targets hidden dirs, or isn't a .md file."""
    root = root if root is not None else VAULT
    root_real = os.path.realpath(root)
    filepath = os.path.realpath(os.path.join(root, file_param))
    if not filepath.startswith(root_real + os.sep) and filepath != root_real:
        return None
    # Block hidden directories (.git, .obsidian, etc.)
    rel = os.path.relpath(filepath, root_real)
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

    def _handle_annotate_form(self, query):
        """Show annotation form: quoted line + text input for comment."""
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
        quoted_line = lines[line_num - 1].strip()
        # Strip markdown syntax for clean display
        display_line = re.sub(r'^[-*+]\s+(\[.\]\s*)?', '', quoted_line)
        display_line = re.sub(r'^#{1,6}\s+', '', display_line)
        enc_file = html.escape(file_param)
        enc_line = html.escape(quoted_line)
        form_html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Annotate — KDV</title>
<style>
body {{ margin: 20px; background: #fff; color: #000; font-family: Georgia, serif; font-size: 15px; }}
blockquote {{ border-left: 3px solid #000; margin: 8px 0; padding: 4px 12px; color: #000; font-size: 13px; }}
textarea {{ width: 100%; height: 80px; font-size: 15px; padding: 8px; margin: 8px 0; }}
button {{ font-size: 16px; padding: 8px 16px; background: #eee; border: 2px solid #333; color: #000; margin-right: 8px; }}
</style>
</head><body>
<h1>Annotate</h1>
<blockquote>{html.escape(display_line)}</blockquote>
<form method="POST" action="/?action=annotate_submit" style="display:inline;">
<input type="hidden" name="file" value="{enc_file}">
<input type="hidden" name="quoted" value="{enc_line}">
<label for="comment">Your comment</label>
<textarea id="comment" name="comment" placeholder="Your comment (optional)" style="display:block;width:100%;height:80px;font-size:15px;padding:8px;margin:8px 0;"></textarea>
<button type="submit">Save</button></form> <form method="GET" action="/" style="display:inline;"><input type="hidden" name="file" value="{urllib.parse.quote(file_param)}"><input type="hidden" name="scrolled" value="1"><button type="submit">Cancel</button></form>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(form_html.encode())

    def _handle_annotate_submit(self, params):
        """Append highlight + comment as single-line checkbox under # Z section at file end."""
        file_param = params.get("file", [""])[0]
        quoted = params.get("quoted", [""])[0]
        comment = params.get("comment", [""])[0].strip()
        if not file_param or not quoted:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        filepath = _safe_vault_path(file_param)
        if not filepath or not os.path.exists(filepath):
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        today_str = date.today().strftime("%Y-%m-%d")
        time_str = time.strftime("%H:%M")
        # Single-line format: - [ ] [[YYYY-MM-DD]] HH:MM "highlight" - comment
        entry = f'- [ ] [[{today_str}]] {time_str} "{quoted}"'
        if comment:
            entry += f" - {comment}"
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        # Find existing # Z section
        z_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "# Z":
                z_idx = i
                break
        if z_idx is not None:
            # Append at end of file (after # Z, all entries go below)
            lines.append(entry)
        else:
            # Create # Z section at end of file
            lines.append("")
            lines.append("# Z")
            lines.append("")
            lines.append(entry)
        import tempfile
        dir_ = os.path.dirname(filepath)
        new_content = "\n".join(lines)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_, delete=False) as tmp:
            tmp.write(new_content)
            tmp_path = tmp.name
        os.replace(tmp_path, filepath)
        self.send_response(302)
        self.send_header("Location", f"/?file={urllib.parse.quote(file_param)}&scrolled=1#z")
        self.end_headers()

    def _handle_kindle(self, query):
        """Show confirmation page before sending file as PDF to Kindle."""
        file_param = query.get("file", [None])[0]
        if not file_param:
            self._redirect_back(query)
            return
        filepath = _safe_vault_path(file_param)
        if not filepath or not os.path.exists(filepath):
            self._redirect_back(query)
            return
        display_name = os.path.basename(file_param).replace(".md", "")
        enc_file = urllib.parse.quote(file_param)
        back_url = f"/?file={enc_file}&scrolled=1"
        missing_config = not KINDLE_EMAIL or not SMTP_USER or not SMTP_PASSWORD
        if missing_config:
            action_html = '<p style="color:#900;margin:12px 0;">Configure KDV_KINDLE_EMAIL, KDV_SMTP_USER, KDV_SMTP_PASSWORD in config.env</p>'
            send_btn = ""
        else:
            action_html = ""
            send_btn = f'<form method="GET" action="/" style="display:inline;"><input type="hidden" name="action" value="kindle_send"><input type="hidden" name="file" value="{html.escape(file_param)}"><button type="submit" class="btn">Send</button></form>'
        cancel_btn = f'<form method="GET" action="/" style="display:inline;"><input type="hidden" name="file" value="{html.escape(file_param)}"><input type="hidden" name="scrolled" value="1"><button type="submit" class="btn">Cancel</button></form>'
        confirm_html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Send to Kindle — KDV</title>
<style>
body {{ margin: 20px; background: #fff; color: #000; font-family: Georgia, serif; font-size: 15px; }}
.filename {{ font-size: 18px; font-weight: bold; margin: 16px 0; padding: 12px; border: 1px solid #ccc; background: #f9f9f9; }}
.btns {{ margin-top: 20px; }}
.btn {{ font-size: 16px; padding: 10px 20px; margin-right: 12px; background: #eee; border: 2px solid #333; color: #000; }}
</style>
</head><body>
<h1>Send to Kindle</h1>
<div class="filename">{html.escape(display_name)}</div>
{action_html}
<div class="btns">
{send_btn}
{cancel_btn}
</div>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(confirm_html.encode())

    def _handle_kindle_send(self, query):
        """Actually send the file as PDF to Kindle after confirmation."""
        file_param = query.get("file", [None])[0]
        if not file_param:
            self._redirect_back(query)
            return
        filepath = _safe_vault_path(file_param)
        if not filepath or not os.path.exists(filepath):
            self._redirect_back(query)
            return
        if not KINDLE_EMAIL or not SMTP_USER or not SMTP_PASSWORD:
            self._redirect_back(query)
            return
        # Convert and send in background thread
        threading.Thread(
            target=_send_to_kindle, args=(filepath,), daemon=True
        ).start()
        # Redirect back to the file view
        self.send_response(302)
        self.send_header("Location", f"/?file={urllib.parse.quote(file_param)}&scrolled=1")
        self.end_headers()

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
        # Redirect back to the same file at the same scroll position
        enc_file = urllib.parse.quote(file_param)
        anchor = query.get("anchor", [None])[0]
        location = f"/?file={enc_file}&scrolled=1"
        if anchor:
            anchor = re.sub(r'[\r\n]', '', anchor)
            location += f"#{anchor}"
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _handle_open(self, query):
        """Open a wiki-linked note: show in viewer + open in Obsidian on Mac."""
        note_ref = query.get("note", [None])[0]
        if not note_ref:
            self._redirect_back(query)
            return
        # Rebuild index to catch newly created files
        _build_vault_index()
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
        # Small delay so Obsidian updates workspace.json before we read it for active file button
        time.sleep(0.3)
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

        # Route POST actions
        parsed_url = urllib.parse.urlparse(self.path)
        post_query = urllib.parse.parse_qs(parsed_url.query)
        post_action = post_query.get("action", [None])[0]
        if post_action == "annotate_submit" and self.check_auth():
            self._handle_annotate_submit(params)
            return

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
        elif action == "annotate":
            self._handle_annotate_form(query)
            return
        elif action == "kindle":
            self._handle_kindle(query)
            return
        elif action == "kindle_send":
            self._handle_kindle_send(query)
            return

        try:
            offset = int(query.get("day", ["0"])[0])
        except (ValueError, TypeError):
            offset = 0
        file_param = query.get("file", [None])[0]
        file_param_from_url = file_param  # preserve original before auto-fill
        view = query.get("view", [None])[0]
        has_anchor = "scrolled" in query

        raw_for_nav = ""  # raw text for N button anchor calculation
        if view == "diff":
            content = markdown_to_html(get_diff_view(), is_diff=True)
            page_label = "Git Diff"
        elif view == "review":
            repo_param = query.get("repo", [None])[0]
            scope = query.get("scope", [None])[0]
            file_only = query.get("file", [None])[0] if repo_param else None
            ctx_lines = query.get("ctx", [None])[0]
            word_diff = query.get("words", ["0"])[0] == "1"
            review_md = get_review_view(repo_param, scope=scope, file_only=file_only, ctx_lines=ctx_lines, word_diff=word_diff)
            content = markdown_to_html(review_md, is_diff=True)
            page_label = "Review"
        else:
            if file_param and "/" in file_param:
                filepath = _safe_vault_path(file_param)
                if not filepath:
                    self.send_response(403)
                    self.end_headers()
                    return
                page_label = os.path.basename(file_param).replace(".md", "")
            elif file_param:
                # Bare name = a daily/period note under DAILY_DIR. Enforce the SAME policy as
                # _safe_vault_path (stay inside the dir, .md only, no hidden parts) so a bare
                # param can't read a non-.md or dotfile, or escape via '..'.
                filepath = _safe_vault_path(file_param, root=DAILY_DIR)
                if not filepath:
                    self.send_response(403)
                    self.end_headers()
                    return
                page_label = file_param.replace(".md", "")
            else:
                filepath, target_date = get_daily_file(offset)
                page_label = target_date.strftime("%Y-%m-%d")
                file_param = os.path.relpath(filepath, VAULT)

            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    raw = f.read()
                fm_html = ""
                fm_inner, fm_lines, raw = split_frontmatter(raw)
                if fm_inner:
                    # Render frontmatter with clickable wiki links
                    fm_html = _render_frontmatter(fm_inner)
                # Auto-jump to today/next-actions page on first load
                # Auto-scroll to today section or Next Actions on first load
                today_str = date.today().strftime("%Y-%m-%d")
                if not has_anchor:
                    scroll_anchor = _scroll_anchor(raw, today_str, default="")
                    if scroll_anchor:
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
                raw_for_nav = raw
                content = markdown_to_html(raw, file_path=rel_path, line_offset=fm_lines)
                if fm_html:
                    content = fm_html + content
                # Append the daily feed below the note, if one exists for this date.
                feed_path = os.path.join(FEED_DIR, f"{page_label}_daily-feed.md")
                if os.path.exists(feed_path):
                    with open(feed_path, "r", encoding="utf-8") as ff:
                        feed_raw = ff.read()
                    _, feed_fm_lines, feed_raw = split_frontmatter(feed_raw)
                    feed_rel = os.path.relpath(feed_path, VAULT)
                    feed_html = markdown_to_html(feed_raw, file_path=feed_rel, line_offset=feed_fm_lines)
                    content += '<hr><h2 id="daily-feed">Daily Feed</h2>' + feed_html
            else:
                content = f"<p>No note for {html.escape(page_label)}</p>"

        week_days = get_week_days()
        day_buttons_parts = []
        for name, day_offset, d in week_days:
            day_file = d.strftime("%Y-%m-%d") + ".md"
            is_active = (view != "diff" and (
                (not file_param_from_url and day_offset == offset) or
                (file_param_from_url and file_param_from_url.endswith(day_file))
            ))
            style = 'style="background:#000;color:#fff;font-weight:bold;border:2px solid #000;padding:2px 6px;" aria-current="page"' if is_active else ''
            label = f"{name} {d.day}"
            day_buttons_parts.append(f'<a href="/?day={day_offset}&scrolled=1" {style}>{label}</a>')
        day_buttons = " ".join(day_buttons_parts)

        period_files = get_period_files()
        period_parts = []
        for label, filename in period_files:
            is_active = (file_param == filename)
            style = 'style="background:#000;color:#fff;font-weight:bold;border:2px solid #000;padding:2px 6px;" aria-current="page"' if is_active else ''
            period_parts.append(f'<a href="/?file={filename}&scrolled=1" {style}>{label}</a>')
        period_buttons = " ".join(period_parts)

        is_diff_view = (view == "diff")
        diff_style = 'style="background:#000;color:#fff;font-weight:bold;border:2px solid #000;padding:2px 6px;" aria-current="page"' if is_diff_view else ''
        diff_button = f'<a href="/?view=diff&scrolled=1" {diff_style}>Diff</a>'

        is_review_view = (view == "review")
        review_style = 'style="background:#000;color:#fff;font-weight:bold;border:2px solid #000;padding:2px 6px;" aria-current="page"' if is_review_view else ''
        review_button = f'<a href="/?view=review&scrolled=1" {review_style}>Review</a>'

        active_file = get_obsidian_active_file()
        if active_file:
            active_name = os.path.basename(active_file).replace(".md", "")
            display_name = active_name[:ACTIVE_FILE_MAX_CHARS] + "..." if len(active_name) > ACTIVE_FILE_MAX_CHARS else active_name
            is_showing_active = (file_param == active_file)
            active_style = 'style="background:#000;color:#fff;font-weight:bold;border:2px solid #000;padding:2px 6px;" aria-current="page"' if is_showing_active else ''
            active_button = f'<a href="/?file={urllib.parse.quote(active_file)}&scrolled=1" {active_style}>{html.escape(display_name)}</a>'
        else:
            active_button = ""

        now_ts = int(time.time())
        time_str = time.strftime("%H:%M")

        if file_param:
            enc_file = urllib.parse.quote(file_param)
            kindle_button = f'<form method="GET" action="/" style="display:inline;margin:0;padding:0;"><input type="hidden" name="action" value="kindle"><input type="hidden" name="file" value="{html.escape(file_param)}"><button type="submit" class="pg-btn" aria-label="Send to Kindle">K</button></form>'
            # N button: jump to today/next-actions anchor
            today_str = date.today().strftime("%Y-%m-%d")
            nav_anchor = _scroll_anchor(raw_for_nav, today_str, default="top")
            nav_button = f'<a href="#{nav_anchor}" class="pg-btn" aria-label="Jump to today or next actions">N</a>'
        else:
            kindle_button = ""
            nav_button = ""

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()

        up_url = "#top"
        down_url = "#bottom"

        # Chrome placeholders substituted in ONE non-recursive pass, {content} last —
        # see _render_page for why (kills the chained-replace injection footgun).
        page_title = f"{page_label} — KDV" if page_label else "KDV"
        page = _render_page(
            content,
            page_title=html.escape(page_title),
            timestamp=str(now_ts),
            time_str=time_str,
            diff_button=diff_button,
            review_button=review_button,
            active_button=active_button,
            day_buttons=day_buttons,
            period_buttons=period_buttons,
            up_url=up_url,
            down_url=down_url,
            kindle_button=kindle_button,
            nav_button=nav_button,
        )
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        import sys
        sys.stderr.write(f"[KDV] {format % args}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="kdv",
        description="Kindle Daily Viewer — serve Obsidian notes for e-ink browsers"
    )
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0 for LAN access, use 127.0.0.1 for local-only)")
    args = parser.parse_args()

    import socket
    from http.server import ThreadingHTTPServer
    class ReusableHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        address_family = socket.AF_INET
        daemon_threads = True
    server = ReusableHTTPServer((args.host, args.port), Handler)

    # Best-effort LAN IP for the access banner only. Must never block startup:
    # offline/VPN-down hangs on connect() otherwise, leaving the port bound but
    # serve_forever() never reached. Short timeout fails fast to localhost.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
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
