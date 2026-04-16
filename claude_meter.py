"""
Claude Meter - Desktop widget showing Claude Code Pro usage limits.
Reads OAuth token from ~/.claude/.credentials.json and queries Anthropic API.
"""

import base64
import ctypes
import json
import logging
import os
import queue
import sqlite3
import threading
import tkinter as tk
import urllib.error
import urllib.parse
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
# .env loader  (no external deps)
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"


def _load_env_file() -> None:
    """Parse .env next to this script and inject missing keys into os.environ."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip()


_load_env_file()


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
WIDGET_H         = 500
POS_FILE         = Path.home() / ".claude" / "meter_pos.json"
CACHE_FILE       = Path.home() / ".claude" / "meter_cache.json"

# Google Antigravity IDE
ANTIGRAVITY_DB        = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"
ANTIGRAVITY_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
ANTIGRAVITY_UA        = "antigravity/1.0.0"
# Client credentials are loaded from .env (never hardcoded here)

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


@dataclass
class AntigravityData:
    sprint_pct: float
    sprint_resets_at: datetime
    weekly_pct: float
    weekly_resets_at: datetime
    last_updated: datetime
    sonnet_pct: Optional[float] = None
    opus_pct: Optional[float] = None


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
            log.debug("extra_usage in anthropic response: %s", raw.get("extra_usage"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FetchError("Token expired or invalid (401)")
            if e.code == 429:
                raise FetchError("Rate limited by API (429)")
            raise FetchError(f"HTTP error {e.code}")
        except urllib.error.URLError as e:
            raise FetchError(f"Network error: {e.reason}")

        # Use extra_usage from the anthropic response if available,
        # otherwise fall back to the claude.ai endpoint (requires browser session)
        if raw.get("extra_usage") is not None:
            extra_usage = raw.get("extra_usage")
        else:
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
# Google Antigravity fetcher
# ---------------------------------------------------------------------------

class AntigravityFetcher:
    """Reads quota data from Google Antigravity IDE via cloudcode-pa API."""

    # ------------------------------------------------------------------
    # Token extraction from state.vscdb (nested protobuf → base64)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_varint(data: bytes, pos: int):
        val, shift = 0, 0
        while pos < len(data):
            b = data[pos]; pos += 1
            val |= (b & 0x7f) << shift; shift += 7
            if not (b & 0x80):
                break
        return val, pos

    @staticmethod
    def _parse_ld_fields(data: bytes):
        """Yield (field_num, value_bytes) for wire-type-2 (length-delimited) fields."""
        i = 0
        while i < len(data):
            if i >= len(data):
                break
            tag = data[i]; i += 1
            field, wire = tag >> 3, tag & 7
            if wire == 2:
                length, i = AntigravityFetcher._read_varint(data, i)
                yield field, data[i:i + length]
                i += length
            elif wire == 0:
                _, i = AntigravityFetcher._read_varint(data, i)
            else:
                return

    def _get_refresh_token(self) -> str:
        if not ANTIGRAVITY_DB.exists():
            raise FetchError("Antigravity not installed")
        try:
            conn = sqlite3.connect(str(ANTIGRAVITY_DB))
            try:
                cur = conn.cursor()
                cur.execute("SELECT value FROM ItemTable WHERE key=?",
                            ("antigravityUnifiedStateSync.oauthToken",))
                row = cur.fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise FetchError(f"Antigravity DB error: {e}")

        if not row:
            raise FetchError("Antigravity: not signed in")

        # Outer proto → field1 → field2 → field1(inner b64) → inner proto → field3=refresh_token
        outer = base64.b64decode(row[0] + "==")
        for _, v1 in self._parse_ld_fields(outer):
            for f2, v2 in self._parse_ld_fields(v1):
                if f2 != 2:
                    continue
                for _, inner_b64_bytes in self._parse_ld_fields(v2):
                    try:
                        inner = base64.b64decode(inner_b64_bytes.decode("utf-8") + "==")
                    except Exception:
                        continue
                    for f4, v4 in self._parse_ld_fields(inner):
                        if f4 == 3:
                            return v4.decode("utf-8")

        raise FetchError("Antigravity: refresh token not found in DB")

    def _get_access_token(self, refresh_token: str) -> str:
        client_id = os.environ.get("ANTIGRAVITY_CLIENT_ID", "")
        client_secret = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise FetchError("Antigravity credentials not configured — restart to run setup")
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            ANTIGRAVITY_TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise FetchError(f"Antigravity token refresh failed ({e.code})")
        except urllib.error.URLError as e:
            raise FetchError(f"Antigravity network error: {e.reason}")

        token = result.get("access_token")
        if not token:
            raise FetchError("Antigravity: no access_token in refresh response")
        return token

    # ------------------------------------------------------------------
    # Quota API
    # ------------------------------------------------------------------

    def fetch(self) -> AntigravityData:
        refresh_token = self._get_refresh_token()
        access_token = self._get_access_token(refresh_token)

        req = urllib.request.Request(
            ANTIGRAVITY_QUOTA_URL,
            data=json.dumps({}).encode(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": ANTIGRAVITY_UA,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FetchError("Antigravity: auth expired — reopen IDE")
            raise FetchError(f"Antigravity quota API error ({e.code})")
        except urllib.error.URLError as e:
            raise FetchError(f"Antigravity network error: {e.reason}")

        return self._parse(raw)

    def _parse(self, raw: dict) -> AntigravityData:
        import re, json as _json
        log.debug("[AG] raw response: %s", _json.dumps(raw, indent=2, default=str))
        now = datetime.now(timezone.utc)
        sprint_pct, sprint_reset = 0.0, now + timedelta(hours=5)
        weekly_pct, weekly_reset = 0.0, now + timedelta(days=7)
        sonnet_pct: Optional[float] = None
        opus_pct: Optional[float] = None

        for model_name, info in raw.get("models", {}).items():
            qi = info.get("quotaInfo")
            if not qi or "remainingFraction" not in qi:
                log.debug("[AG] %-40s  no quotaInfo", model_name)
                continue
            remaining = float(qi["remainingFraction"])
            used_pct = (1.0 - remaining) * 100.0

            # Track Claude model usage regardless of resetTime
            if model_name == "claude-sonnet-4-6":
                sonnet_pct = used_pct
            elif model_name == "claude-opus-4-6-thinking":
                opus_pct = used_pct

            if "resetTime" not in qi:
                continue
            try:
                s = qi["resetTime"]
                s_clean = re.sub(r"[Zz]$|[+-]\d{2}:\d{2}$", "", s)
                fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s_clean else "%Y-%m-%dT%H:%M:%S"
                reset_dt = datetime.strptime(s_clean, fmt).replace(tzinfo=timezone.utc)
            except Exception:
                log.debug("[AG] %-40s  bad resetTime: %s", model_name, qi.get("resetTime"))
                continue

            delta = (reset_dt - now).total_seconds()
            bucket = "sprint" if delta < 86400 else "weekly"
            log.debug("[AG] %-40s  used=%5.1f%%  resets_in=%6.0fs  bucket=%s",
                      model_name, used_pct, delta, bucket)

            if delta < 86400:           # resets within 24h → sprint bucket
                if used_pct > sprint_pct:
                    sprint_pct = used_pct
                    sprint_reset = reset_dt
            else:                       # resets in days → weekly bucket
                if used_pct > weekly_pct:
                    weekly_pct = used_pct
                    weekly_reset = reset_dt

        return AntigravityData(
            sprint_pct=sprint_pct,
            sprint_resets_at=sprint_reset,
            weekly_pct=weekly_pct,
            weekly_resets_at=weekly_reset,
            last_updated=now,
            sonnet_pct=sonnet_pct,
            opus_pct=opus_pct,
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
# First-run setup helpers
# ---------------------------------------------------------------------------

def _detect_antigravity_credentials() -> tuple:
    """Try to extract client_id/secret from the local Antigravity IDE installation."""
    import re
    main_js = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs" / "Antigravity" / "resources" / "app" / "out" / "main.js"
    )
    if not main_js.exists():
        return "", ""
    try:
        client_id = client_secret = ""
        chunk_size = 65536
        overlap = 512
        with open(main_js, "rb") as f:
            prev = b""
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                data = prev + chunk
                if not client_id:
                    m = re.search(rb'[a-z]{3}="([\d-]+\.apps\.googleusercontent\.com)"', data)
                    if m:
                        client_id = m.group(1).decode()
                if not client_secret:
                    m = re.search(rb'[a-z]{3}="(GOCSPX-[A-Za-z0-9_-]+)"', data)
                    if m:
                        client_secret = m.group(1).decode()
                if client_id and client_secret:
                    break
                prev = data[-overlap:]
        return client_id, client_secret
    except Exception as exc:
        log.warning("Auto-detect credentials failed: %s", exc)
        return "", ""


def _save_env_values(values: dict) -> None:
    """Write/update key=value pairs in the .env file."""
    existing: dict = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update(values)
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_setup_dialog() -> bool:
    """Show a first-run dialog to collect Antigravity OAuth credentials.

    Returns True if the user saved valid credentials, False if cancelled.
    """
    result = {"saved": False}

    dlg = tk.Tk()
    dlg.title("Claude Meter — Setup")
    dlg.configure(bg=BG)
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)

    # Center on screen
    dlg.update_idletasks()
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"420x320+{(sw - 420) // 2}+{(sh - 320) // 2}")

    tk.Label(dlg, text="◉ Claude Meter — First-time Setup", bg=HEADER, fg=FG,
             font=("Segoe UI", 10, "bold"), pady=8).pack(fill="x")

    body = tk.Frame(dlg, bg=BG, padx=16, pady=10)
    body.pack(fill="both", expand=True)

    tk.Label(body, text="Google Antigravity OAuth credentials", bg=BG, fg="#5cb8ff",
             font=("Segoe UI", 9, "bold")).pack(anchor="w")
    tk.Label(body,
             text=("Found in your Antigravity installation:\n"
                   "%LocalAppData%\\Programs\\Antigravity\\resources\\app\\out\\main.js\n"
                   'Search ".apps.googleusercontent.com" and "GOCSPX-"'),
             bg=BG, fg=FG_DIM, font=("Segoe UI", 7), justify="left").pack(anchor="w", pady=(2, 8))

    tk.Label(body, text="Client ID:", bg=BG, fg=FG_LABEL,
             font=("Segoe UI", 8)).pack(anchor="w")
    entry_id = tk.Entry(body, bg=BAR_BG, fg=FG, insertbackground=FG,
                        font=("Segoe UI", 8), width=55, relief="flat")
    entry_id.pack(fill="x", pady=(0, 6))

    tk.Label(body, text="Client Secret:", bg=BG, fg=FG_LABEL,
             font=("Segoe UI", 8)).pack(anchor="w")
    entry_secret = tk.Entry(body, bg=BAR_BG, fg=FG, insertbackground=FG,
                            font=("Segoe UI", 8), width=55, relief="flat", show="•")
    entry_secret.pack(fill="x", pady=(0, 6))

    lbl_status = tk.Label(body, text="", bg=BG, fg=BAR_CRIT, font=("Segoe UI", 7))
    lbl_status.pack(anchor="w")

    # Pre-fill if we have partial values already
    if os.environ.get("ANTIGRAVITY_CLIENT_ID"):
        entry_id.insert(0, os.environ["ANTIGRAVITY_CLIENT_ID"])
    if os.environ.get("ANTIGRAVITY_CLIENT_SECRET"):
        entry_secret.insert(0, os.environ["ANTIGRAVITY_CLIENT_SECRET"])

    def on_save():
        cid = entry_id.get().strip()
        csec = entry_secret.get().strip()
        if not cid or not csec:
            lbl_status.config(text="Both fields are required.")
            return
        os.environ["ANTIGRAVITY_CLIENT_ID"] = cid
        os.environ["ANTIGRAVITY_CLIENT_SECRET"] = csec
        _save_env_values({"ANTIGRAVITY_CLIENT_ID": cid, "ANTIGRAVITY_CLIENT_SECRET": csec})
        result["saved"] = True
        dlg.destroy()

    def on_skip():
        dlg.destroy()

    btn_frame = tk.Frame(body, bg=BG)
    btn_frame.pack(fill="x", pady=(4, 0))
    tk.Button(btn_frame, text="Save & Start", command=on_save,
              bg="#0f3460", fg=FG, activebackground="#1a5490", activeforeground=FG,
              relief="flat", padx=12, pady=4).pack(side="right", padx=(4, 0))
    tk.Button(btn_frame, text="Skip (Antigravity section will be disabled)",
              command=on_skip, bg=BAR_BG, fg=FG_DIM,
              activebackground="#3a3a5a", activeforeground=FG,
              relief="flat", padx=8, pady=4).pack(side="right")

    dlg.mainloop()
    return result["saved"]


def _ensure_ag_credentials() -> None:
    """Ensure Antigravity credentials are available.

    Order of preference:
    1. Already in os.environ (loaded from .env or system env)
    2. Auto-detected from local Antigravity installation → saved to .env
    3. User enters them via setup dialog → saved to .env
    4. User skips → widget starts without Antigravity section
    """
    if os.environ.get("ANTIGRAVITY_CLIENT_ID") and os.environ.get("ANTIGRAVITY_CLIENT_SECRET"):
        return

    log.info("Antigravity credentials not found in .env — attempting auto-detect")
    client_id, client_secret = _detect_antigravity_credentials()
    if client_id and client_secret:
        log.info("Auto-detected Antigravity credentials from IDE installation")
        os.environ["ANTIGRAVITY_CLIENT_ID"] = client_id
        os.environ["ANTIGRAVITY_CLIENT_SECRET"] = client_secret
        _save_env_values({"ANTIGRAVITY_CLIENT_ID": client_id, "ANTIGRAVITY_CLIENT_SECRET": client_secret})
        return

    log.info("Auto-detect failed — showing setup dialog")
    _run_setup_dialog()


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class ClaudeMeterWidget:
    def __init__(self):
        self.fetcher = UsageFetcher()
        self.ag_fetcher = AntigravityFetcher()
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
        ag_cached = self._load_ag_cache()
        if ag_cached:
            self._update_ag_ui(ag_cached)

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

        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=3)

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
        self.model_frame.pack(fill="x", pady=(2, 0))
        self.lbl_sonnet = tk.Label(self.model_frame, text="", bg=BG,
                                   fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_sonnet.pack(anchor="w")
        self.lbl_opus = tk.Label(self.model_frame, text="", bg=BG,
                                 fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_opus.pack(anchor="w")

        # Extra Usage section
        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=3)
        self.extra_frame = tk.Frame(body, bg=BG)
        self.extra_frame.pack(fill="x")
        tk.Label(self.extra_frame, text="EXTRA USAGE", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")
        self.bar_extra = self._make_bar(self.extra_frame)
        self.lbl_extra = tk.Label(self.extra_frame, text="—", bg=BG,
                                  fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_extra.pack(anchor="w")

        # Antigravity section
        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=3)
        tk.Label(body, text="◉ ANTIGRAVITY", bg=BG, fg="#5cb8ff",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")

        tk.Label(body, text="SPRINT", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7)).pack(anchor="w")
        self.bar_ag_sprint = self._make_bar(body)
        ag_sprint_row = tk.Frame(body, bg=BG)
        ag_sprint_row.pack(fill="x")
        self.lbl_ag_sprint_pct = tk.Label(ag_sprint_row, text="—", bg=BG, fg=FG,
                                          font=("Segoe UI", 9))
        self.lbl_ag_sprint_pct.pack(side="right")
        self.lbl_ag_sprint_reset = tk.Label(ag_sprint_row, text="Reset in: —", bg=BG,
                                            fg=FG_DIM, font=("Segoe UI", 8))
        self.lbl_ag_sprint_reset.pack(side="left")

        tk.Label(body, text="WEEKLY", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7)).pack(anchor="w", pady=(3, 0))
        self.bar_ag_weekly = self._make_bar(body)
        ag_weekly_row = tk.Frame(body, bg=BG)
        ag_weekly_row.pack(fill="x")
        self.lbl_ag_weekly_pct = tk.Label(ag_weekly_row, text="—", bg=BG, fg=FG,
                                          font=("Segoe UI", 9))
        self.lbl_ag_weekly_pct.pack(side="right")
        self.lbl_ag_weekly_reset = tk.Label(ag_weekly_row, text="Reset in: —", bg=BG,
                                            fg=FG_DIM, font=("Segoe UI", 8))
        self.lbl_ag_weekly_reset.pack(side="left")

        ag_claude_frame = tk.Frame(body, bg=BG)
        ag_claude_frame.pack(fill="x", pady=(3, 0))
        self.lbl_ag_sonnet = tk.Label(ag_claude_frame, text="", bg=BG,
                                      fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_ag_sonnet.pack(anchor="w")
        self.lbl_ag_opus = tk.Label(ag_claude_frame, text="", bg=BG,
                                    fg=FG_LABEL, font=("Segoe UI", 8))
        self.lbl_ag_opus.pack(anchor="w")

        self.lbl_ag_error = tk.Label(body, text="", bg=BG, fg=BAR_CRIT,
                                     font=("Segoe UI", 7), wraplength=WIDGET_W - 24)
        self.lbl_ag_error.pack(anchor="w")

        # Footer
        tk.Frame(body, bg="#2a2a4a", height=1).pack(fill="x", pady=(3, 2))
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

        try:
            ag_data = self.ag_fetcher.fetch()
            self.q.put(("ag_ok", ag_data))
        except FetchError as e:
            log.warning("Antigravity fetch error: %s", e)
            self.q.put(("ag_err", str(e)))
        except Exception as e:
            log.warning("Antigravity unexpected error: %s", e)
            self.q.put(("ag_err", f"AG error: {e}"))

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
                    elif kind == "ag_ok":
                        self._update_ag_ui(payload)
                        self._save_ag_cache(payload)
                    elif kind == "ag_err":
                        log.debug("AG error displayed: %s", payload)
                        self.lbl_ag_error.config(text=payload)
                        self.bar_ag_sprint.coords("fill", 0, 0, 0, 10)
                        self.bar_ag_weekly.coords("fill", 0, 0, 0, 10)
                        self.lbl_ag_sprint_pct.config(text="—")
                        self.lbl_ag_weekly_pct.config(text="—")
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

    def _update_ag_ui(self, d: AntigravityData):
        self.lbl_ag_error.config(text="")
        self._update_bar(self.bar_ag_sprint, d.sprint_pct)
        self.lbl_ag_sprint_pct.config(text=f"{d.sprint_pct:.1f}%")
        t, c = format_timedelta(d.sprint_resets_at)
        self.lbl_ag_sprint_reset.config(text=f"Reset in: {t}", fg=c)

        self._update_bar(self.bar_ag_weekly, d.weekly_pct)
        self.lbl_ag_weekly_pct.config(text=f"{d.weekly_pct:.1f}%")
        t, c = format_timedelta(d.weekly_resets_at)
        self.lbl_ag_weekly_reset.config(text=f"Reset in: {t}", fg=c)

        if d.sonnet_pct is not None:
            self.lbl_ag_sonnet.config(text=f"Sonnet sprint: {d.sonnet_pct:.1f}%")
        if d.opus_pct is not None:
            self.lbl_ag_opus.config(text=f"Opus sprint:   {d.opus_pct:.1f}%")

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
            existing = {}
            try:
                existing = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            existing.update({
                "five_hour_pct": d.five_hour_pct,
                "five_hour_resets_at": d.five_hour_resets_at.isoformat(),
                "seven_day_pct": d.seven_day_pct,
                "seven_day_resets_at": d.seven_day_resets_at.isoformat(),
                "sonnet_pct": d.sonnet_pct,
                "opus_pct": d.opus_pct,
                "extra_enabled": d.extra_enabled,
                "extra_limit_cents": d.extra_limit_cents,
                "extra_used_cents": d.extra_used_cents,
            })
            CACHE_FILE.write_text(json.dumps(existing), encoding="utf-8")
        except Exception:
            pass

    def _save_ag_cache(self, d: AntigravityData):
        try:
            existing = {}
            try:
                existing = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            existing.update({
                "ag_sprint_pct": d.sprint_pct,
                "ag_sprint_resets_at": d.sprint_resets_at.isoformat(),
                "ag_weekly_pct": d.weekly_pct,
                "ag_weekly_resets_at": d.weekly_resets_at.isoformat(),
                "ag_sonnet_pct": d.sonnet_pct,
                "ag_opus_pct": d.opus_pct,
            })
            CACHE_FILE.write_text(json.dumps(existing), encoding="utf-8")
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

    def _load_ag_cache(self) -> Optional[AntigravityData]:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if "ag_sprint_pct" not in data:
                return None
            def parse_dt(s):
                import re
                s_clean = re.sub(r'[Zz]$|[+-]\d{2}:\d{2}$', '', s)
                return datetime.fromisoformat(s_clean).replace(tzinfo=timezone.utc)
            return AntigravityData(
                sprint_pct=float(data["ag_sprint_pct"]),
                sprint_resets_at=parse_dt(data["ag_sprint_resets_at"]),
                weekly_pct=float(data["ag_weekly_pct"]),
                weekly_resets_at=parse_dt(data["ag_weekly_resets_at"]),
                last_updated=datetime.now(timezone.utc),
                sonnet_pct=data.get("ag_sonnet_pct"),
                opus_pct=data.get("ag_opus_pct"),
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
    _ensure_ag_credentials()
    import time
    time.sleep(120)
    ClaudeMeterWidget().run()
