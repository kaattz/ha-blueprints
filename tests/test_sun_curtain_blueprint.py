from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = ROOT / "智能阳光窗帘控制.yaml"


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


def load_blueprint() -> dict:
    return yaml.load(BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def trigger_by_id(document: dict, trigger_id: str) -> dict:
    return next(trigger for trigger in document["trigger"] if trigger.get("id") == trigger_id)


def action_branch(document: dict, trigger_id: str) -> dict:
    for branch in document["action"][0]["choose"]:
        for condition in branch["conditions"]:
            if condition.get("id") == trigger_id:
                return branch
    raise AssertionError(f"missing action branch for {trigger_id}")


def test_presence_gone_trigger_is_unchanged() -> None:
    document = load_blueprint()
    trigger = trigger_by_id(document, "person_gone_wait")

    assert trigger["for"] == {"minutes": {"__input__": "no_presence_duration"}}
    assert "is_state(presence_entity, 'off')" in trigger["value_template"]


def test_presence_gone_closes_when_sun_condition_still_holds() -> None:
    document = load_blueprint()
    branch = action_branch(document, "person_gone_wait")
    template_conditions = [
        condition for condition in branch["conditions"] if condition["condition"] == "template"
    ]

    assert len(template_conditions) == 1
    template = template_conditions[0]["value_template"]
    assert "sun_hits_window" in template
    assert "lux > effective_lux_limit" in template
    assert "el > dynamic_min" in template
    assert "el < dynamic_max" in template

    assert branch["sequence"] == [
        {
            "action": "cover.close_cover",
            "target": {"__input__": "target_curtains"},
        }
    ]


def test_no_new_triggers_were_added() -> None:
    document = load_blueprint()
    ids = [trigger.get("id") for trigger in document["trigger"]]

    assert ids == ["sun_arrived", "sun_gone_wait", "person_gone_wait"]


def test_sun_arrived_close_flow_is_preserved() -> None:
    document = load_blueprint()
    branch = action_branch(document, "sun_arrived")

    assert branch["sequence"] == [
        {
            "action": "cover.close_cover",
            "target": {"__input__": "target_curtains"},
        }
    ]


def test_existing_open_flow_is_preserved() -> None:
    document = load_blueprint()
    default = document["action"][0]["default"]

    assert default[-1] == {
        "action": "cover.open_cover",
        "target": {"__input__": "target_curtains"},
    }


def test_target_curtain_and_presence_inputs_are_still_wired() -> None:
    document = load_blueprint()
    inputs = document["blueprint"]["input"]

    assert "target_curtains" in inputs
    assert "presence_sensor" in inputs
    assert document["variables"]["presence_entity"] == {"__input__": "presence_sensor"}