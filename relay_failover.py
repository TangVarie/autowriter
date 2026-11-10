"""relay_failover: optional backup relay for Anthropic Messages calls.

Shared core. The same file is copied verbatim into truth-vault (relay_failover.py, repo root),
autowriter (relay_failover.py, repo root) and jevforcoentent (judge/relay.py). Standard library only.

Env (all optional; missing or empty = not set):
  ANTHROPIC_BASE_URL_BACKUP, ANTHROPIC_API_KEY_BACKUP  backup relay; "configured" only when both are non-empty
  RELAY_FORCE              "" / "auto" = automatic, "primary" = never use the backup, "backup" = backup only
  RELAY_PRIMARY_TIMEOUT_S  per-attempt timeout for the primary in automatic mode (5..600; default per call site)
  RELAY_SLOW_S             a primary success slower than this counts as a breaker failure (0..600, 0 = off)
  RELAY_BREAKER_FAILS      consecutive primary failures that open the breaker (1..20, default 2)
  RELAY_BREAKER_COOLDOWN_S how long an open breaker sends calls straight to the backup (30..86400, default 1200)

Never logs or returns a base URL, host, key or relay error body: only "primary"/"backup", HTTP status,
exception class name, relay error.type, timings and counts.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from typing import Callable, Optional, TypeVar, Union

CORE_VERSION = 1

T = TypeVar("T")
log = logging.getLogger("relay")

MODE_PRIMARY_ONLY = "primary_only"
MODE_AUTO = "auto"
MODE_BACKUP_ONLY = "backup_only"

OFFICIAL_BASE_URL = "https://api.anthropic.com"
FAILOVER_STATUSES = frozenset({401, 402, 403, 404, 408, 409, 429})          # plus every 5xx
TRANSIENT_ERROR_TYPES = frozenset({"overloaded_error", "api_error", "rate_limit_error", "timeout_error"})
TRANSPORT_ERROR_NAMES = frozenset({"APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionError",
                                   "URLError", "HTTPException", "SSLError", "JSONDecodeError"})
GATEWAY_FILTER_MARKERS = ("sensitive_words_detected",)                        # D-125: content verdict, not an outage
PROBE_TTL_S = 600.0
COUNTER_KEYS = ("primary_ok", "primary_slow", "primary_fail", "backup_ok", "backup_fail", "breaker_open_skips")

_getenv: Callable[[str], Optional[str]] = os.environ.get
_lock = threading.Lock()            # counters, warn-once, atexit flag
_state_lock = threading.Lock()      # breaker read-modify-write inside this process (+ flock across processes)
_counts = dict.fromkeys(COUNTER_KEYS, 0)
_warned: set = set()
_mem_state: dict = {}               # last state this process saw, used when the state file cannot be read
_atexit_registered = False


def _now() -> float:                # wall clock: shared across processes; tests patch this
    return time.time()


def _mono() -> float:               # call durations; tests patch this
    return time.monotonic()


# ── configuration ──────────────────────────────────────────────────────────

def configure(getenv: Callable[[str], Optional[str]]) -> None:
    """autowriter: read config.<NAME> (env, then st.secrets) instead of os.environ."""
    global _getenv
    _getenv = getenv


def _env(name: str) -> str:
    try:
        v = _getenv(name)
    except Exception:
        v = None
    return "" if v is None else str(v).strip()


def _num(name: str, default, lo, hi, cast=float):
    raw = _env(name)
    if not raw:
        return default
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def backup_base_url() -> str:
    return _env("ANTHROPIC_BASE_URL_BACKUP")


def backup_api_key() -> str:
    return _env("ANTHROPIC_API_KEY_BACKUP")


def backup_configured() -> bool:
    return bool(backup_base_url() and backup_api_key())


def force_setting() -> str:
    raw = _env("RELAY_FORCE").lower()
    if raw in ("", "auto"):
        return "auto"
    if raw in ("primary", "backup"):
        return raw
    _warn_once("force_invalid", "relay: RELAY_FORCE not recognized (expected primary, backup or empty); using auto")
    return "auto"


def mode(configured: Optional[bool] = None) -> str:
    """primary_only = exactly the pre-backup behaviour; auto = primary first with failover; backup_only."""
    if configured is None:
        configured = backup_configured()
    force = force_setting()
    if force == "primary":
        return MODE_PRIMARY_ONLY
    if not configured:
        if force == "backup":
            _warn_once("force_unconfigured",
                       "relay: RELAY_FORCE=backup but no backup relay is configured; using primary")
        return MODE_PRIMARY_ONLY
    return MODE_BACKUP_ONLY if force == "backup" else MODE_AUTO


def primary_timeout_s(default: float) -> float:
    return float(_num("RELAY_PRIMARY_TIMEOUT_S", float(default), 5.0, 600.0))


def slow_s(default: float) -> float:
    return float(_num("RELAY_SLOW_S", float(default), 0.0, 600.0))


def breaker_fails() -> int:
    return int(_num("RELAY_BREAKER_FAILS", 2, 1, 20, int))


def breaker_cooldown_s() -> float:
    return float(_num("RELAY_BREAKER_COOLDOWN_S", 1200.0, 30.0, 86400.0))


# ── classification ─────────────────────────────────────────────────────────

def status_of(exc) -> Optional[int]:
    for attr in ("status_code", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
            return v
    return None


def error_type_of(exc) -> Optional[str]:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        t = err.get("type") if isinstance(err, dict) else body.get("type")
        if isinstance(t, str):
            return t
    return None


def is_gateway_filtered(exc) -> bool:
    try:
        s = f"{exc!r} {exc}"
    except Exception:
        return False
    return any(m in s for m in GATEWAY_FILTER_MARKERS)


def is_failover_worthy(exc) -> bool:
    if is_gateway_filtered(exc):
        return False
    if error_type_of(exc) in TRANSIENT_ERROR_TYPES:
        return True
    st = status_of(exc)
    if st is not None and st >= 400:
        return st in FAILOVER_STATUSES or st >= 500
    return any(c.__name__ in TRANSPORT_ERROR_NAMES for c in type(exc).__mro__)


def describe(exc) -> str:
    """Log-safe label: 'HTTP 503 overloaded_error' or 'APITimeoutError'. Never the message."""
    st = status_of(exc)
    out = f"HTTP {st}" if st is not None and st >= 400 else type(exc).__name__
    et = error_type_of(exc)
    if et in TRANSIENT_ERROR_TYPES:
        out += f" {et}"
    return out


# ── breaker state (JSON file in the temp dir, shared by every process on the machine) ──

def _norm_url(url: Optional[str]) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u or OFFICIAL_BASE_URL


def state_path(primary_url: Optional[str]) -> str:
    h = hashlib.sha256(_norm_url(primary_url).encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"relay_breaker_{h}.json")


def _fresh() -> dict:
    return {"v": 1, "fails": 0, "opened_at": None, "probe_at": None}


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # missing / unreadable / corrupt
        return dict(_mem_state.get(path) or _fresh())
    st = _fresh()
    if not isinstance(d, dict) or d.get("v") != 1:
        return st
    fails = d.get("fails")
    st["fails"] = fails if isinstance(fails, int) and not isinstance(fails, bool) and fails >= 0 else 0
    for k in ("opened_at", "probe_at"):
        x = d.get(k)
        st[k] = float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    return st


def _save(path: str, st: dict) -> None:
    _mem_state[path] = dict(st)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


class _FileLock:
    """flock on <state>.lock; silently a no-op where fcntl or the temp dir is unavailable."""

    def __init__(self, path: str):
        self._path = path + ".lock"
        self._fd = None

    def __enter__(self):
        try:
            import fcntl
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
            self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except Exception:
                pass
        return False


def _transact(path: str, fn):
    with _state_lock, _FileLock(path):
        st = _load(path)
        before = dict(st)
        out = fn(st, _now())
        if st != before:
            _save(path, st)
        return out


def _phase(st: dict, now: float, cooldown: float) -> str:
    opened = st["opened_at"]
    if opened is None:
        return "closed"
    if opened > now + 60.0:          # timestamp from the future (clock skew / corrupt): allow a probe
        return "half_open"
    return "open" if now < opened + cooldown else "half_open"


def _decide(path: str, cooldown: float) -> str:
    """'primary' (closed), 'probe' (half-open, this call claimed the probe) or 'skip' (go straight to backup)."""
    def fn(st, now):
        ph = _phase(st, now, cooldown)
        if ph == "closed":
            return "primary"
        if ph == "open":
            return "skip"
        pa = st["probe_at"]
        if pa is None or now - pa > PROBE_TTL_S or pa > now + 60.0:
            st["probe_at"] = now
            return "probe"
        return "skip"
    return _transact(path, fn)


def _record(path: str, outcome: str, probe: bool, why: str) -> None:
    """outcome: 'ok' (fast success), 'fail' (failover-worthy error or slow success), 'neutral' (other error)."""
    threshold, cooldown = breaker_fails(), breaker_cooldown_s()

    def fn(st, now):
        if outcome == "ok":
            was_open = st["opened_at"] is not None
            st.update(fails=0, opened_at=None, probe_at=None)
            return ("closed", 0) if was_open else None
        if outcome == "fail":
            st["fails"] += 1
            if probe or (st["opened_at"] is None and st["fails"] >= threshold):
                st["opened_at"], st["probe_at"] = now, None
                return ("opened", st["fails"])
            return None
        if probe:
            st["probe_at"] = None
        return None

    event = _transact(path, fn)
    if event and event[0] == "opened":
        log.warning("relay: breaker open for %ds after %d consecutive primary failures (last: %s); "
                    "calls go straight to backup", int(cooldown), event[1], why)
    elif event and event[0] == "closed":
        log.warning("relay: breaker closed (primary ok again)")


def breaker_state(primary_url: Optional[str] = None) -> str:
    """'off' unless mode is auto; else 'closed' | 'open' | 'half_open'."""
    if mode() != MODE_AUTO:
        return "off"
    url = _env("ANTHROPIC_BASE_URL") if primary_url is None else primary_url
    cooldown = breaker_cooldown_s()
    return _transact(state_path(url), lambda st, now: _phase(st, now, cooldown))


# ── the call ───────────────────────────────────────────────────────────────

def call(unit: Callable[[str], T], *, relay_mode: str, primary_url: Optional[str],
         slow_after_s: Union[float, Callable[[], float]] = 0.0) -> T:
    """Run one transport attempt with failover.

    unit(target) performs ONE attempt of the call site's request: target "primary" = the bounded primary client
    (SDK max_retries=0, timeout=primary_timeout_s(site default)); target "backup" = the backup client with the call
    site's normal settings. Call sites never call this in primary_only mode (they keep their old code path).
    """
    _ensure_atexit()
    if relay_mode == MODE_BACKUP_ONLY:
        return _backup(unit)
    if relay_mode != MODE_AUTO:
        raise ValueError("relay_failover.call() needs mode auto or backup_only")
    path = state_path(primary_url)
    route = _decide(path, breaker_cooldown_s())
    if route == "skip":
        _inc("breaker_open_skips")
        return _backup(unit)
    probe = route == "probe"
    t0 = _mono()
    failed: Optional[str] = None
    try:
        result = unit("primary")
    except Exception as exc:
        if not is_failover_worthy(exc):
            _record(path, "neutral", probe, "")
            raise
        failed = describe(exc)
    if failed is None:
        took = _mono() - t0
        limit = slow_after_s() if callable(slow_after_s) else slow_after_s
        slow = bool(limit) and limit > 0 and took > limit
        _inc("primary_ok")
        if slow:
            _inc("primary_slow")
        _record(path, "fail" if slow else "ok", probe, f"slow {took:.1f}s")
        return result
    # outside the except block: the backup's exception must not carry the primary's as __context__
    _inc("primary_fail")
    _record(path, "fail", probe, failed)
    _warn_once("first_failover", f"relay: primary failed ({failed}); failing over to backup")
    return _backup(unit)


def _backup(unit: Callable[[str], T]) -> T:
    try:
        result = unit("backup")
    except Exception:
        _inc("backup_fail")
        raise
    _inc("backup_ok")
    return result


def sdk_backup_client(anthropic_module, **extra):
    """anthropic SDK client for the backup: explicit key + URL, and no bearer token from the primary's env."""
    client = anthropic_module.Anthropic(api_key=backup_api_key(), base_url=backup_base_url(), **extra)
    try:
        client.auth_token = None   # some SDK versions read ANTHROPIC_AUTH_TOKEN even when api_key is given
    except Exception:
        pass
    if _env("ANTHROPIC_CUSTOM_HEADERS"):
        _warn_once("custom_headers",
                   "relay: ANTHROPIC_CUSTOM_HEADERS is set; some SDK versions also send it to the backup relay")
    return client


# ── counters, health, summary ──────────────────────────────────────────────

def _inc(key: str) -> None:
    with _lock:
        _counts[key] += 1


def counts() -> dict:
    with _lock:
        return dict(_counts)


def summary(primary_url: Optional[str] = None) -> dict:
    """For Done lines / exit line. Keys never include 'ok' or 'failed' (workflows grep those)."""
    return {"mode": mode(), **counts(), "breaker": breaker_state(primary_url)}


def health(include_counts: bool = True, primary_url: Optional[str] = None) -> dict:
    out = {"relay_mode": mode(), "relay_backup_configured": backup_configured(),
           "relay_force": force_setting(), "relay_breaker": breaker_state(primary_url)}
    if include_counts:
        out["relay_counts"] = counts()
    return out


def _warn_once(key: str, msg: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg)


def _emit_summary() -> None:
    if not any(counts().values()):
        return
    try:
        log.warning("relay summary: %s", json.dumps(summary(), sort_keys=True))
    except Exception:
        pass


def _ensure_atexit() -> None:
    global _atexit_registered
    with _lock:
        if _atexit_registered:
            return
        _atexit_registered = True
    atexit.register(_emit_summary)


def _reset_for_tests() -> None:
    with _lock:
        for k in _counts:
            _counts[k] = 0
        _warned.clear()
    with _state_lock:
        _mem_state.clear()
