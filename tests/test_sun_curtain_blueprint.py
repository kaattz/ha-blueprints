from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
BLUEPRINT_PATH = ROOT / "智能阳光窗帘控制.yaml"


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node):
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


def load_blueprint():
    return yaml.load(BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def test_lux_threshold_helper_is_optional_typed_input_number_entity():
    helper = load_blueprint()["blueprint"]["input"]["lux_threshold_helper"]

    assert helper["default"] == ""
    assert helper["selector"]["entity"]["domain"] == "input_number"


def test_existing_blueprint_input_keys_are_preserved():
    inputs = load_blueprint()["blueprint"]["input"]
    existing = {
        "target_curtains",
        "presence_sensor",
        "lux_sensor",
        "weather_entity",
        "window_azimuth",
        "azimuth_range",
        "min_elevation_winter",
        "min_elevation_summer",
        "max_elevation_winter",
        "max_elevation_summer",
        "lux_threshold",
        "no_sun_duration",
        "no_presence_duration",
        "automation_switch",
    }

    assert existing.issubset(inputs)


def test_helper_is_bound_for_triggers_and_action_trace_variables():
    document = load_blueprint()

    assert document["trigger_variables"]["lux_threshold_helper_entity"] == {
        "__input__": "lux_threshold_helper"
    }
    assert document["variables"]["lux_threshold_helper_entity"] == {
        "__input__": "lux_threshold_helper"
    }
    assert "effective_lux_limit" in document["variables"]
    assert "lux_limit_source" in document["variables"]


def test_all_close_condition_templates_validate_helper_and_fallback():
    source = BLUEPRINT_PATH.read_text(encoding="utf-8")

    assert source.count("{% set helper_valid =") == 3
    assert source.count("1000 <= helper_lux <= 50000") == 5
    assert source.count("{% set effective_lux_limit =") >= 3
    assert source.count("lux > effective_lux_limit") == 3
    assert source.count("lux < effective_lux_limit") == 2
    assert "lux > (lux_limit | float(0))" not in source
    assert "lux < (lux_limit | float(0))" not in source
