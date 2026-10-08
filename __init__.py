"""nowstamp: keep the agent aware of the current date and time.

- pre_llm_call: injects the current time into each turn's user message,
  plus how long ago the previous message in the conversation was.
- post_llm_call: records when the agent finished replying.
- transform_tool_result (opt-in): stamps the current time onto a tool result,
  at most once per interval, so the agent stays current during long turns.
"""
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

_DEFAULTS = {
    "timezone": "",
    "format": "%a %Y-%m-%d %H:%M %Z (UTC%z)",
    "language": "en",
    "show_elapsed": True,
    "stamp_tool_results": False,
    "stamp_interval_seconds": 60,
}
_cfg = dict(_DEFAULTS)

_DAYS = {
    "id": ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"],
}

_MAX_SESSIONS = 200
_lock = threading.Lock()
_last_seen = {}   # session_id -> ISO time of the last message (persisted)
_last_shown = {}  # session/task id -> datetime the agent was last told the time
_state = None     # ctx.state when the host provides it (survives restarts)


def _now():
    """Timezone-aware now: plugin override, then Hermes setting, then server local."""
    if _cfg["timezone"]:
        try:
            return datetime.now(ZoneInfo(_cfg["timezone"]))
        except Exception:
            pass
    try:
        import hermes_time
        return hermes_time.now()
    except Exception:
        return datetime.now().astimezone()


def _format(now):
    fmt = _cfg["format"]
    days = _DAYS.get(_cfg["language"])
    if days:
        day = days[now.weekday()]
        fmt = fmt.replace("%A", day).replace("%a", day)
    try:
        return now.strftime(fmt)
    except Exception:
        return now.strftime(_DEFAULTS["format"])


def _ago(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    m = s // 60
    if m < 60:
        return f"{m} min ago"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m} min ago" if m else f"{h} h ago"
    d, h = divmod(h, 24)
    return f"{d} d {h} h ago" if h else f"{d} d ago"


def _when(now, prev):
    days = (now.date() - prev.date()).days
    if days <= 0:
        return f"today {prev:%H:%M}"
    if days == 1:
        return f"yesterday {prev:%H:%M}"
    return f"{prev:%Y-%m-%d %H:%M}"


def _trim(table):
    while len(table) > _MAX_SESSIONS:
        table.pop(next(iter(table)))


def _save(snapshot):
    if _state is None:
        return
    try:
        _state.set("last_seen", snapshot)
    except Exception:
        pass


def _restore():
    if _state is None:
        return
    try:
        saved = _state.get("last_seen", default={})
    except Exception:
        return
    if not isinstance(saved, dict):
        return
    with _lock:
        for key, value in saved.items():
            if isinstance(key, str) and isinstance(value, str):
                _last_seen.setdefault(key, value)


def _touch(session_id, now):
    """Record `now` as the session's latest message time; return the previous one."""
    key = str(session_id)
    with _lock:
        raw = _last_seen.pop(key, None)
        _last_seen[key] = now.isoformat()
        _trim(_last_seen)
        snapshot = dict(_last_seen)
    _save(snapshot)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(now.tzinfo)
    except Exception:
        return None


def _keys(session_id, task_id):
    keys = [str(k) for k in (session_id, task_id) if k]
    return keys or [""]


def _mark_shown(keys, now):
    with _lock:
        for key in keys:
            _last_shown.pop(key, None)
            _last_shown[key] = now
        _trim(_last_shown)


def _stamp_due(keys, now):
    """True (and marks it) when the agent has not been told the time recently."""
    interval = max(0, _cfg["stamp_interval_seconds"])
    with _lock:
        seen = [_last_shown[k] for k in keys if k in _last_shown]
        if seen and (now - max(seen)).total_seconds() < interval:
            return False
        for key in keys:
            _last_shown.pop(key, None)
            _last_shown[key] = now
        _trim(_last_shown)
    return True


def inject_time(session_id=None, task_id=None, **kwargs):
    try:
        now = _now()
        text = f"[Current time: {_format(now)}"
        if _cfg["show_elapsed"] and session_id:
            prev = _touch(session_id, now)
            if prev is not None:
                gap = (now - prev).total_seconds()
                text += f" | previous message: {_ago(gap)} ({_when(now, prev)})"
        _mark_shown(_keys(session_id, task_id), now)
        return {"context": text + "]"}
    except Exception:
        return None


def mark_reply(session_id=None, **kwargs):
    try:
        if session_id:
            _touch(session_id, _now())
    except Exception:
        pass
    return None


def stamp_tool_result(result=None, session_id=None, task_id=None, **kwargs):
    try:
        if not isinstance(result, str):
            return None
        now = _now()
        if not _stamp_due(_keys(session_id, task_id), now):
            return None
        stamp = _format(now)
        try:
            data = json.loads(result)
        except Exception:
            data = None
        if isinstance(data, dict):
            data["_current_time"] = stamp
            return json.dumps(data, ensure_ascii=False)
        return f"{result}\n[Current time: {stamp}]"
    except Exception:
        return None


def _load_config(ctx):
    _cfg.update(_DEFAULTS)
    getter = getattr(ctx, "get_config", None)
    if getter is None:
        return
    for key, default in _DEFAULTS.items():
        try:
            value = getter(key, default=default)
        except Exception:
            continue
        if type(value) is type(default):
            _cfg[key] = value


def register(ctx):
    global _state
    _load_config(ctx)
    _state = getattr(ctx, "state", None)
    _restore()
    ctx.register_hook("pre_llm_call", inject_time)
    if _cfg["show_elapsed"]:
        ctx.register_hook("post_llm_call", mark_reply)
    if _cfg["stamp_tool_results"]:
        ctx.register_hook("transform_tool_result", stamp_tool_result)
