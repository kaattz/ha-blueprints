"""Native persistence helpers for the sun-curtain learning app."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

if __package__:
    from .sun_curtain_learning_core import analyze, parse_jsonl
else:
    from sun_curtain_learning_core import analyze, parse_jsonl


_SAMPLE_PATTERN = re.compile(
    r"^sun_curtain_lux_samples-(\d{4}-\d{2}-\d{2})\.jsonl$"
)
_MARKER_NAME = "sun_curtain_internal_write.json"
_NOTIFICATION_NAME = "sun_curtain_notification_claims.json"
_CANDIDATE_NAME = "sun_curtain_candidate_history.json"
_PENDING_NOTIFICATION_NAME = "sun_curtain_pending_notification.json"
_STORE_LOCK = threading.RLock()


class StorageLimitError(RuntimeError):
    """Raised when a persisted sample file would exceed its limit."""


def analyze_sample_files(
    data_dir: Path,
    *,
    now: datetime,
    current_threshold: float,
    previous_candidates: list[float],
) -> dict[str, object]:
    text = load_sample_text(data_dir, now=now)
    parsed = parse_jsonl(text, now=now)
    return asdict(
        analyze(
            parsed.samples,
            now=now,
            current_threshold=current_threshold,
            previous_candidates=previous_candidates,
        )
    )


def append_sample(
    data_dir: Path, sample: dict[str, object], *, max_file_bytes: int = 5_000_000
) -> Path:
    directory = _ensure_directory(data_dir)
    timestamp = datetime.fromisoformat(str(sample["timestamp"]))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    if sample.get("schema_version") != 1:
        raise ValueError("unsupported_schema")

    line = json.dumps(
        sample,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    encoded = line.encode("utf-8")
    path = directory / f"sun_curtain_lux_samples-{timestamp.date().isoformat()}.jsonl"

    with _STORE_LOCK:
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + len(encoded) > max_file_bytes:
            raise StorageLimitError("sample_file_size_limit")
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return path


def load_sample_text(
    data_dir: Path,
    *,
    now: datetime,
    retention_days: int = 30,
    max_total_bytes: int = 50_000_000,
) -> str:
    directory = Path(data_dir)
    if not directory.exists():
        return ""
    cutoff = now.date() - timedelta(days=retention_days)
    chunks: list[str] = []
    with _STORE_LOCK:
        total_bytes = 0
        for path in sorted(directory.glob("sun_curtain_lux_samples-*.jsonl")):
            file_date = _sample_file_date(path)
            if file_date is None or not cutoff <= file_date <= now.date():
                continue
            total_bytes += path.stat().st_size
            if total_bytes > max_total_bytes:
                raise StorageLimitError("sample_total_size_limit")
            chunks.append(path.read_text(encoding="utf-8"))
    return "".join(chunks)


def prune_sample_files(
    data_dir: Path, *, now: datetime, retention_days: int = 30
) -> list[Path]:
    directory = Path(data_dir)
    if not directory.exists():
        return []
    cutoff = now.date() - timedelta(days=retention_days)
    removed: list[Path] = []
    with _STORE_LOCK:
        for path in sorted(directory.glob("sun_curtain_lux_samples-*.jsonl")):
            file_date = _sample_file_date(path)
            if file_date is not None and file_date < cutoff:
                path.unlink()
                removed.append(path)
    return removed


def write_internal_marker(
    data_dir: Path, *, value: float, now: datetime, ttl_seconds: int
) -> None:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    directory = _ensure_directory(data_dir)
    _atomic_write_json(
        directory / _MARKER_NAME,
        {
            "value": float(value),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        },
    )


def consume_matching_marker(
    data_dir: Path, *, value: float, now: datetime
) -> bool:
    path = Path(data_dir) / _MARKER_NAME
    with _STORE_LOCK:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected = float(payload["value"])
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return False
        path.unlink(missing_ok=True)
    return now <= expires_at and float(value) == expected


def clear_internal_marker(data_dir: Path) -> None:
    with _STORE_LOCK:
        (Path(data_dir) / _MARKER_NAME).unlink(missing_ok=True)


def claim_daily_notification(data_dir: Path, reason_code: str, *, now: datetime) -> bool:
    if not reason_code:
        raise ValueError("reason_code_required")
    directory = _ensure_directory(data_dir)
    path = directory / _NOTIFICATION_NAME
    with _STORE_LOCK:
        claims = _read_json_object(path)
        today = now.date().isoformat()
        if claims.get(reason_code) == today:
            return False
        claims[reason_code] = today
        _atomic_write_json_unlocked(path, claims)
    return True


def record_candidate(data_dir: Path, *, value: float, now: datetime) -> None:
    directory = _ensure_directory(data_dir)
    path = directory / _CANDIDATE_NAME
    with _STORE_LOCK:
        payload = _read_json_list(path)
        today = now.date()
        retained = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("candidate_history_invalid")
            timestamp = datetime.fromisoformat(str(item["timestamp"]))
            if timestamp.date() != today:
                retained.append(item)
        retained.append({"value": float(value), "timestamp": now.isoformat()})
        retained.sort(key=lambda item: str(item["timestamp"]))
        _atomic_write_json_unlocked(path, retained[-7:])


def load_candidates(data_dir: Path, *, now: datetime) -> list[float]:
    path = Path(data_dir) / _CANDIDATE_NAME
    with _STORE_LOCK:
        payload = _read_json_list(path)
    by_date: dict[date, float] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("candidate_history_invalid")
        timestamp = datetime.fromisoformat(str(item["timestamp"]))
        if timestamp.tzinfo is None:
            raise ValueError("candidate_timestamp_missing_timezone")
        if timestamp.date() < now.date():
            by_date[timestamp.date()] = float(item["value"])

    expected = now.date() - timedelta(days=1)
    recent_reversed: list[float] = []
    while expected in by_date and len(recent_reversed) < 3:
        recent_reversed.append(by_date[expected])
        expected -= timedelta(days=1)
    return list(reversed(recent_reversed))


def clear_candidates(data_dir: Path) -> None:
    with _STORE_LOCK:
        (Path(data_dir) / _CANDIDATE_NAME).unlink(missing_ok=True)


def write_pending_notification(data_dir: Path, payload: dict[str, str]) -> None:
    required = {"title", "message", "level"}
    if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
        raise ValueError("pending_notification_invalid")
    directory = _ensure_directory(data_dir)
    _atomic_write_json(directory / _PENDING_NOTIFICATION_NAME, payload)


def load_pending_notification(data_dir: Path) -> dict[str, str] | None:
    path = Path(data_dir) / _PENDING_NOTIFICATION_NAME
    if not path.exists():
        return None
    with _STORE_LOCK:
        payload = _read_json_object(path)
    required = {"title", "message", "level"}
    if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
        raise ValueError("pending_notification_invalid")
    return {key: payload[key] for key in required}


def clear_pending_notification(data_dir: Path) -> None:
    with _STORE_LOCK:
        (Path(data_dir) / _PENDING_NOTIFICATION_NAME).unlink(missing_ok=True)


def _ensure_directory(data_dir: Path) -> Path:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sample_file_date(path: Path) -> date | None:
    match = _SAMPLE_PATTERN.match(path.name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _atomic_write_json(path: Path, payload: object) -> None:
    with _STORE_LOCK:
        _atomic_write_json_unlocked(path, payload)


def _atomic_write_json_unlocked(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}_invalid")
    return payload


def _read_json_list(path: Path) -> list[object]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path.name}_invalid")
    return payload
