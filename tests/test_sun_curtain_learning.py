from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pyscript_modules.sun_curtain_learning_core import (
    AmbiguousDataError,
    DataCorruptionError,
    analyze,
    calculate_confidence,
    cluster_threshold,
    detect_change_events,
    filter_eligible,
    is_stable,
    limited_threshold,
    parse_jsonl,
    resample_minutes,
)
from pyscript_modules.sun_curtain_learning_store import (
    StorageLimitError,
    analyze_sample_files,
    append_sample,
    claim_daily_notification,
    clear_candidates,
    clear_internal_marker,
    clear_pending_notification,
    consume_matching_marker,
    load_candidates,
    load_pending_notification,
    load_sample_text,
    prune_sample_files,
    record_candidate,
    write_pending_notification,
    write_internal_marker,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sun_curtain_learning"
ROOT_DIR = Path(__file__).parents[1]
NOW = datetime.fromisoformat("2026-08-17T21:00:00+08:00")


def load_fixture(name: str, *, max_bad_fraction: float = 0.01):
    return parse_jsonl(
        (FIXTURE_DIR / name).read_text(encoding="utf-8"),
        now=NOW,
        max_bad_fraction=max_bad_fraction,
    )


def test_parse_jsonl_isolates_bad_rows_when_within_allowed_fraction():
    result = load_fixture("corrupt_rows.jsonl", max_bad_fraction=0.75)

    assert len(result.samples) == 1
    assert result.bad_line_count == 3


def test_parse_jsonl_fails_when_bad_rows_exceed_one_percent():
    with pytest.raises(DataCorruptionError, match="bad_line_fraction"):
        load_fixture("corrupt_rows.jsonl")


def test_parse_jsonl_ignores_expired_valid_rows_without_counting_corruption():
    current = json.loads((FIXTURE_DIR / "clear_day.jsonl").read_text().splitlines()[0])
    expired = dict(current)
    expired["timestamp"] = "2026-07-01T12:00:00+08:00"

    result = parse_jsonl(
        "\n".join((json.dumps(expired), json.dumps(current))),
        now=NOW,
        max_bad_fraction=0,
    )

    assert len(result.samples) == 1
    assert result.bad_line_count == 0


def test_filter_eligible_excludes_cover_movement_and_closed_samples():
    parsed = load_fixture("cover_transition.jsonl")

    eligible = filter_eligible(parsed.samples)

    assert [sample.lux for sample in eligible] == [12000]


def test_resample_and_change_detection_find_sustained_level_shift():
    parsed = load_fixture("clear_day.jsonl")
    minutes = resample_minutes(parsed.samples)

    events = detect_change_events(minutes)

    assert len(events) == 1
    assert events[0].threshold == pytest.approx(8500)
    assert events[0].before_lux == pytest.approx(5000)
    assert events[0].after_lux == pytest.approx(12000)


def test_change_detection_does_not_cross_ineligible_cover_boundary():
    parsed = load_fixture("clear_day.jsonl")
    boundary = replace(
        parsed.samples[2],
        eligible=False,
        exclude_reason="cover_moving",
        cover_state="closing",
        cover_position=70,
    )
    samples = [parsed.samples[0], parsed.samples[1], boundary, parsed.samples[3]]

    events = detect_change_events(resample_minutes(samples))

    assert events == []


def test_cluster_threshold_is_deterministic_for_separated_values():
    values = [5000] * 10 + [12000] * 10

    first = cluster_threshold(values)
    second = cluster_threshold(list(reversed(values)))

    assert first == pytest.approx(8500)
    assert second == first


def test_cluster_threshold_rejects_indistinguishable_values():
    parsed = load_fixture("cloudy_overlap.jsonl")

    with pytest.raises(AmbiguousDataError, match="empty_cluster"):
        cluster_threshold([sample.lux for sample in parsed.samples])


def test_confidence_uses_weakest_gate_instead_of_average():
    score = calculate_confidence(
        valid_days=14,
        event_count=10,
        indirect_p95=5000,
        direct_p05=12000,
        change_threshold=8500,
        cluster_threshold_value=8925,
    )

    expected_agreement = 1 - (8925 - 8500) / ((8925 + 8500) / 2)
    assert score == pytest.approx(expected_agreement)


def test_stability_requires_three_candidates_within_five_percent():
    assert is_stable([8000, 8100, 8200]) is True
    assert is_stable([8000, 8200]) is False
    assert is_stable([8000, 8500, 9000]) is False


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        (8000, 12000, 9600),
        (8000, 5000, 6400),
        (8000, 8550, 8600),
    ],
)
def test_limited_threshold_rounds_to_hundreds_and_caps_change(
    current: float, candidate: float, expected: int
):
    assert limited_threshold(current, candidate) == expected


def test_analyze_requires_fourteen_days_and_ten_events():
    parsed = load_fixture("clear_day.jsonl")

    result = analyze(
        parsed.samples,
        now=NOW,
        current_threshold=8000,
        previous_candidates=[8500, 8500],
    )

    assert result.status == "insufficient_data"
    assert result.reason_code == "valid_days_below_14"
    assert result.applied_lux is None


def test_analyze_returns_ready_for_stable_separated_fourteen_day_dataset():
    parsed = load_fixture("clear_day.jsonl")
    samples = []
    for day_offset in range(14):
        samples.extend(
            replace(sample, timestamp=sample.timestamp + timedelta(days=day_offset))
            for sample in parsed.samples
        )

    result = analyze(
        samples,
        now=NOW,
        current_threshold=8000,
        previous_candidates=[8500, 8500],
    )

    assert result.status == "ready"
    assert result.reason_code == "ready"
    assert result.candidate_lux == pytest.approx(8500)
    assert result.applied_lux == 8500
    assert result.confidence >= 0.95
    assert result.valid_days == 14
    assert result.event_count == 14


def test_analyze_clusters_only_event_candidates_not_unrelated_third_level():
    parsed = load_fixture("clear_day.jsonl")
    samples = []
    for day_offset in range(14):
        shifted = [
            replace(sample, timestamp=sample.timestamp + timedelta(days=day_offset))
            for sample in parsed.samples
        ]
        samples.extend(shifted)
        samples.extend(
            (
                replace(
                    shifted[-1],
                    timestamp=shifted[-1].timestamp.replace(hour=14, minute=0),
                    lux=30000,
                ),
                replace(
                    shifted[-1],
                    timestamp=shifted[-1].timestamp.replace(hour=14, minute=9),
                    lux=30000,
                ),
            )
        )

    result = analyze(
        samples,
        now=NOW,
        current_threshold=8000,
        previous_candidates=[8500, 8500],
    )

    assert result.status == "ready"
    assert result.candidate_lux == pytest.approx(8500)


def test_store_appends_versioned_jsonl_and_loads_retained_files(tmp_path: Path):
    sample = json.loads((FIXTURE_DIR / "clear_day.jsonl").read_text().splitlines()[0])

    path = append_sample(tmp_path, sample)
    loaded = load_sample_text(tmp_path, now=NOW)

    assert path.name == "sun_curtain_lux_samples-2026-08-01.jsonl"
    assert loaded.count("\n") == 1
    assert json.loads(loaded) == sample


def test_store_analysis_entrypoint_returns_serializable_result(tmp_path: Path):
    fixture_rows = [
        json.loads(line)
        for line in (FIXTURE_DIR / "clear_day.jsonl").read_text().splitlines()
    ]
    for day_offset in range(14):
        for row in fixture_rows:
            shifted = dict(row)
            shifted["timestamp"] = (
                datetime.fromisoformat(row["timestamp"]) + timedelta(days=day_offset)
            ).isoformat()
            append_sample(tmp_path, shifted)

    result = analyze_sample_files(
        tmp_path,
        now=NOW,
        current_threshold=8000,
        previous_candidates=[8500, 8500],
    )

    assert result["status"] == "ready"
    assert result["candidate_lux"] == pytest.approx(8500)
    json.dumps(result)


def test_store_rejects_append_that_would_exceed_file_limit(tmp_path: Path):
    sample = json.loads((FIXTURE_DIR / "clear_day.jsonl").read_text().splitlines()[0])

    with pytest.raises(StorageLimitError, match="sample_file_size_limit"):
        append_sample(tmp_path, sample, max_file_bytes=10)


def test_store_rejects_analysis_input_above_total_size_limit(tmp_path: Path):
    path = tmp_path / "sun_curtain_lux_samples-2026-08-01.jsonl"
    path.write_text("{}\n" * 10, encoding="utf-8")

    with pytest.raises(StorageLimitError, match="sample_total_size_limit"):
        load_sample_text(tmp_path, now=NOW, max_total_bytes=10)


def test_store_prunes_only_sample_files_older_than_retention(tmp_path: Path):
    old = tmp_path / "sun_curtain_lux_samples-2026-07-01.jsonl"
    retained = tmp_path / "sun_curtain_lux_samples-2026-08-01.jsonl"
    unrelated = tmp_path / "keep-me.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    retained.write_text("{}\n", encoding="utf-8")
    unrelated.write_text("{}\n", encoding="utf-8")

    removed = prune_sample_files(tmp_path, now=NOW, retention_days=30)

    assert removed == [old]
    assert retained.exists()
    assert unrelated.exists()


def test_internal_marker_matches_once_and_mismatch_fails_closed(tmp_path: Path):
    write_internal_marker(tmp_path, value=7600, now=NOW, ttl_seconds=10)

    assert consume_matching_marker(tmp_path, value=7600, now=NOW + timedelta(seconds=5))
    assert not consume_matching_marker(tmp_path, value=7600, now=NOW + timedelta(seconds=6))

    write_internal_marker(tmp_path, value=7600, now=NOW, ttl_seconds=10)
    assert not consume_matching_marker(tmp_path, value=7700, now=NOW + timedelta(seconds=5))


def test_internal_marker_can_be_cleared_after_failed_update(tmp_path: Path):
    write_internal_marker(tmp_path, value=7600, now=NOW, ttl_seconds=10)

    clear_internal_marker(tmp_path)

    assert not consume_matching_marker(tmp_path, value=7600, now=NOW + timedelta(seconds=1))


def test_notification_reason_is_claimed_once_per_local_day(tmp_path: Path):
    assert claim_daily_notification(tmp_path, "storage_error", now=NOW)
    assert not claim_daily_notification(tmp_path, "storage_error", now=NOW)
    assert claim_daily_notification(
        tmp_path, "storage_error", now=NOW + timedelta(days=1)
    )


def test_candidate_history_keeps_only_three_most_recent_values(tmp_path: Path):
    for index, value in enumerate((7000, 7200, 7400, 7600)):
        record_candidate(tmp_path, value=value, now=NOW + timedelta(days=index))

    assert load_candidates(tmp_path, now=NOW + timedelta(days=4)) == [7200, 7400, 7600]


def test_candidate_history_requires_fresh_consecutive_previous_days(tmp_path: Path):
    record_candidate(tmp_path, value=7000, now=NOW - timedelta(days=10))
    record_candidate(tmp_path, value=7200, now=NOW - timedelta(days=9))
    record_candidate(tmp_path, value=7600, now=NOW - timedelta(days=1))

    assert load_candidates(tmp_path, now=NOW) == [7600]


def test_candidate_history_replaces_same_day_and_can_be_cleared(tmp_path: Path):
    record_candidate(tmp_path, value=7000, now=NOW - timedelta(days=1))
    record_candidate(tmp_path, value=7600, now=NOW - timedelta(days=1, hours=-1))

    assert load_candidates(tmp_path, now=NOW) == [7600]
    clear_candidates(tmp_path)
    assert load_candidates(tmp_path, now=NOW) == []


def test_pending_notification_round_trip_and_clear(tmp_path: Path):
    payload = {"title": "updated", "message": "threshold changed", "level": "info"}

    write_pending_notification(tmp_path, payload)
    assert load_pending_notification(tmp_path) == payload
    clear_pending_notification(tmp_path)
    assert load_pending_notification(tmp_path) is None


def test_pyscript_adapter_uses_app_config_executor_and_no_hardcoded_entities():
    adapter = (
        ROOT_DIR / "pyscript" / "apps" / "sun_curtain_learning" / "__init__.py"
    ).read_text(encoding="utf-8")
    config = (
        ROOT_DIR / "pyscript" / "sun_curtain_learning.config.example.yaml"
    ).read_text(encoding="utf-8")

    assert "pyscript.app_config" in adapter
    assert "@state_trigger" in adapter
    assert "@time_trigger" in adapter
    assert "@service" in adapter
    assert "task.executor" in adapter
    assert "state.get(" in adapter
    assert "service.call(" in adapter
    assert 'task.unique("sun_curtain_learning_analysis")' in adapter
    assert adapter.count("_state(LEARNING_SWITCH)") >= 2
    assert adapter.count("_state(THRESHOLD_HELPER)") >= 3
    assert "threshold_unchanged" in adapter
    assert "notification_failed" in adapter
    assert "write_pending_notification" in adapter
    assert "clear_candidates" in adapter
    assert "sensor.livingroom_balcony_lux" not in adapter
    assert "cover.livingroom_curtain" not in adapter
    for required_key in (
        "lux_sensor",
        "learning_cover",
        "weather_entity",
        "automation_switch",
        "artificial_light_guard",
        "threshold_helper",
        "learning_switch",
        "notify_script",
        "window_azimuth",
        "azimuth_range",
    ):
        assert f"{required_key}:" in config
    assert config.startswith("allow_all_imports: true\n")


def test_store_imports_from_deployed_top_level_module_directory():
    module_dir = ROOT_DIR / "pyscript_modules"
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(module_dir)!r}); "
        "import sun_curtain_learning_store"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
