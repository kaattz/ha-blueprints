from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = ROOT / "卧室智能控制.yaml"


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


def load_blueprint() -> dict:
    return yaml.load(BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def condition_templates(branch: dict) -> set[str]:
    return {
        condition["value_template"]
        for condition in branch["conditions"]
        if condition.get("condition") == "template"
    }


class FakeState:
    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed


def render_entry_evidence(
    *,
    motion_age: int | None = None,
    door_age: int | None = None,
    indoor_state: str = "on",
    motion_state: str = "off",
    door_state: str = "off",
) -> bool:
    document = load_blueprint()
    outer_branches = next(action["choose"] for action in document["action"] if "choose" in action)
    entry_sequence = outer_branches[0]["sequence"]
    wait_action = next(action for action in entry_sequence if "wait_template" in action)
    now_value = datetime(2026, 9, 10, 18, 0, 0)
    states = {
        "binary_sensor.indoor": FakeState(indoor_state, now_value - timedelta(seconds=1)),
    }
    motion_entities: list[str] = []
    door_entities: list[str] = []

    if motion_age is not None:
        motion_entities = ["binary_sensor.entry_motion"]
        states[motion_entities[0]] = FakeState(
            motion_state, now_value - timedelta(seconds=motion_age)
        )
    if door_age is not None:
        door_entities = ["binary_sensor.door"]
        states[door_entities[0]] = FakeState(
            door_state, now_value - timedelta(seconds=door_age)
        )

    def expand(entity_ids: list[str]) -> list[FakeState]:
        return [states[entity_id] for entity_id in entity_ids if entity_id in states]

    environment = Environment()
    environment.filters["bool"] = lambda value, _default=False: str(value).strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    environment.globals.update(
        now=lambda: now_value,
        as_timestamp=lambda value: value.timestamp(),
        expand=expand,
        is_state=lambda entity_id, state: states[entity_id].state == state,
        states=states,
    )
    context: dict[str, object] = {
        "v_bed_sensor": "binary_sensor.indoor",
        "v_motion_outside_list": motion_entities,
        "v_door_list": door_entities,
        "v_timeout": 10,
    }
    rendered = environment.from_string(wait_action["wait_template"]).render(**context)
    return bool(yaml.safe_load(rendered))


def test_night_window_inputs_are_optional_and_cross_midnight() -> None:
    document = load_blueprint()
    inputs = document["blueprint"]["input"]["section_night"]["input"]

    assert inputs["inp_night_start_time"]["default"] == "21:00:00"
    assert inputs["inp_night_end_time"]["default"] == "07:00:00"
    assert "time" in inputs["inp_night_start_time"]["selector"]
    assert "time" in inputs["inp_night_end_time"]["selector"]

    variables = document["action"][0]["variables"]
    template = variables["is_night_window"]
    environment = Environment()

    def render(now_value: datetime) -> bool:
        environment.globals["now"] = lambda: now_value
        value = environment.from_string(template).render(
            night_start_text="21:00:00",
            night_end_text="07:00:00",
        )
        return bool(yaml.safe_load(value))

    assert render(datetime(2026, 9, 9, 20, 59, 59)) is False
    assert render(datetime(2026, 9, 9, 21, 0, 0)) is True
    assert render(datetime(2026, 9, 10, 6, 59, 59)) is True
    assert render(datetime(2026, 9, 10, 7, 0, 0)) is False


def test_entry_detection_inputs_have_their_own_section() -> None:
    document = load_blueprint()
    inputs = document["blueprint"]["input"]
    entry_inputs = inputs["section_entry_detection"]["input"]

    assert set(entry_inputs) == {"inp_door", "inp_entrance_motion", "inp_entry_timeout"}
    assert entry_inputs["inp_entry_timeout"]["name"] == "进房信号关联窗口 (秒)"
    assert entry_inputs["inp_entry_timeout"]["default"] == 10
    assert "inp_door" not in inputs["section_night"]["input"]
    assert "inp_entrance_motion" not in inputs["section_night"]["input"]


def test_entry_evidence_degrades_by_configured_sensors() -> None:
    assert render_entry_evidence(motion_age=2, door_age=3) is True
    assert render_entry_evidence(motion_age=2) is True
    assert render_entry_evidence(door_age=3) is True
    assert render_entry_evidence() is True

    assert render_entry_evidence(motion_age=11, door_age=3) is False
    assert render_entry_evidence(motion_age=2, door_age=11) is False


def test_only_real_indoor_off_to_on_starts_ordinary_entry_detection() -> None:
    document = load_blueprint()
    triggers = {trigger["id"]: trigger for trigger in document["trigger"]}

    assert triggers["t_enter"]["from"] == "off"
    assert triggers["t_enter"]["to"] == "on"
    assert "t_door" not in triggers
    assert "t_entry_motion" not in triggers

    outer_branches = next(action["choose"] for action in document["action"] if "choose" in action)
    entry_branch = outer_branches[0]
    assert entry_branch["conditions"] == [{"condition": "trigger", "id": "t_enter"}]
    wait_action = next(action for action in entry_branch["sequence"] if "wait_template" in action)
    assert wait_action["timeout"]["seconds"] == {"__input__": "inp_entry_timeout"}
    assert wait_action["continue_on_timeout"] is False
    assert render_entry_evidence(indoor_state="off", motion_age=2, door_age=3) is True
    assert render_entry_evidence(motion_age=2, motion_state="unavailable") is False


def test_entry_branches_block_normal_light_for_bed_occupancy_only() -> None:
    document = load_blueprint()
    variables = document["action"][0]["variables"]
    outer_branches = next(action["choose"] for action in document["action"] if "choose" in action)
    entry_sequence = outer_branches[0]["sequence"]
    entry_mode_branches = next(action["choose"] for action in entry_sequence if "choose" in action)
    normal_branch = entry_mode_branches[0]
    night_branch = entry_mode_branches[1]

    assert "bed_occupied" in variables
    assert "suite_occupied" in variables
    assert "valid_entry" not in variables
    assert "is_night_window" in variables

    normal_templates = condition_templates(normal_branch)
    assert "{{ not is_sleeping }}" in normal_templates
    assert "{{ not bed_occupied }}" in normal_templates
    assert "{{ not occupied_without_main }}" not in normal_templates
    assert "{{ valid_entry }}" not in normal_templates

    night_templates = condition_templates(night_branch)
    assert "{{ is_sleeping }}" in night_templates
    assert "{{ is_night_window }}" in night_templates
    assert "{{ valid_entry }}" not in night_templates
    assert "{{ not bed_occupied }}" not in night_templates

    assert all(condition.get("condition") != "trigger" for condition in normal_branch["conditions"])
    assert all(condition.get("condition") != "trigger" for condition in night_branch["conditions"])
    assert "{{ v_sleep_entry_mode != 'parallel' }}" in night_templates

    wait_index = next(index for index, action in enumerate(entry_sequence) if "wait_template" in action)
    assert entry_sequence[wait_index + 1] == {
        "condition": "state",
        "entity_id": {"__input__": "inp_bed_presence"},
        "state": "on",
    }


def test_shutdown_branches_bypass_entry_evidence_wait() -> None:
    document = load_blueprint()
    outer_branches = next(action["choose"] for action in document["action"] if "choose" in action)

    trigger_ids = [
        next(
            condition["id"]
            for condition in branch["conditions"]
            if condition.get("condition") == "trigger"
        )
        for branch in outer_branches
    ]
    assert trigger_ids == ["t_enter", "t_sleep_motion", "t_lightoff", "t_ac"]
    for branch in outer_branches[2:]:
        assert all("wait_template" not in action for action in branch["sequence"])


def test_sleep_entry_uses_real_motion_then_real_door_without_indoor_edge() -> None:
    document = load_blueprint()
    inputs = document["blueprint"]["input"]["section_night"]["input"]
    assert inputs["inp_sleep_entry_mode"]["default"] == "restart"
    assert [option["value"] for option in inputs["inp_sleep_entry_mode"]["selector"]["select"]["options"]] == ["restart", "parallel"]
    assert document["trigger_variables"] == {"sleep_entry_mode": {"__input__": "inp_sleep_entry_mode"}}
    triggers = {trigger["id"]: trigger for trigger in document["trigger"]}
    assert triggers["t_enter"]["entity_id"] == {"__input__": "inp_bed_presence"}
    assert triggers["t_sleep_motion"] == {
        "platform": "state",
        "entity_id": {"__input__": "inp_entrance_motion"},
        "from": "off",
        "to": "on",
        "id": "t_sleep_motion",
        "enabled": "{{ sleep_entry_mode == 'parallel' }}",
    }
    assert document["mode"] == {"__input__": "inp_sleep_entry_mode"}
    assert document["max_exceeded"] == "warning"
    branches = next(action["choose"] for action in document["action"] if "choose" in action)
    night_entry = branches[1]
    assert night_entry["conditions"][0] == {"condition": "trigger", "id": "t_sleep_motion"}
    assert {"condition": "time", "after": {"__input__": "inp_night_start_time"}, "before": {"__input__": "inp_night_end_time"}} in night_entry["conditions"]
    assert any("v_sleep_modes" in condition.get("value_template", "") for condition in night_entry["conditions"])
    assert any("v_any_light" in condition.get("value_template", "") for condition in night_entry["conditions"])
    assert night_entry["sequence"][0] == {
        "wait_for_trigger": [
            {
                "platform": "state",
                "entity_id": {"__input__": "inp_door"},
                "from": "off",
                "to": "on",
            }
        ],
        "timeout": {"seconds": {"__input__": "inp_entry_timeout"}},
        "continue_on_timeout": False,
    }
    assert night_entry["sequence"][1] == {
        "condition": "time",
        "after": {"__input__": "inp_night_start_time"},
        "before": {"__input__": "inp_night_end_time"},
    }
    assert "v_sleep_modes" in night_entry["sequence"][2]["value_template"]
    assert "is_sleeping" not in night_entry["sequence"][2]["value_template"]
    assert night_entry["sequence"][-1]["action"] == "light.turn_on"
    assert night_entry["sequence"][-1]["target"] == {"__input__": "inp_night_lights"}


def test_sleep_entry_does_not_replace_manual_brightness_or_fail_open() -> None:
    document = load_blueprint()
    branches = next(action["choose"] for action in document["action"] if "choose" in action)
    sequence = branches[1]["sequence"]
    template = sequence[3]["value_template"]
    now_value = datetime(2026, 9, 15, 0, 0, 51)
    state_values = {
        "binary_sensor.light_status": FakeState("off", now_value),
    }
    environment = Environment()
    environment.globals["expand"] = lambda ids: [state_values[name] for name in ids if name in state_values]

    def allowed(status):
        if status is None:
            state_values.pop("binary_sensor.light_status", None)
        else:
            state_values["binary_sensor.light_status"] = FakeState(status, now_value)
        rendered = environment.from_string(template).render(v_any_light=["binary_sensor.light_status"])
        return bool(yaml.safe_load(rendered))

    assert allowed("off") is True
    assert allowed("on") is False
    assert allowed("unavailable") is False
    assert allowed(None) is False
    empty_rendered = environment.from_string(template).render(v_any_light=[])
    assert bool(yaml.safe_load(empty_rendered)) is False


def test_parallel_shutdown_rechecks_live_presence_before_turning_off() -> None:
    document = load_blueprint()
    branches = next(action["choose"] for action in document["action"] if "choose" in action)
    shutdown = branches[2]["sequence"]
    assert shutdown[0] == {
        "condition": "state",
        "entity_id": {"__input__": "inp_bed_presence"},
        "state": "off",
    }
    assert shutdown[1]["action"] == "light.turn_off"

    ac_shutdown = branches[3]["sequence"]
    assert ac_shutdown[0] == {
        "condition": "state",
        "entity_id": {"__input__": "inp_bed_presence"},
        "state": "off",
    }
    assert ac_shutdown[1]["action"] == "climate.turn_off"
