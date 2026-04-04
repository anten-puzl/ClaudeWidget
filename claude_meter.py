"""
Claude Meter - Desktop widget showing Claude Code Pro usage limits.
Reads OAuth token from ~/.claude/.credentials.json and queries Anthropic API.
"""

import ctypes
import json
import logging
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import sys
import traceback

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FILE = Path.home() / ".claude" / "claude_meter.log"
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
API_URL          = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_AI_BASE   = "https://claude.ai/api"
API_BETA_HEADER  = "oauth-2025-04-20"
REFRESH_MS       = 10 * 60 * 1000   # 10 minutes
POLL_MS          = 500
WIDGET_W         = 280
WIDGET_H         = 310
POS_FILE         = Path.home() / ".claude" / "meter_pos.json"
CACHE_FILE       = Path.home() / ".claude" / "meter_cache.json"

# Colors
BG       = "#1a1a2e"
HEADER   = "#0f3460"
FG       = "#e0e0e0"
FG_DIM   = "#888899"
FG_LABEL = "#aaaacc"
BAR_BG   = "#2a2a4a"
BAR_OK   = "#4caf50"
BAR_WARN = "#f5a623"
BAR_CRIT = "#e94560"


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

@dataclass
class UsageData:
    five_hour_pct: float
    five_hour_resets_at: datetime
    seven_day_pct: float
    seven_day_resets_at: datetime
    sonnet_pct: Optional[float]
    opus_pct: Optional[float]
    last_updated: datetime
    extra_enabled: bool = False
    extra_limit_cents: int = 0
    extra_used_cents: float = 0.0


class FetchError(Exception):
    pass


OAUTH_TOKEN_URL = "https://claude.ai/api/auth/oauth/token"


class UsageFetcher:
    def __init__(self):
        self._org_id: Optional[str] = None

    def _claude_ai_get(self, path: str, token: str) -> dict:
        req = urllib.request.Request(
            f"{CLAUDE_AI_BASE}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "claude-meter-widget/1.0.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _get_org_id(self, token: str) -> str:
        if self._org_id:
            return self._org_id
        orgs = self._claude_ai_get("/organizations", token)
        self._org_id = orgs[0]["id"]
        log.debug("Resolved org_id=%s", self._org_id)
        return self._org_id

    def _fetch_extra_usage(self, token: str) -> Optional[dict]:
        try:
            org_id = self._get_org_id(token)
            data = self._claude_ai_get(f"/organizations/{org_id}/usage", token)
            return data.get("extra_usage")
        except Exception as exc:
            log.warning("Could not fetch extra_usage: %s", exc)
            return None

    def get_token(self) -> str:
        try:
            data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FetchError(f"Credentials not found:\n{CREDENTIALS_FILE}")
        except json.JSONDecodeError:
            raise FetchError("Credentials file is not valid JSON")

        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            raise FetchError("accessToken not found in credentials")

        # Check if token is expired and try to refresh it
        expires_at = oauth.get("expiresAt")
        if expires_at:
            try:
                now_ms = datetime.now(timezone.utc).timestamp() * 1000
                if float(expires_at) < now_ms:
                    log.debug("Token expired (expiresAt=%s), attempting refresh...", expires_at)
                    token = self._refresh_token(data, oauth)
            except FetchError:
                raise
            except Exception as exc:
                log.warning("Failed to check/refresh token expiry: %s", exc)

        log.debug("Token found, making API request...")
        return token

    def _refresh_token(self, creds_data: dict, oauth: dict) -> str:
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            raise FetchError("Token expired and no refreshToken available — please re-login to Claude")

        payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise FetchError(f"Token refresh failed ({e.code}) — please re-login to Claude")
        except urllib.error.URLError as e:
            raise FetchError(f"Network error during token refresh: {e.reason}")

        new_access = result.get("access_token")
        if not new_access:
            raise FetchError("Token refresh returned no access_token")

        # Persist the refreshed tokens back to the credentials file
        try:
            oauth["accessToken"] = new_access
            if "refresh_token" in result:
                oauth["refreshToken"] = result["refresh_token"]
            if "expires_in" in result:
                now_ms = datetime.now(timezone.utc).timestamp() * 1000
                oauth["expiresAt"] = int(now_ms + result["expires_in"] * 1000)
            creds_data["claudeAiOauth"] = oauth
            CREDENTIALS_FILE.write_text(
                json.dumps(creds_data, indent=2), encoding="utf-8"
            )
            log.debug("Token refreshed and saved successfully")
        except Exception as exc:
            log.warning("Could not save refreshed token: %s", exc)

        return new_access

    def fetch(self) -> UsageData:
        token = self.get_token()
        req = urllib.request.Request(
            API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": API_BETA_HEADER,
                "Content-Type": "application/json",
                "User-Agent": "claude-meter-widget/1.0.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode())
            now = datetime.now(timezone.utc)
            log.debug("API response received at (UTC): %s", now.isoformat())
            sd = raw.get("seven_day") or {}
            fh = raw.get("five_hour") or {}
            log.debug("five_hour raw: %s", fh)
            log.debug("seven_day raw: %s", sd)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FetchError("Token expired or invalid (401)")
            if e.code == 429:
                raise FetchError("Rate limited by API (429)")
            raise FetchError(f"HTTP error {e.code}")
        except urllib.error.URLError as e:
            raise FetchError(f"Network error: {e.reason}")

        extra_usage = self._fetch_extra_usage(token)
        return self._parse(raw, extra_usage)

    def _parse(self, raw: dict, extra_usage: Optional[dict] = None) -> UsageData:
        def pct(section) -> Optional[float]:
            if section is None:
                return None
            return float(section.get("utilization", 0))

        def resets(section, fallback: timedelta) -> datetime:
            s = section.get("resets_at", "")
            if not isinstance(s, str) or not s:
                log.warning("resets_at missing or not a string: %r, using fallback %s", s, fallback)
                return datetime.now(timezone.utc) + fallback
            try:
                # Strip timezone suffix and parse as naive UTC — compatible with Python 3.6+
                import re
                s_clean = re.sub(r'[Zz]$|[+-]\d{2}:\d{2}$', '', s)
                # Python 3.6 does not have datetime.fromisoformat; use strptime instead
                fmt = "%Y-%m-%dT%H:%M:%S.%f" if '.' in s_clean else "%Y-%m-%dT%H:%M:%S"
                dt = datetime.strptime(s_clean, fmt).replace(tzinfo=timezone.utc)
                log.debug("Parsed resets_at=%r -> %s", s, dt.isoformat())
                return dt
            except Exception as exc:
                log.warning("Failed to parse resets_at=%r (%s), using fallback %s", s, exc, fallback)
                return datetime.now(timezone.utc) + fallback

        fh = raw.get("five_hour") or {}
        sd = raw.get("seven_day") or {}

        eu = extra_usage or {}
        return UsageData(
            five_hour_pct=float(fh.get("utilization", 0)),
            five_hour_resets_at=resets(fh, timedelta(hours=5)),
            seven_day_pct=float(sd.get("utilization", 0)),
            seven_day_resets_at=resets(sd, timedelta(days=7)),
            sonnet_pct=pct(raw.get("seven_day_sonnet")),
            opus_pct=pct(raw.get("seven_day_opus")),
            last_updated=datetime.now(timezone.utc),
            extra_enabled=bool(eu.get("is_enabled", False)),
            extra_limit_cents=int(eu.get("monthly_limit", 0)),
            extra_used_cents=float(eu.get("used_credits", 0.0)),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bar_color(pct: float) -> str:
    if pct >= 80:
        return BAR_CRIT
    if pct >= 50:
        return BAR_WARN
    return BAR_OK


def format_timedelta(dt: datetime) -> tuple:
    """Returns (text, color) for time until reset."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    total_sec = int(delta.total_seconds())

    if total_sec <= 0:
        return "resetting...", BAR_OK

    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    mins = (total_sec % 3600) // 60

    if days > 0:
        text = f"{days}d {hours}h"
        color = FG if days > 1 else BAR_WARN
    elif hours > 0:
        text = f"{hours}h {mins}m"
        color = BAR_OK if hours >= 1 else BAR_WARN
    else:
        text = f"{mins}m"
        color = BAR_CRIT if mins < 15 else BAR_WARN

    return text, color


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class ClaudeMeterWidget:
    def __init__(self):
        self.fetcher = UsageFetcher()
        self.q: queue.Queue = queue.Queue()
        self._drag_x = 0
        self._drag_y = 0
        self.retry_count = 0
        self.max_retries = 3
        self.last_success_time: Optional[datetime] = None
        self._has_error = False

        self.root = tk.Tk()
        self.root.title("Claude Meter")
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)

        x, y = self._load_position()
        self.root.geometry(f"{WIDGET_W}x{WIDGET_H}+{x}+{y}")

        self._build_ui()
        self.root.update_idletasks()
        self._setup_win32()

        # Load cached data so reset times are correct before first fetch
        cached = self._load_cache()
        if cached:
            self._update_ui(cached)

        # Start refresh loop
        self._trigger_fetch()
        self.root.after(POLL_MS, self._poll_queue)
        self.root.after(5000, self._keep_bottom)
        self.root.after(REFRESH_MS, self._schedule_refresh)
        self.root.after(60_000, self._tick_updated)

    # ------------------------------------------------------------------
    # Win32 desktop placement
    # ------------------------------------------------------------------

    def _setup_win32(self):
        hwnd = self.root.winfo_id()
        user32 = ctypes.windll.user32

        HWND_BOTTOM    = 1
        SWP_NOMOVE     = 0x0002
        SWP_NOSIZE     = 0x0001
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

        GWL_EXSTYLE      = -20
        WS_EX_TOOLWINDOW = 0x00000080
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)


    def _push_to_bottom(self, _event=None):
        hwnd = self.root.winfo_id()
        ctypes.windll.user32.SetWindowPos(
            hwnd, 1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010
        )

    def _keep_bottom(self):
        self._push_to_bottom()
        self.root.after(5000, self._keep_bottom)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = self.root

        # --- Header bar (drag handle) ---
        header = tk.Frame(root, bg=HEADER, cursor="fleur")
        header.pack(fill="x")

        tk.Label(header, text="◉ Claude Meter", bg=HEADER, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=8, pady=5)

        close_btn = tk.Label(header, text="×", bg=HEADER, fg=FG_DIM,
                             font=("Segoe UI", 14), cursor="hand2")
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", self._on_close)

        header.bind("<Button-1>",   self._drag_start)
        header.bind("<B1-Motion>",  self._drag_motion)
        header.bind("<ButtonRelease-1>", self._drag_end)
        for child in header.winfo_children():
            if child != close_btn:
                child.bind("<Button-1>",  self._drag_start)
                child.bind("<B1-Motion>", self._drag_motion)
                child.bind("<ButtonRelease-1>", self._drag_end)

        root.bind("<FocusIn>", self._push_to_bottom)

        # --- Body ---
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=6)

        # 5-hour section
        tk.Label(body, text="5-HOUR WINDOW", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")

        self.bar_5h = self._make_bar(body)
        self.lbl_5h_pct = tk.Label(body, text="—", bg=BG, fg=FG,
                                   font=("Segoe UI", 9))
        self.lbl_5h_pct.pack(anchor="e", pady=(0, 1))
        self.lbl_5h_reset = tk.Label(body, text="Reset in: —", bg=BG,
                                     fg=FG_DIM, font=("Segoe UI", 8))
        self.lbl_5h_reset.pack(anchor="w")

        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=5)

        # 7-day section
        tk.Label(body, text="7-DAY WINDOW", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")

        self.bar_7d = self._make_bar(body)
        self.lbl_7d_pct = tk.Label(body, text="—", bg=BG, fg=FG,
                                   font=("Segoe UI", 9))
        self.lbl_7d_pct.pack(anchor="e", pady=(0, 1))
        self.lbl_7d_reset = tk.Label(body, text="Reset in: —", bg=BG,
                                     fg=FG_DIM, font=("Segoe UI", 8))
        self.lbl_7d_reset.pack(anchor="w")

        # Per-model labels
        self.model_frame = tk.Frame(body, bg=BG)
        self.model_frame.pack(fill="x", pady=(4, 0))
        self.lbl_sonnet = tk.Label(self.model_frame, text="", bg=BG,
                                   fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_sonnet.pack(anchor="w")
        self.lbl_opus = tk.Label(self.model_frame, text="", bg=BG,
                                 fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_opus.pack(anchor="w")

        # Extra Usage section
        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=5)
        self.extra_frame = tk.Frame(body, bg=BG)
        self.extra_frame.pack(fill="x")
        tk.Label(self.extra_frame, text="EXTRA USAGE", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")
        self.bar_extra = self._make_bar(self.extra_frame)
        self.lbl_extra = tk.Label(self.extra_frame, text="—", bg=BG,
                                  fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_extra.pack(anchor="w")

        # Footer
        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=(5, 3))
        self.lbl_updated = tk.Label(body, text="Fetching...", bg=BG,
                                    fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_updated.pack(anchor="w")
        self.lbl_last_ok = tk.Label(body, text="", bg=BG,
                                    fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_last_ok.pack(anchor="w")

    def _make_bar(self, parent) -> tk.Canvas:
        bar_width = WIDGET_W - 24
        canvas = tk.Canvas(parent, width=bar_width, height=10,
                           bg=BAR_BG, highlightthickness=0)
        canvas.pack(anchor="w", pady=(2, 0))
        canvas.create_rectangle(0, 0, 0, 10, fill=BAR_OK, outline="",
                                 tags="fill")
        return canvas

    def _update_bar(self, canvas: tk.Canvas, pct: float):
        bar_width = WIDGET_W - 24
        fill_w = int(bar_width * min(pct, 100) / 100)
        color = bar_color(pct)
        canvas.coords("fill", 0, 0, fill_w, 10)
        canvas.itemconfig("fill", fill=color)

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def _trigger_fetch(self):
        threading.Thread(target=self._bg_fetch, daemon=True).start()

    def _bg_fetch(self):
        try:
            data = self.fetcher.fetch()
            self.q.put(("ok", data))
        except FetchError as e:
            error_msg = str(e)
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            self.q.put(("err", error_msg))
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            traceback.print_exc()
            self.q.put(("err", error_msg))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    if kind == "ok":
                        self.retry_count = 0  # Reset retry counter on success
                        self._has_error = False
                        self._update_ui(payload)
                        self._save_cache(payload)
                    else:
                        self._has_error = True
                        log.warning("Fetch error: %s", payload)
                        self.lbl_updated.config(text=f"Error: {payload}", fg=BAR_CRIT)
                        if self.last_success_time:
                            t = self.last_success_time.astimezone().strftime("%H:%M:%S")
                            self.lbl_last_ok.config(text=f"Last OK: {t}")
                        # Retry with backoff for transient errors
                        is_transient = (
                            "429" in payload or "Rate limited" in payload
                            or "401" in payload or "Token expired" in payload
                            or "Network error" in payload
                        )
                        if is_transient and self.retry_count < self.max_retries:
                            self.retry_count += 1
                            if "401" in payload or "Token expired" in payload:
                                retry_delay = 60000 * self.retry_count  # 60s, 120s, 180s
                            else:
                                retry_delay = 30000 * self.retry_count  # 30s, 60s, 90s
                            log.warning("Transient error '%s', retry %d in %ds",
                                        payload, self.retry_count, retry_delay // 1000)
                            self.root.after(retry_delay, self._trigger_fetch)
                except Exception as exc:
                    log.exception("Unexpected error processing queue item: %s", exc)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_queue)

    def _schedule_refresh(self):
        self._trigger_fetch()
        self.root.after(REFRESH_MS, self._schedule_refresh)

    def _update_ui(self, d: UsageData):
        # 5-hour bar
        self._update_bar(self.bar_5h, d.five_hour_pct)
        self.lbl_5h_pct.config(text=f"{d.five_hour_pct:.1f}%")
        t, c = format_timedelta(d.five_hour_resets_at)
        self.lbl_5h_reset.config(text=f"Reset in: {t}", fg=c)

        # 7-day bar
        self._update_bar(self.bar_7d, d.seven_day_pct)
        self.lbl_7d_pct.config(text=f"{d.seven_day_pct:.1f}%")
        t, c = format_timedelta(d.seven_day_resets_at)
        log.debug("7-day resets_at=%s, now=%s, display='%s'",
                  d.seven_day_resets_at.isoformat(),
                  datetime.now(timezone.utc).isoformat(), t)
        self.lbl_7d_reset.config(text=f"Reset in: {t}", fg=c)

        # Per-model
        if d.sonnet_pct is not None:
            self.lbl_sonnet.config(text=f"Sonnet 7d:  {d.sonnet_pct:.1f}%")
        if d.opus_pct is not None:
            self.lbl_opus.config(text=f"Opus 7d:    {d.opus_pct:.1f}%")

        # Extra usage
        if d.extra_enabled and d.extra_limit_cents > 0:
            limit_eur = d.extra_limit_cents / 100
            used_eur = d.extra_used_cents / 100
            pct = min(d.extra_used_cents / d.extra_limit_cents * 100, 100)
            self._update_bar(self.bar_extra, pct)
            currency = "€"
            self.lbl_extra.config(
                text=f"{currency}{used_eur:.2f} / {currency}{limit_eur:.2f} spent"
            )
        else:
            self.bar_extra.coords("fill", 0, 0, 0, 10)
            self.lbl_extra.config(text="disabled" if not d.extra_enabled else "—")

        # Footer
        self.last_success_time = d.last_updated
        self.lbl_updated.config(text=self._ago(d.last_updated), fg=FG_LABEL)
        self.lbl_last_ok.config(text="")

    def _ago(self, dt: datetime) -> str:
        elapsed = int((datetime.now(timezone.utc) - dt).total_seconds())
        if elapsed < 60:
            return "Updated: just now"
        mins = elapsed // 60
        hours = mins // 60
        if hours > 0:
            return f"Updated: {hours}h {mins % 60}m ago"
        return f"Updated: {mins}m ago"

    def _tick_updated(self):
        if self.last_success_time and not self._has_error:
            self.lbl_updated.config(text=self._ago(self.last_success_time))
        self.root.after(60_000, self._tick_updated)

    # ------------------------------------------------------------------
    # Drag
    # ------------------------------------------------------------------

    def _drag_start(self, e):
        self._drag_x = e.x
        self._drag_y = e.y

    def _drag_motion(self, e):
        x = self.root.winfo_x() + (e.x - self._drag_x)
        y = self.root.winfo_y() + (e.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")
        self._push_to_bottom()

    def _drag_end(self, _e):
        self._save_position()

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    def _load_position(self) -> tuple:
        try:
            data = json.loads(POS_FILE.read_text(encoding="utf-8"))
            return int(data["x"]), int(data["y"])
        except Exception:
            return 20, 200

    def _save_position(self):
        try:
            POS_FILE.write_text(
                json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Data cache persistence
    # ------------------------------------------------------------------

    def _save_cache(self, d: UsageData):
        try:
            CACHE_FILE.write_text(
                json.dumps({
                    "five_hour_pct": d.five_hour_pct,
                    "five_hour_resets_at": d.five_hour_resets_at.isoformat(),
                    "seven_day_pct": d.seven_day_pct,
                    "seven_day_resets_at": d.seven_day_resets_at.isoformat(),
                    "sonnet_pct": d.sonnet_pct,
                    "opus_pct": d.opus_pct,
                    "extra_enabled": d.extra_enabled,
                    "extra_limit_cents": d.extra_limit_cents,
                    "extra_used_cents": d.extra_used_cents,
                }),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _load_cache(self) -> Optional[UsageData]:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            def parse_dt(s):
                import re
                s_clean = re.sub(r'[Zz]$|[+-]\d{2}:\d{2}$', '', s)
                return datetime.fromisoformat(s_clean).replace(tzinfo=timezone.utc)
            return UsageData(
                five_hour_pct=float(data["five_hour_pct"]),
                five_hour_resets_at=parse_dt(data["five_hour_resets_at"]),
                seven_day_pct=float(data["seven_day_pct"]),
                seven_day_resets_at=parse_dt(data["seven_day_resets_at"]),
                sonnet_pct=data.get("sonnet_pct"),
                opus_pct=data.get("opus_pct"),
                last_updated=datetime.now(timezone.utc),
                extra_enabled=bool(data.get("extra_enabled", False)),
                extra_limit_cents=int(data.get("extra_limit_cents", 0)),
                extra_used_cents=float(data.get("extra_used_cents", 0.0)),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close(self, _e=None):
        self._save_position()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time
    time.sleep(120)
    ClaudeMeterWidget().run()
