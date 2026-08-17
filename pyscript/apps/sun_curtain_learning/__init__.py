"""Pyscript adapter for sun-curtain threshold learning."""

import sys
from datetime import datetime


NATIVE_MODULE_DIR = "/config/pyscript_modules"
if NATIVE_MODULE_DIR not in sys.path:
    sys.path.append(NATIVE_MODULE_DIR)

from sun_curtain_learning_store import (  # noqa: E402
    analyze_sample_files,
    append_sample,
    claim_daily_notification,
    clear_candidates,
    clear_internal_marker,
    clear_pending_notification,
    consume_matching_marker,
    load_candidates,
    load_pending_notification,
    prune_sample_files,
    record_candidate,
    write_internal_marker,
    write_pending_notification,
)


CONFIG = pyscript.app_config
REQUIRED_CONFIG = (
    "lux_sensor",
    "learning_cover",
    "weather_entity",
    "automation_switch",
    "artificial_light_guard",
    "threshold_helper",
    "learning_switch",
    "notify_script",
    "diagnostic_entity",
    "window_azimuth",
    "azimuth_range",
    "data_dir",
)
MISSING_CONFIG = [key for key in REQUIRED_CONFIG if key not in CONFIG]
if MISSING_CONFIG:
    raise ValueError(f"missing_app_config:{','.join(MISSING_CONFIG)}")

LUX_SENSOR = CONFIG["lux_sensor"]
LEARNING_COVER = CONFIG["learning_cover"]
WEATHER_ENTITY = CONFIG["weather_entity"]
AUTOMATION_SWITCH = CONFIG["automation_switch"]
ARTIFICIAL_LIGHT_GUARD = CONFIG["artificial_light_guard"]
THRESHOLD_HELPER = CONFIG["threshold_helper"]
LEARNING_SWITCH = CONFIG["learning_switch"]
NOTIFY_SCRIPT = CONFIG["notify_script"]
DIAGNOSTIC_ENTITY = CONFIG["diagnostic_entity"]
WINDOW_AZIMUTH = float(CONFIG["window_azimuth"])
AZIMUTH_RANGE = float(CONFIG["azimuth_range"])
DATA_DIR = CONFIG["data_dir"]
RAINY_STATES = {
    "rainy",
    "pouring",
    "lightning-rainy",
    "hail",
    "snowy-rainy",
}


def _now():
    return datetime.now().astimezone()


def _to_float(value):
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _state(entity_id):
    try:
        return state.get(entity_id)
    except NameError:
        return None


def _attributes(entity_id):
    attributes = state.getattr(entity_id)
    return attributes if isinstance(attributes, dict) else {}


def _notify(title, message, level="info"):
    domain, service_name = NOTIFY_SCRIPT.split(".", 1)
    service.call(
        domain,
        service_name,
        blocking=True,
        title=title,
        message=message,
        level=level,
    )


def _notify_once(reason_code, title, message, level="warning"):
    if task.executor(claim_daily_notification, DATA_DIR, reason_code, now=_now()):
        _notify(title, message, level)


def _set_diagnostic(status, attributes=None):
    details = dict(attributes or {})
    details["updated_at"] = _now().isoformat()
    state.set(DIAGNOSTIC_ENTITY, status, new_attributes=details)


def _retry_pending_notification():
    try:
        pending = task.executor(load_pending_notification, DATA_DIR)
        if pending is None:
            return True
        _notify(pending["title"], pending["message"], pending["level"])
        task.executor(clear_pending_notification, DATA_DIR)
        return True
    except Exception as error:
        _set_diagnostic(
            "error",
            {"reason_code": "notification_failed", "notification_error": str(error)},
        )
        return False


@time_trigger
def initialize_learning_status():
    _set_diagnostic("learning", {"reason_code": "initialized"})
    _retry_pending_notification()


@state_trigger(LUX_SENSOR)
def capture_lux_sample(value=None, **kwargs):
    if _state(LEARNING_SWITCH) != "on":
        return

    lux = _to_float(value if value is not None else _state(LUX_SENSOR))
    if lux is None:
        _set_diagnostic("error", {"reason_code": "lux_invalid"})
        return

    now = _now()
    sun_attributes = _attributes("sun.sun")
    cover_attributes = _attributes(LEARNING_COVER)
    azimuth = _to_float(sun_attributes.get("azimuth"))
    elevation = _to_float(sun_attributes.get("elevation"))
    cover_position = _to_float(cover_attributes.get("current_position"))
    cover_state = _state(LEARNING_COVER)
    weather_state = _state(WEATHER_ENTITY)
    automation_enabled = _state(AUTOMATION_SWITCH) == "on"
    light_guard = _state(ARTIFICIAL_LIGHT_GUARD)

    exclude_reason = None
    if azimuth is None or elevation is None:
        exclude_reason = "sun_state_invalid"
    elif cover_state in ("opening", "closing"):
        exclude_reason = "cover_moving"
    elif cover_state != "open" or cover_position is None or cover_position < 99:
        exclude_reason = "cover_not_open"
    elif not automation_enabled:
        exclude_reason = "automation_disabled"
    elif light_guard != "off":
        exclude_reason = "artificial_light_on"
    elif weather_state in (None, "unknown", "unavailable"):
        exclude_reason = "weather_invalid"
    elif weather_state in RAINY_STATES:
        exclude_reason = "rainy"
    elif elevation <= 0:
        exclude_reason = "sun_below_horizon"
    elif abs(((azimuth - WINDOW_AZIMUTH + 540) % 360) - 180) > AZIMUTH_RANGE:
        exclude_reason = "sun_outside_window_azimuth"

    payload = {
        "schema_version": 1,
        "timestamp": now.isoformat(),
        "lux": lux,
        "sun_azimuth": azimuth if azimuth is not None else 0,
        "sun_elevation": elevation if elevation is not None else 0,
        "weather_state": weather_state or "unknown",
        "cover_state": cover_state or "unknown",
        "cover_position": cover_position if cover_position is not None else -1,
        "automation_enabled": automation_enabled,
        "artificial_light_guard": light_guard or "unknown",
        "eligible": exclude_reason is None,
        "exclude_reason": exclude_reason,
    }
    try:
        task.executor(append_sample, DATA_DIR, payload)
    except Exception as error:
        _set_diagnostic("error", {"reason_code": "sample_write_failed"})
        _notify_once(
            "sample_write_failed",
            "阳光窗帘自动学习异常",
            f"样本写入失败：{error}",
        )


def _analyze_and_maybe_apply(dry_run=False):
    task.unique("sun_curtain_learning_analysis")
    if not _retry_pending_notification():
        return {"status": "error", "reason_code": "notification_failed"}
    if _state(LEARNING_SWITCH) != "on":
        _set_diagnostic("paused", {"reason_code": "learning_switch_off"})
        return {"status": "paused", "reason_code": "learning_switch_off"}

    now = _now()
    current_threshold = _to_float(_state(THRESHOLD_HELPER))
    if current_threshold is None:
        result = {"status": "error", "reason_code": "threshold_invalid"}
        _set_diagnostic("error", result)
        _notify_once(
            "threshold_invalid",
            "阳光窗帘自动学习异常",
            "正式照度阈值不可用，未执行学习。",
        )
        return result

    try:
        previous_candidates = task.executor(load_candidates, DATA_DIR, now=now)
        result = task.executor(
            analyze_sample_files,
            DATA_DIR,
            now=now,
            current_threshold=current_threshold,
            previous_candidates=previous_candidates,
        )
        if result.get("candidate_lux") is not None and not dry_run:
            task.executor(
                record_candidate,
                DATA_DIR,
                value=result["candidate_lux"],
                now=now,
            )
        task.executor(prune_sample_files, DATA_DIR, now=now, retention_days=30)
    except Exception as error:
        result = {"status": "error", "reason_code": "analysis_failed"}
        _set_diagnostic("error", result)
        _notify_once(
            "analysis_failed",
            "阳光窗帘自动学习异常",
            f"夜间分析失败：{error}",
        )
        return result

    _set_diagnostic(result["status"], result)
    if dry_run or result["status"] != "ready":
        return result

    applied = result["applied_lux"]
    latest_learning_state = _state(LEARNING_SWITCH)
    latest_threshold = _to_float(_state(THRESHOLD_HELPER))
    if latest_learning_state != "on" or latest_threshold != current_threshold:
        service.call(
            "input_boolean",
            "turn_off",
            blocking=True,
            entity_id=LEARNING_SWITCH,
        )
        paused = {
            **result,
            "status": "paused",
            "reason_code": "analysis_inputs_changed",
        }
        _set_diagnostic("paused", paused)
        return paused
    if float(applied) == current_threshold:
        unchanged = {**result, "reason_code": "threshold_unchanged"}
        _set_diagnostic("ready", unchanged)
        return unchanged

    notification = {
        "title": "阳光窗帘阈值已更新",
        "message": (
            f"照度阈值已从 {current_threshold:.0f} 更新为 {applied:.0f} lx；"
            f"候选值 {result['candidate_lux']:.0f} lx，"
            f"有效天数 {result['valid_days']}，事件 {result['event_count']}，"
            f"可信度 {result['confidence']:.1%}。"
        ),
        "level": "info",
    }
    try:
        task.executor(
            write_internal_marker,
            DATA_DIR,
            value=applied,
            now=now,
            ttl_seconds=10,
        )
        task.executor(write_pending_notification, DATA_DIR, notification)
        service.call(
            "input_number",
            "set_value",
            blocking=True,
            entity_id=THRESHOLD_HELPER,
            value=applied,
        )
        read_back = _to_float(_state(THRESHOLD_HELPER))
        if read_back != float(applied):
            raise RuntimeError("threshold_readback_mismatch")
    except Exception as error:
        task.executor(clear_internal_marker, DATA_DIR)
        task.executor(clear_pending_notification, DATA_DIR)
        _set_diagnostic("error", {**result, "reason_code": "threshold_update_failed"})
        _notify_once(
            "threshold_update_failed",
            "阳光窗帘阈值更新失败",
            f"候选值 {result['candidate_lux']:.0f} lx 未能应用：{error}",
        )
        return {**result, "status": "error", "reason_code": "threshold_update_failed"}

    try:
        _notify(notification["title"], notification["message"], notification["level"])
        task.executor(clear_pending_notification, DATA_DIR)
    except Exception as error:
        failed = {
            **result,
            "status": "error",
            "reason_code": "notification_failed",
            "notification_error": str(error),
        }
        _set_diagnostic("error", failed)
        return failed
    return result


@time_trigger("once(sunset + 30 min)")
def nightly_analysis(**kwargs):
    return _analyze_and_maybe_apply(dry_run=False)


@service("pyscript.sun_curtain_learning_analyze", supports_response="optional")
def analyze_now(dry_run=True):
    return _analyze_and_maybe_apply(dry_run=bool(dry_run))


@state_trigger(THRESHOLD_HELPER)
def threshold_changed(value=None, old_value=None, **kwargs):
    if old_value is None or value is None:
        return
    numeric_value = _to_float(value)
    if numeric_value is None:
        return
    now = _now()
    if task.executor(
        consume_matching_marker,
        DATA_DIR,
        value=numeric_value,
        now=now,
    ):
        return
    service.call(
        "input_boolean",
        "turn_off",
        blocking=True,
        entity_id=LEARNING_SWITCH,
    )
    _set_diagnostic("paused", {"reason_code": "manual_threshold_override"})
    _notify(
        "阳光窗帘自动学习已暂停",
        f"检测到人工修改照度阈值为 {numeric_value:.0f} lx，自动学习已暂停。",
        "warning",
    )


@state_trigger(LEARNING_SWITCH)
def learning_switch_changed(value=None, old_value=None, **kwargs):
    if old_value == "off" and value == "on":
        task.executor(clear_candidates, DATA_DIR)
        _set_diagnostic("learning", {"reason_code": "learning_reenabled"})
