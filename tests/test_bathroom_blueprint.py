from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = ROOT / "卫生间智能控制.yaml"
TZ = ZoneInfo("Asia/Shanghai")


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


class FakeState:
    def __init__(self, state: str) -> None:
        self.state = state


def load_blueprint() -> dict:
    return yaml.load(BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def window_close_template() -> str:
    document = load_blueprint()
    return next(
        trigger["value_template"]
        for trigger in document["trigger"]
        if trigger.get("id") == "trigger_window_close"
    )


def render_window_close_trigger(
    *,
    probe: datetime,
    window_close_time: str = "22:00:00",
    window_close_end_time: str = "06:00:00",
    window_auto_close: bool = True,
    window_state: str = "open",
    active_state: str = "off",
    post_state: str = "off",
    template: str | None = None,
) -> bool:
    if template is None:
        template = window_close_template()

    def parse_clock(value: str) -> time:
        hour, minute, second = (int(part) for part in value.split(":"))
        return time(hour, minute, second)

    def today_at(value: str) -> datetime:
        return datetime.combine(probe.date(), parse_clock(value), tzinfo=TZ)

    def states(entity_id: str) -> str:
        return window_state if entity_id == "cover.window" else "off"

    def expand(entity_ids) -> list[FakeState]:
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        return [FakeState(active_state if "active" in item else post_state) for item in entity_ids]

    environment = Environment()
    environment.globals.update(now=lambda: probe, today_at=today_at, states=states, expand=expand)
    rendered = environment.from_string(template).render(
        tv_window_auto_close=window_auto_close,
        tv_window_close_time=window_close_time,
        tv_window_close_end_time=window_close_end_time,
        tv_input_bool_active="input_boolean.active",
        tv_input_bool_post="input_boolean.post",
        tv_cover_window="cover.window",
    )
    return bool(yaml.safe_load(rendered))


def test_window_close_ignores_morning_openings_after_close_period_ends() -> None:
    for hour, minute in ((6, 0), (6, 33), (8, 35), (8, 59), (9, 0), (12, 0)):
        assert (
            render_window_close_trigger(probe=datetime(2026, 9, 16, hour, minute, tzinfo=TZ))
            is False
        ), (hour, minute)


def test_window_close_still_fires_during_the_night_period() -> None:
    assert render_window_close_trigger(probe=datetime(2026, 9, 15, 22, 0, tzinfo=TZ)) is True
    assert render_window_close_trigger(probe=datetime(2026, 9, 15, 23, 30, tzinfo=TZ)) is True
    assert render_window_close_trigger(probe=datetime(2026, 9, 16, 3, 0, tzinfo=TZ)) is True
    assert render_window_close_trigger(probe=datetime(2026, 9, 16, 5, 59, tzinfo=TZ)) is True


def test_window_close_period_boundaries_are_half_open() -> None:
    assert render_window_close_trigger(probe=datetime(2026, 9, 16, 21, 59, 59, tzinfo=TZ)) is False
    assert render_window_close_trigger(probe=datetime(2026, 9, 16, 22, 0, 0, tzinfo=TZ)) is True
    assert render_window_close_trigger(probe=datetime(2026, 9, 17, 5, 59, 59, tzinfo=TZ)) is True
    assert render_window_close_trigger(probe=datetime(2026, 9, 17, 6, 0, 0, tzinfo=TZ)) is False


def test_window_close_period_follows_configured_end_time() -> None:
    early = datetime(2026, 9, 16, 4, 30, tzinfo=TZ)
    assert render_window_close_trigger(probe=early, window_close_end_time="06:00:00") is True
    assert render_window_close_trigger(probe=early, window_close_end_time="04:00:00") is False
    assert render_window_close_trigger(probe=early, window_close_end_time="08:00:00") is True


def test_window_close_supports_same_day_period() -> None:
    template = window_close_template()
    assert (
        render_window_close_trigger(
            probe=datetime(2026, 9, 16, 7, 0, tzinfo=TZ),
            window_close_time="06:00:00",
            window_close_end_time="22:00:00",
            template=template,
        )
        is True
    )
    assert (
        render_window_close_trigger(
            probe=datetime(2026, 9, 16, 23, 0, tzinfo=TZ),
            window_close_time="06:00:00",
            window_close_end_time="22:00:00",
            template=template,
        )
        is False
    )


def test_window_close_keeps_existing_guards() -> None:
    night = datetime(2026, 9, 16, 23, 30, tzinfo=TZ)
    assert render_window_close_trigger(probe=night, window_state="closed") is False
    assert render_window_close_trigger(probe=night, active_state="on") is False
    assert render_window_close_trigger(probe=night, post_state="on") is False
    assert render_window_close_trigger(probe=night, window_auto_close=False) is False


def test_window_close_period_is_disabled_when_times_are_equal() -> None:
    for hour in (0, 6, 12, 22):
        assert (
            render_window_close_trigger(
                probe=datetime(2026, 9, 16, hour, 30, tzinfo=TZ),
                window_close_time="22:00:00",
                window_close_end_time="22:00:00",
            )
            is False
        ), hour


def test_window_close_ignores_blind_open_time() -> None:
    document = load_blueprint()
    template = window_close_template()

    assert "tv_blind_open_time" not in template
    assert "tv_blind_open_time" not in document["trigger_variables"]


def test_window_close_input_and_trigger_are_wired() -> None:
    document = load_blueprint()
    inputs = document["blueprint"]["input"]["section_env"]["input"]
    triggers = {trigger.get("id"): trigger for trigger in document["trigger"]}

    assert inputs["setting_window_close_end_time"]["default"] == "06:00:00"
    assert "time" in inputs["setting_window_close_end_time"]["selector"]
    assert inputs["setting_window_close_time"]["default"] == "22:00:00"

    assert triggers["trigger_window_close"]["platform"] == "template"
    assert document["trigger_variables"]["tv_window_close_end_time"] == {
        "__input__": "setting_window_close_end_time"
    }
    assert document["trigger_variables"]["tv_window_close_time"] == {
        "__input__": "setting_window_close_time"
    }


# --- 离开逻辑守卫：洗澡中 / 水在流时不得关灯 -------------------------------------
#
# 事故复盘（2026-09-27 外卫淋浴）：
#   21:12:21 淋浴区人体存在瞬时 off -> 四个传感器全部 off，命中 leave_check；
#   21:12:52（+30s 无人关灯延时）HA 关掉外卫全部灯（壁灯 / 射灯28 / 灯带56）。
#   此时水浸传感器仍为 on（21:02:34 起持续有水），人也在淋浴房里。
#   注意：input_bool_active 早在 21:02:09 就已被掉线引发的 leave_check 清掉，
#   所以「只守标志位」挡不住这次事故 —— 水浸传感器才是可靠信号。

MAIN_SENSOR = "binary_sensor.main"
SHOWER_SENSOR = "binary_sensor.shower"
ZONE_A_SENSOR = "binary_sensor.zone_a"
WATER_SENSOR = "binary_sensor.water"
ACTIVE_FLAG = "input_boolean.active"


def choose_branches() -> list[dict]:
    return load_blueprint()["action"][0]["choose"]


def branch_for(trigger_id: str) -> dict:
    for branch in choose_branches():
        for condition in branch.get("conditions", []):
            if condition.get("condition") == "trigger" and condition.get("id") == trigger_id:
                return branch
    raise AssertionError(f"no choose branch for trigger id {trigger_id!r}")


def template_conditions(branch: dict) -> list[str]:
    return [
        condition["value_template"]
        for condition in branch.get("conditions", [])
        if condition.get("condition") == "template"
    ]


def water_guard_template() -> str:
    """The running-water guard among the leave_check branch conditions."""
    for template in template_conditions(branch_for("leave_check")):
        if "water_leak_sensor" in template and "presence_sensor" not in template:
            return template
    raise AssertionError("leave_check branch has no running-water guard")


def aio_template_probe(
    template: str,
    *,
    water: str = "off",
    active: str = "off",
    sensor_states: dict[str, str] | None = None,
) -> bool:
    """Render a blueprint template with HA-like `is_state` global + test."""
    states = {
        MAIN_SENSOR: "off",
        SHOWER_SENSOR: "off",
        ZONE_A_SENSOR: "off",
        WATER_SENSOR: water,
    }
    states.update(sensor_states or {})

    def is_state(entity_id: str, expected: str) -> bool:
        if entity_id == ACTIVE_FLAG:
            return active == expected
        return states.get(entity_id, "off") == expected

    environment = Environment()
    environment.globals.update(is_state=is_state, expand=lambda value: [], states=lambda value: "off")
    environment.tests["is_state"] = is_state
    rendered = environment.from_string(template).render(
        var_inputs={
            "main_presence_sensor": MAIN_SENSOR,
            "shower_presence_sensor": SHOWER_SENSOR,
            "zone_a_presence_sensor": ZONE_A_SENSOR,
            "zone_b_presence_sensor": [],
            "water_leak_sensor": WATER_SENSOR,
            "input_bool_active": ACTIVE_FLAG,
        }
    )
    return bool(yaml.safe_load(rendered))


def render_water_guard(*, water: str = "off", template: str | None = None) -> bool:
    """是否允许进入离开流程（True = 水已停）。"""
    if template is None:
        template = water_guard_template()
    return aio_template_probe(template, water=water)


def sensor_refresh_template(
    *,
    water: str = "off",
    active: str = "off",
    sensor_states: dict[str, str] | None = None,
    template: str | None = None,
) -> bool:
    """The post-delay re-check that must gate the actual light switch-off."""
    if template is None:
        template = light_off_recheck_template()
    return aio_template_probe(
        template, water=water, active=active, sensor_states=sensor_states
    )


def leave_light_sequence() -> list[dict]:
    branch = branch_for("leave_check")
    parallel = next(step["parallel"] for step in branch["sequence"] if "parallel" in step)
    return parallel[0]["sequence"]


def contains_action(step: dict, action: str) -> bool:
    """True if the step performs `action`, descending into nested branches."""
    if step.get("action") == action:
        return True
    if "sequence" in step:
        return any(contains_action(child, action) for child in step["sequence"])
    if "parallel" in step:
        return any(contains_action(child, action) for child in step["parallel"])
    if "then" in step:
        return any(contains_action(child, action) for child in step["then"])
    if "else" in step:
        return any(contains_action(child, action) for child in step["else"])
    return False


def step_templates(step: dict) -> list[str]:
    """Template conditions attached to a step (directly or via a nested `if`)."""
    templates = []
    if step.get("condition") == "template":
        templates.append(step["value_template"])
    if step.get("condition") == "and":
        templates += [
            condition["value_template"]
            for condition in step["conditions"]
            if condition.get("condition") == "template"
        ]
    for key in ("if", "conditions"):
        for condition in step.get(key, []) or []:
            if condition.get("condition") == "template":
                templates.append(condition["value_template"])
    return templates


def light_off_recheck_template() -> str:
    """The template guarding the delayed light switch-off."""
    sequence = leave_light_sequence()
    delay_index = next(index for index, step in enumerate(sequence) if "delay" in step)
    for step in sequence[delay_index + 1 :]:
        if not contains_action(step, "light.turn_off"):
            continue
        for template in step_templates(step):
            if "presence_sensor" in template or "water_leak_sensor" in template:
                return template
    raise AssertionError("no re-check guarding light.turn_off after the delay")


def test_leave_branch_ignores_shower_while_water_is_running() -> None:
    # 事故现场：水一直在流 -> 绝不能执行离开关灯流程。
    assert render_water_guard(water="on") is False


def test_leave_branch_still_runs_after_water_stops() -> None:
    # 水停后才允许走离开流程（否则标志位/循环泵/除湿收尾会永远清不掉）。
    assert render_water_guard(water="off") is True


def test_leave_light_off_rechecks_emptiness_and_water() -> None:
    """关灯延时不只用于等待；延时结束后必须重新确认仍然无人且水已停。"""
    assert sensor_refresh_template(water="on", active="off") is False
    assert sensor_refresh_template(water="off", active="off") is True
    assert (
        sensor_refresh_template(
            water="off", active="off", sensor_states={SHOWER_SENSOR: "on"}
        )
        is False
    )


def test_leave_branch_delay_precedes_light_off() -> None:
    """延时必须在关灯之前，确保「持续无人」而不是瞬时无人。"""
    sequence = leave_light_sequence()
    delay_index = next(index for index, step in enumerate(sequence) if "delay" in step)
    turn_off_index = next(
        index for index, step in enumerate(sequence) if contains_action(step, "light.turn_off")
    )
    assert delay_index < turn_off_index


def state_triggers() -> list[tuple[str, str, str]]:
    """(entity_id, to, id) for every state trigger in the blueprint."""
    document = load_blueprint()
    rows = []
    for trigger in document["trigger"]:
        if trigger.get("platform") != "state":
            continue
        entity_id = trigger["entity_id"]
        if isinstance(entity_id, dict):
            entity_id = entity_id["__input__"]
        rows.append((entity_id, trigger.get("to"), trigger.get("id")))
    return rows


def test_reentry_restarts_pending_light_off() -> None:
    """mode: restart + 进人触发 = 延时期间重新进人会取消本次关灯。

    这是「持续无人」的第一道保障：延时还没走完就有人进来，
    自动化被 restart，关灯动作根本不会执行。
    """
    document = load_blueprint()
    assert document["mode"] == "restart"

    triggers = state_triggers()
    for sensor in ("main_presence_sensor", "shower_presence_sensor"):
        assert (sensor, "on") in {(entity, to) for entity, to, _ in triggers}, sensor


def test_running_water_restarts_pending_light_off() -> None:
    """延时期间水龙头/花洒再次出水，也应取消本次关灯。"""
    triggers = state_triggers()
    assert ("water_leak_sensor", "on") in {(entity, to) for entity, to, _ in triggers}


def test_water_stop_rechecks_the_leave_flow() -> None:
    """守卫拦下后，水停时必须能重新触发收尾，否则标志位/灯永远清不掉。"""
    triggers = state_triggers()
    assert ("water_leak_sensor", "off", "leave_check") in triggers


def test_safety_stop_is_not_gated_by_running_water() -> None:
    """熔断分支不能依赖水停，否则水浸传感器卡住就永远无法收尾。"""
    templates = template_conditions(branch_for("force_safety_stop"))
    assert not any("water_leak_sensor" in template for template in templates)


# --- 补光分支：不得再依赖「主灯已开」 -------------------------------------------


def entry_light_if(trigger_id: str) -> dict:
    """The per-zone light `if` block inside the shared entry branch."""
    for branch in choose_branches():
        for condition in branch.get("conditions", []):
            if condition.get("condition") != "or":
                continue
            ids = {item.get("id") for item in condition.get("conditions", [])}
            if trigger_id not in ids:
                continue
            for step in branch["sequence"]:
                if "if" not in step:
                    continue
                for inner in step["if"]:
                    if inner.get("condition") == "trigger" and inner.get("id") == trigger_id:
                        return step
    raise AssertionError(f"no entry light block for trigger id {trigger_id!r}")


def entry_light_templates(trigger_id: str) -> list[str]:
    return [
        condition["value_template"]
        for condition in entry_light_if(trigger_id)["if"]
        if condition.get("condition") == "template"
    ]


def entry_light_or_block(trigger_id: str) -> dict:
    """The `or` block gating a follow-zone light (main-light-on OR dark)."""
    for condition in entry_light_if(trigger_id)["if"]:
        if condition.get("condition") == "or":
            return condition
    raise AssertionError(f"{trigger_id} has no or-block gating its light")


def test_follow_branches_can_light_up_on_their_own() -> None:
    """主灯被关掉后，淋浴区/扩展区在黑暗中再次进人时仍应能自己开灯。

    事故：21:12:52 主灯被离开逻辑关掉，21:22:19 淋浴区再次进人却无法开灯
    （当时照度 4.0 lx，远低于阈值 30 lx）。
    """
    for trigger_id in ("entry_shower", "entry_zone_a", "entry_zone_b"):
        or_block = entry_light_or_block(trigger_id)
        conditions = or_block["conditions"]

        templates = [c["value_template"] for c in conditions if c.get("condition") == "template"]
        numerics = [c for c in conditions if c.get("condition") == "numeric_state"]

        # 一条「跟随已有照明」，一条「黑暗中自主判断」，任一成立即可。
        assert len(conditions) == 2, (trigger_id, conditions)
        assert any("light_sink_area" in template for template in templates), trigger_id
        assert len(numerics) == 1, trigger_id
        assert numerics[0]["below"] == {"__input__": "illuminance_threshold"}, trigger_id
        assert numerics[0]["entity_id"] == {"__input__": "illuminance_sensor"}, trigger_id


def test_follow_branches_do_not_require_main_light() -> None:
    """主灯依赖必须是 or 的一个分支，不能是独立的 AND 条件（否则永远开不了灯）。"""
    for trigger_id in ("entry_shower", "entry_zone_a", "entry_zone_b"):
        for condition in entry_light_if(trigger_id)["if"]:
            if condition.get("condition") != "template":
                continue
            assert "light_sink_area" not in condition["value_template"], (
                f"{trigger_id} still hard-requires the main light being on"
            )


def test_follow_branches_keep_night_suppression() -> None:
    """去掉主灯依赖的同时，起床模式抑制必须保留。"""
    for trigger_id in ("entry_shower", "entry_zone_a", "entry_zone_b"):
        templates = entry_light_templates(trigger_id)
        assert any("input_get_up_mode" in template for template in templates), trigger_id


def test_follow_branches_still_turn_on_lights() -> None:
    for trigger_id in ("entry_shower", "entry_zone_a", "entry_zone_b"):
        actions = [step.get("action") for step in entry_light_if(trigger_id)["then"]]
        assert "light.turn_on" in actions, trigger_id
