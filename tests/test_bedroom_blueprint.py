from __future__ import annotations

from datetime import datetime
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


def test_entry_branches_require_real_entry_and_protect_existing_occupants() -> None:
    document = load_blueprint()
    variables = document["action"][0]["variables"]
    branches = document["action"][1]["choose"]
    normal_branch = branches[0]
    night_branch = branches[1]

    assert "occupied_without_main" in variables
    assert "is_night_window" in variables

    normal_templates = condition_templates(normal_branch)
    assert "{{ not is_sleeping }}" in normal_templates
    assert "{{ not occupied_without_main }}" in normal_templates
    assert "{{ valid_entry_motion }}" in normal_templates

    night_templates = condition_templates(night_branch)
    assert "{{ is_sleeping }}" in night_templates
    assert "{{ is_night_window }}" in night_templates
    assert "{{ valid_entry_motion }}" in night_templates

    normal_trigger = next(
        condition for condition in normal_branch["conditions"] if condition.get("condition") == "trigger"
    )
    night_trigger = next(
        condition for condition in night_branch["conditions"] if condition.get("condition") == "trigger"
    )
    assert normal_trigger["id"] == "t_enter"
    assert night_trigger["id"] == "t_enter"

    branch_trigger_ids = {
        condition["id"]
        for branch in branches
        for condition in branch["conditions"]
        if condition.get("condition") == "trigger"
    }
    assert "t_door" not in branch_trigger_ids
