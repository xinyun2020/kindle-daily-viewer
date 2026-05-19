# Kindle Daily Viewer

Serve your Obsidian vault as plain HTML for e-ink browsers. No JavaScript, no external dependencies — just Python 3 stdlib.

Built for Kindle's experimental browser, but works on any e-ink device (Kobo, reMarkable) or regular browser.

## Features

- Daily note viewer with day/week/month/quarter/year navigation
- Git diff view (changed files, unpushed commits)
- Checkbox toggle — tap to flip `- [ ]` / `- [x]` in the actual markdown file
- Wiki link navigation — tap opens note in viewer + activates in Obsidian
- Obsidian active file button (reads workspace.json)
- Annotation — tap [+] on any line to highlight + comment (appends to file)
- Send to Kindle — one tap converts current file to PDF and emails to your Kindle
- Navigation buttons — N (jump to today/next-actions), up/down (top/bottom of page)
- Password auth with SHA256 cookie
- E-ink optimized — pure black/white, single-tap everything, large touch targets, no JS
- GitHub Desktop integration — auto-detects active repo (including worktrees) in diff view
- Strips dataview/dataviewjs blocks automatically

## Install

```bash
brew tap xinyun2020/tap
brew install kindle-daily-viewer
```

## Setup

```bash
# Create config
mkdir -p ~/.config/kdv
cp $(brew --prefix)/share/kdv/config.env.example ~/.config/kdv/config.env

# Edit — at minimum set KDV_VAULT
vim ~/.config/kdv/config.env
```

## Run

```bash
kdv
```

Then open `http://<your-mac-ip>:8080/` on your Kindle browser. Default password: `password`.

## Survive reboot

KDV stops when you close the terminal. To keep it running after reboot, set up a LaunchAgent (macOS auto-starts it on login):

```bash
# Option A: Homebrew services (simplest)
brew services start kindle-daily-viewer

# Option B: LaunchAgent (more control)
cp $(brew --prefix)/share/kdv/com.kdv.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kdv.plist
```

Verify it's running after reboot:

```bash
curl -s http://localhost:8080/ | head -1
```

## Config

All settings via `~/.config/kdv/config.env` or environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `KDV_VAULT` | yes | — | Path to Obsidian vault |
| `KDV_DAILY_DIR` | no | `$VAULT/log` | Daily notes directory |
| `KDV_PORT` | no | `8080` | Server port |
| `KDV_PASSWORD` | no | `password` | Login password |
| `KDV_FEED_DIR` | no | `$VAULT/feed` | Daily feed files |
| `KDV_WORKTREE_DIR` | no | — | Git worktree dir for diff view |
| `KDV_EXTRA_REPOS` | no | — | Comma-separated repo paths for diff view |
| `KDV_GITHUB_DESKTOP` | no | `false` | Auto-detect GitHub Desktop repos in diff view |
| `KDV_DIFF_TRUNCATE` | no | `50000` | Max diff bytes before truncation |
| `KDV_KINDLE_EMAIL` | no | — | Your Kindle email (for send-to-kindle) |
| `KDV_SMTP_USER` | no | — | Gmail address for sending |
| `KDV_SMTP_PASSWORD` | no | — | Gmail app password (supports `${VAR}` from ~/.env) |
| `KDV_PDF_AUTHOR` | no | — | Author name in PDF metadata |

## Send to Kindle

Requires `pandoc` and `weasyprint` for PDF conversion:

```bash
brew install pandoc
pip install weasyprint
```

Configure your Kindle email and Gmail app password in `config.env`. Get an app password at https://myaccount.google.com/apppasswords.

## Security model

KDV is a local-only server. It binds to your machine, serves your vault over your home network, and never contacts the internet. No accounts, no cloud, no telemetry, no external dependencies — your notes stay on your disk. Trust is local: anyone on your network with the password can read your vault, so use it on networks you trust.

## How it works

```mermaid
sequenceDiagram
    participant K as Kindle / e-ink browser
    participant S as KDV server (Python 3)
    participant V as Obsidian vault (.md files)
    K->>S: GET /today
    S->>V: read daily note from disk
    V-->>S: markdown content
    S-->>K: plain HTML (no JavaScript)
    K->>S: GET /?action=toggle (tap checkbox)
    S->>V: write back - [ ] → - [x]
    V-->>S: saved
    S-->>K: redirect (updated page)
```

One Python file, zero dependencies. The server reads your vault, converts markdown to HTML, and serves it. Tap a checkbox on Kindle and it flips `- [ ]` / `- [x]` in the actual markdown file. Wiki links resolve note names by walking the vault directory.

## License

MIT
