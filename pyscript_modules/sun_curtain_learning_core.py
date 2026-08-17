"""Pure learning core for the sun-curtain illuminance threshold."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence


class DataCorruptionError(ValueError):
    """Raised when persisted sample corruption exceeds the safe limit."""


class AmbiguousDataError(ValueError):
    """Raised when samples cannot be separated reliably."""


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    lux: float
    sun_azimuth: float
    sun_elevation: float
    weather_state: str
    cover_state: str
    cover_position: float
    automation_enabled: bool
    artificial_light_guard: str
    eligible: bool
    exclude_reason: str | None


@dataclass(frozen=True)
class ParseResult:
    samples: tuple[Sample, ...]
    bad_line_count: int


@dataclass(frozen=True)
class ChangeEvent:
    timestamp: datetime
    threshold: float
    before_lux: float
    after_lux: float


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    reason_code: str
    candidate_lux: float | None
    applied_lux: int | None
    confidence: float
    valid_days: int
    event_count: int


def parse_jsonl(text: str, *, now: datetime, max_bad_fraction: float = 0.01) -> ParseResult:
    if not 0 <= max_bad_fraction <= 1:
        raise ValueError("max_bad_fraction must be between 0 and 1")

    samples: list[Sample] = []
    bad_line_count = 0
    lines = [line for line in text.splitlines() if line.strip()]
    oldest = now - timedelta(days=30)

    for line in lines:
        try:
            payload = json.loads(line)
            sample = _sample_from_payload(payload)
            if sample.timestamp < oldest:
                continue
            if sample.timestamp > now:
                raise ValueError("timestamp_in_future")
            samples.append(sample)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            bad_line_count += 1

    total_count = len(lines)
    bad_fraction = bad_line_count / total_count if total_count else 0
    if bad_fraction > max_bad_fraction:
        raise DataCorruptionError(
            f"bad_line_fraction={bad_fraction:.4f} exceeds {max_bad_fraction:.4f}"
        )

    samples.sort(key=lambda sample: sample.timestamp)
    return ParseResult(tuple(samples), bad_line_count)


def filter_eligible(samples: Iterable[Sample]) -> list[Sample]:
    return [sample for sample in samples if _sample_is_eligible(sample)]


def resample_minutes(samples: Sequence[Sample]) -> list[Sample]:
    if not samples:
        return []

    ordered = sorted(samples, key=lambda sample: sample.timestamp)
    by_day: dict[object, list[Sample]] = {}
    for sample in ordered:
        by_day.setdefault(sample.timestamp.date(), []).append(sample)

    result: list[Sample] = []
    for day_samples in by_day.values():
        start = _floor_minute(day_samples[0].timestamp)
        end = _floor_minute(day_samples[-1].timestamp)
        sample_index = 0
        current: Sample | None = None
        minute = start

        while minute <= end:
            while (
                sample_index < len(day_samples)
                and day_samples[sample_index].timestamp <= minute
            ):
                current = day_samples[sample_index]
                sample_index += 1

            if current is not None and minute - current.timestamp <= timedelta(minutes=30):
                result.append(
                    Sample(
                        timestamp=minute,
                        lux=current.lux,
                        sun_azimuth=current.sun_azimuth,
                        sun_elevation=current.sun_elevation,
                        weather_state=current.weather_state,
                        cover_state=current.cover_state,
                        cover_position=current.cover_position,
                        automation_enabled=current.automation_enabled,
                        artificial_light_guard=current.artificial_light_guard,
                        eligible=current.eligible,
                        exclude_reason=current.exclude_reason,
                    )
                )
            minute += timedelta(minutes=1)

    return result


def detect_change_events(samples: Sequence[Sample]) -> list[ChangeEvent]:
    if not samples:
        return []

    by_day: dict[object, list[Sample]] = {}
    for sample in sorted(samples, key=lambda item: item.timestamp):
        by_day.setdefault(sample.timestamp.date(), []).append(sample)

    events: list[ChangeEvent] = []
    for day_samples in by_day.values():
        if len(day_samples) < 20:
            continue

        differences = [
            day_samples[index].lux - day_samples[index - 1].lux
            for index in range(1, len(day_samples))
        ]
        noise_mad = _median_absolute_deviation(differences)

        for index in range(10, len(day_samples) - 9):
            before = day_samples[index - 10 : index]
            after = day_samples[index : index + 10]
            if len(before) < 8 or len(after) < 8:
                continue
            if not _minutes_are_contiguous(before + after):
                continue
            if not all(_sample_is_eligible(sample) for sample in before + after):
                continue

            before_lux = statistics.median(sample.lux for sample in before)
            after_lux = statistics.median(sample.lux for sample in after)
            shift = abs(after_lux - before_lux)
            required_shift = max(1000.0, before_lux * 0.20, noise_mad * 6)
            if shift <= required_shift:
                continue

            sustained = after[:5]
            sustained_median = statistics.median(sample.lux for sample in sustained)
            if abs(sustained_median - after_lux) > max(500.0, shift * 0.10):
                continue

            events.append(
                ChangeEvent(
                    timestamp=day_samples[index].timestamp,
                    threshold=(before_lux + after_lux) / 2,
                    before_lux=before_lux,
                    after_lux=after_lux,
                )
            )
            break

    return events


def cluster_threshold(values: Sequence[float]) -> float:
    low_cluster, high_cluster = _cluster_values(values)
    indirect_p95 = _percentile(low_cluster, 0.95)
    direct_p05 = _percentile(high_cluster, 0.05)
    if indirect_p95 >= direct_p05:
        raise AmbiguousDataError("overlapping_clusters")
    return (indirect_p95 + direct_p05) / 2


def calculate_confidence(
    *,
    valid_days: int,
    event_count: int,
    indirect_p95: float,
    direct_p05: float,
    change_threshold: float,
    cluster_threshold_value: float,
) -> float:
    coverage_score = min(max(valid_days / 14, 0), 1)
    event_score = min(max(event_count / 10, 0), 1)
    gap = direct_p05 - indirect_p95
    required_gap = max(1000.0, direct_p05 * 0.10)
    separation_score = _clamp(gap / required_gap, 0, 1)
    midpoint = max((change_threshold + cluster_threshold_value) / 2, 1)
    agreement_score = _clamp(
        1 - abs(change_threshold - cluster_threshold_value) / midpoint,
        0,
        1,
    )
    return min(coverage_score, event_score, separation_score, agreement_score)


def is_stable(candidates: Sequence[float]) -> bool:
    if len(candidates) < 3:
        return False
    recent = list(candidates[-3:])
    median = statistics.median(recent)
    if median <= 0:
        return False
    return (max(recent) - min(recent)) / median <= 0.05


def limited_threshold(current: float, candidate: float) -> int:
    _require_finite_number(current, "current")
    _require_finite_number(candidate, "candidate")
    if current <= 0:
        raise ValueError("current must be positive")
    if not 1000 <= candidate <= 50000:
        raise ValueError("candidate_out_of_range")

    limited = _clamp(candidate, current * 0.8, current * 1.2)
    return int(round(limited / 100) * 100)


def analyze(
    samples: Sequence[Sample],
    *,
    now: datetime,
    current_threshold: float,
    previous_candidates: Sequence[float],
) -> AnalysisResult:
    retained = [
        sample
        for sample in samples
        if now - timedelta(days=30) <= sample.timestamp <= now
    ]
    eligible = filter_eligible(retained)
    valid_days = len({sample.timestamp.date() for sample in eligible})
    if valid_days < 14:
        return _analysis_failure(
            "insufficient_data", "valid_days_below_14", valid_days=valid_days
        )

    minutes = resample_minutes(retained)
    events = detect_change_events(minutes)
    event_count = len(events)
    if event_count < 10:
        return _analysis_failure(
            "insufficient_data",
            "event_count_below_10",
            valid_days=valid_days,
            event_count=event_count,
        )

    change_threshold_value = statistics.median(event.threshold for event in events)
    try:
        event_values = [
            value
            for event in events
            for value in (min(event.before_lux, event.after_lux), max(event.before_lux, event.after_lux))
        ]
        low_cluster, high_cluster = _cluster_values(event_values)
    except AmbiguousDataError as error:
        return _analysis_failure(
            "ambiguous",
            str(error),
            valid_days=valid_days,
            event_count=event_count,
        )

    indirect_p95 = _percentile(low_cluster, 0.95)
    direct_p05 = _percentile(high_cluster, 0.05)
    if indirect_p95 >= direct_p05:
        return _analysis_failure(
            "ambiguous",
            "overlapping_clusters",
            valid_days=valid_days,
            event_count=event_count,
        )

    cluster_threshold_value = (indirect_p95 + direct_p05) / 2
    candidate = (change_threshold_value + cluster_threshold_value) / 2
    confidence = calculate_confidence(
        valid_days=valid_days,
        event_count=event_count,
        indirect_p95=indirect_p95,
        direct_p05=direct_p05,
        change_threshold=change_threshold_value,
        cluster_threshold_value=cluster_threshold_value,
    )
    if confidence < 0.95:
        return AnalysisResult(
            status="ambiguous",
            reason_code="confidence_below_95",
            candidate_lux=candidate,
            applied_lux=None,
            confidence=confidence,
            valid_days=valid_days,
            event_count=event_count,
        )
    if not is_stable([*previous_candidates, candidate]):
        return AnalysisResult(
            status="learning",
            reason_code="candidate_not_stable",
            candidate_lux=candidate,
            applied_lux=None,
            confidence=confidence,
            valid_days=valid_days,
            event_count=event_count,
        )

    return AnalysisResult(
        status="ready",
        reason_code="ready",
        candidate_lux=candidate,
        applied_lux=limited_threshold(current_threshold, candidate),
        confidence=confidence,
        valid_days=valid_days,
        event_count=event_count,
    )


def _sample_from_payload(payload: object) -> Sample:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported_schema")

    timestamp = datetime.fromisoformat(str(payload["timestamp"]))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")

    lux = _finite_float(payload["lux"], "lux")
    sun_azimuth = _finite_float(payload["sun_azimuth"], "sun_azimuth")
    sun_elevation = _finite_float(payload["sun_elevation"], "sun_elevation")
    cover_position = _finite_float(payload["cover_position"], "cover_position")
    if lux < 0:
        raise ValueError("lux_negative")

    automation_enabled = payload["automation_enabled"]
    eligible = payload["eligible"]
    if not isinstance(automation_enabled, bool) or not isinstance(eligible, bool):
        raise TypeError("boolean_field_invalid")

    exclude_reason = payload.get("exclude_reason")
    if exclude_reason is not None and not isinstance(exclude_reason, str):
        raise TypeError("exclude_reason_invalid")

    return Sample(
        timestamp=timestamp,
        lux=lux,
        sun_azimuth=sun_azimuth,
        sun_elevation=sun_elevation,
        weather_state=_required_string(payload, "weather_state"),
        cover_state=_required_string(payload, "cover_state"),
        cover_position=cover_position,
        automation_enabled=automation_enabled,
        artificial_light_guard=_required_string(payload, "artificial_light_guard"),
        eligible=eligible,
        exclude_reason=exclude_reason,
    )


def _cluster_values(values: Sequence[float]) -> tuple[list[float], list[float]]:
    clean_values = [_finite_float(value, "cluster_value") for value in values]
    if len(clean_values) < 2:
        raise AmbiguousDataError("insufficient_cluster_values")

    low_center = min(clean_values)
    high_center = max(clean_values)
    if low_center == high_center:
        raise AmbiguousDataError("empty_cluster")

    low_cluster: list[float] = []
    high_cluster: list[float] = []
    for _ in range(100):
        low_cluster = []
        high_cluster = []
        for value in clean_values:
            if abs(value - low_center) <= abs(value - high_center):
                low_cluster.append(value)
            else:
                high_cluster.append(value)
        if not low_cluster or not high_cluster:
            raise AmbiguousDataError("empty_cluster")

        new_low = statistics.fmean(low_cluster)
        new_high = statistics.fmean(high_cluster)
        if abs(new_low - low_center) <= 1 and abs(new_high - high_center) <= 1:
            break
        low_center = new_low
        high_center = new_high

    if statistics.fmean(low_cluster) > statistics.fmean(high_cluster):
        low_cluster, high_cluster = high_cluster, low_cluster
    return sorted(low_cluster), sorted(high_cluster)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise AmbiguousDataError("empty_percentile")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def _median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0
    median = statistics.median(values)
    return statistics.median(abs(value - median) for value in values)


def _minutes_are_contiguous(samples: Sequence[Sample]) -> bool:
    return all(
        current.timestamp - previous.timestamp == timedelta(minutes=1)
        for previous, current in zip(samples, samples[1:])
    )


def _sample_is_eligible(sample: Sample) -> bool:
    return (
        sample.eligible
        and sample.exclude_reason is None
        and sample.automation_enabled
        and sample.cover_state == "open"
        and sample.cover_position >= 99
        and sample.artificial_light_guard == "off"
    )


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key}_invalid")
    return value


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name}_invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}_not_finite")
    return number


def _require_finite_number(value: object, name: str) -> None:
    _finite_float(value, name)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def _analysis_failure(
    status: str,
    reason_code: str,
    *,
    valid_days: int,
    event_count: int = 0,
) -> AnalysisResult:
    return AnalysisResult(
        status=status,
        reason_code=reason_code,
        candidate_lux=None,
        applied_lux=None,
        confidence=0,
        valid_days=valid_days,
        event_count=event_count,
    )
