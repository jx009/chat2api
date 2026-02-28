import json
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any


CODEX_USAGE_FILE = os.path.join("data", "codex_usage.json")
TOKEN_CONFIG_FILE = os.path.join("data", "token_config.json")

_codex_usage_map: Dict[str, Dict[str, Any]] = {}
_token_config_map: Dict[str, Dict[str, Any]] = {}
_UNSET = object()


def _normalize_expires_at(expires_at: Optional[str]) -> Optional[str]:

    if expires_at is None:
        return None

    value = str(expires_at).strip()
    if not value:
        return None

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except Exception as exc:
        raise ValueError("Invalid expires_at format, expected ISO 8601 datetime") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _ensure_data_dir():
    os.makedirs("data", exist_ok=True)


def _load_json_file(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _persist_json_file(path: str, data: Dict[str, Any]):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _parse_float(headers: Dict[str, Any], key: str) -> Optional[float]:
    value = headers.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_int(headers: Dict[str, Any], key: str) -> Optional[int]:
    value = headers.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def extract_codex_usage_headers(headers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从上游响应头提取 x-codex-* 字段。"""
    normalized_headers = {str(k).lower(): v for k, v in headers.items()}
    snapshot: Dict[str, Any] = {}
    has_data = False

    if (v := _parse_float(normalized_headers, "x-codex-primary-used-percent")) is not None:
        snapshot["primary_used_percent"] = v
        has_data = True
    if (v := _parse_int(normalized_headers, "x-codex-primary-reset-after-seconds")) is not None:
        snapshot["primary_reset_after_seconds"] = v
        has_data = True
    if (v := _parse_int(normalized_headers, "x-codex-primary-window-minutes")) is not None:
        snapshot["primary_window_minutes"] = v
        has_data = True

    if (v := _parse_float(normalized_headers, "x-codex-secondary-used-percent")) is not None:
        snapshot["secondary_used_percent"] = v
        has_data = True
    if (v := _parse_int(normalized_headers, "x-codex-secondary-reset-after-seconds")) is not None:
        snapshot["secondary_reset_after_seconds"] = v
        has_data = True
    if (v := _parse_int(normalized_headers, "x-codex-secondary-window-minutes")) is not None:
        snapshot["secondary_window_minutes"] = v
        has_data = True

    if (v := _parse_float(normalized_headers, "x-codex-primary-over-secondary-limit-percent")) is not None:
        snapshot["primary_over_secondary_percent"] = v
        has_data = True

    if not has_data:
        return None

    snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
    return snapshot


def normalize_codex_windows(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """将 primary/secondary 窗口标准化映射为 5h/7d 字段。"""
    result = dict(snapshot)

    primary_window = snapshot.get("primary_window_minutes")
    secondary_window = snapshot.get("secondary_window_minutes")

    use_5h_from_primary = False
    use_7d_from_primary = False
    use_5h_from_secondary = False
    use_7d_from_secondary = False

    if primary_window is not None and secondary_window is not None:
        if primary_window < secondary_window:
            use_5h_from_primary = True
            use_7d_from_secondary = True
        else:
            use_5h_from_secondary = True
            use_7d_from_primary = True
    elif primary_window is not None:
        if primary_window <= 360:
            use_5h_from_primary = True
        else:
            use_7d_from_primary = True
    elif secondary_window is not None:
        if secondary_window <= 360:
            use_5h_from_secondary = True
        else:
            use_7d_from_secondary = True
    else:
        use_5h_from_secondary = True
        use_7d_from_primary = True

    src_5h = "primary" if use_5h_from_primary else ("secondary" if use_5h_from_secondary else None)
    src_7d = "primary" if use_7d_from_primary else ("secondary" if use_7d_from_secondary else None)

    if src_5h:
        for suffix in ["used_percent", "reset_after_seconds", "window_minutes"]:
            key = f"{src_5h}_{suffix}"
            if key in snapshot:
                result[f"codex_5h_{suffix}"] = snapshot[key]

    if src_7d:
        for suffix in ["used_percent", "reset_after_seconds", "window_minutes"]:
            key = f"{src_7d}_{suffix}"
            if key in snapshot:
                result[f"codex_7d_{suffix}"] = snapshot[key]

    return result


def update_codex_snapshot(token_key: str, snapshot: Dict[str, Any]):
    normalized = normalize_codex_windows(snapshot)
    _codex_usage_map[token_key] = normalized
    _persist_codex_usage()


def get_codex_snapshot(token_key: str) -> Optional[Dict[str, Any]]:
    data = _codex_usage_map.get(token_key)
    return dict(data) if data else None


def get_all_codex_snapshots() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in _codex_usage_map.items()}


def add_token_config(full_token: str, name: str, expires_at: Optional[str] = None) -> str:
    token_key = full_token[:20]
    normalized_expires_at = _normalize_expires_at(expires_at)
    _token_config_map[token_key] = {
        "name": name,
        "full_token": full_token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": normalized_expires_at,
    }
    _persist_token_config()
    return token_key


def update_token_config(token_key: str, name: Optional[str] = None, expires_at: Any = _UNSET) -> bool:
    if token_key not in _token_config_map:
        return False

    if name is not None:
        _token_config_map[token_key]["name"] = name
    if expires_at is not _UNSET:
        _token_config_map[token_key]["expires_at"] = _normalize_expires_at(expires_at)
    _persist_token_config()
    return True


def delete_token_config(token_key: str) -> bool:
    if token_key not in _token_config_map:
        return False
    del _token_config_map[token_key]
    _persist_token_config()

    if token_key in _codex_usage_map:
        del _codex_usage_map[token_key]
        _persist_codex_usage()
    return True


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def is_token_expired(token_key: str, now: Optional[datetime] = None) -> bool:
    config = _token_config_map.get(token_key)
    if not config:
        return False
    expires_at = _parse_iso_datetime(config.get("expires_at"))
    if expires_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return now >= expires_at


def get_expired_token_entries(now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    result: Dict[str, Dict[str, Any]] = {}
    for key, cfg in _token_config_map.items():
        expires_at = _parse_iso_datetime(cfg.get("expires_at"))
        if expires_at and current >= expires_at:
            result[key] = dict(cfg)
    return result


def get_all_token_configs() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in _token_config_map.items()}


def get_token_name(token_key: str) -> Optional[str]:
    config = _token_config_map.get(token_key)
    return config.get("name") if config else None


def get_all_codex_snapshots_with_names() -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    all_keys = set(_codex_usage_map.keys()) | set(_token_config_map.keys())
    for key in all_keys:
        snapshot = dict(_codex_usage_map.get(key, {}))
        token_cfg = _token_config_map.get(key, {})
        snapshot["token_name"] = token_cfg.get("name", "")
        snapshot["token_key"] = key
        result[key] = snapshot
    return result


def _persist_codex_usage():
    _persist_json_file(CODEX_USAGE_FILE, _codex_usage_map)


def _persist_token_config():
    _persist_json_file(TOKEN_CONFIG_FILE, _token_config_map)


def _sanitize_token_config_map() -> bool:
    changed = False
    for key, cfg in list(_token_config_map.items()):
        if not isinstance(cfg, dict):
            _token_config_map[key] = {
                "name": "",
                "full_token": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": None,
            }
            changed = True
            continue

        if "name" not in cfg:
            cfg["name"] = ""
            changed = True
        if "full_token" not in cfg:
            cfg["full_token"] = ""
            changed = True
        if "created_at" not in cfg:
            cfg["created_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        if "expires_at" not in cfg:
            cfg["expires_at"] = None
            changed = True
        else:
            try:
                normalized = _normalize_expires_at(cfg.get("expires_at")) if cfg.get("expires_at") is not None else None
            except ValueError:
                normalized = None
            if cfg.get("expires_at") != normalized:
                cfg["expires_at"] = normalized
                changed = True

    return changed


_ensure_data_dir()
_codex_usage_map = _load_json_file(CODEX_USAGE_FILE)
_token_config_map = _load_json_file(TOKEN_CONFIG_FILE)
if _sanitize_token_config_map():
    _persist_token_config()
