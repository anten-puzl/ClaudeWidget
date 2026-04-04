# Claude Meter

A lightweight Windows desktop widget that displays your **Claude.ai Pro** usage limits in real time — sitting on your desktop without cluttering the taskbar.

![Claude Meter Widget](screenshot.png)

## Features

- **5-Hour window** — current session usage with countdown to reset
- **7-Day window** — weekly usage with countdown to reset
- **Per-model breakdown** — Sonnet and Opus usage (when available)
- **Extra Usage balance** — shows how much of your monthly overage budget has been spent (e.g. `€0.00 / €17.00`)
- Always-on-desktop (stays behind other windows, hidden from taskbar)
- Draggable, remembers position between restarts
- Data cached locally — shows last known values instantly on startup

## Requirements

- Python 3.8+
- `tkinter` (included with standard Python on Windows)
- Windows OS (uses Win32 API for desktop placement)
- Active **Claude.ai Pro** account with Claude Code logged in

## Installation

```bash
git clone https://github.com/your-username/claude-meter.git
cd claude-meter
```

No additional dependencies — uses only the Python standard library.

## Usage

```bash
python claude_meter.py
```

The widget starts minimized to the desktop. Drag it by the header bar to reposition. Click `×` to close.

> **Note:** The widget waits 2 minutes before starting (to allow system boot to complete). To skip the delay during development, remove or reduce the `time.sleep(120)` line at the bottom of the file.

## How It Works

1. Reads your OAuth token from `~/.claude/.credentials.json` (written by Claude Code CLI)
2. Automatically refreshes the token if expired
3. Queries two APIs using your token:
   - `api.anthropic.com/api/oauth/usage` — 5h and 7-day window data
   - `claude.ai/api/organizations/{org_id}/usage` — extra usage balance
4. Displays data in the widget and refreshes every **10 minutes**
5. Caches the last result to `~/.claude/meter_cache.json`

No credentials are stored by this app — the token is read directly from Claude Code's own credentials file.

## Files

| File | Description |
|------|-------------|
| `claude_meter.py` | Main widget application |
| `~/.claude/meter_cache.json` | Last fetched data (auto-created) |
| `~/.claude/meter_pos.json` | Widget position (auto-created) |
| `~/.claude/claude_meter.log` | Debug log (auto-created) |

## License

MIT
