from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


def iter_input_definitions(inputs: dict):
    for definition in inputs.values():
        if "input" in definition:
            yield from iter_input_definitions(definition["input"])
        if "selector" in definition:
            yield definition


def test_all_blueprint_selectors_use_home_assistant_normalized_shape() -> None:
    for path in sorted(ROOT.glob("*.yaml")):
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=BlueprintLoader)
        inputs = document["blueprint"].get("input", {})

        for definition in iter_input_definitions(inputs):
            selector = definition["selector"]
            kind, config = next(iter(selector.items()))

            if kind == "entity":
                assert "multiple" in config, path.name
                assert "reorder" in config, path.name
            elif kind == "text":
                assert "multiple" in config, path.name
                assert "multiline" in config, path.name
            elif kind == "number":
                assert "step" in config, path.name
            elif kind == "duration":
                assert "enable_second" in config, path.name
            elif kind == "select":
                assert {"multiple", "custom_value", "sort"}.issubset(config), path.name

            for filter_key in ("domain", "device_class", "integration"):
                if isinstance(config, dict) and filter_key in config:
                    assert isinstance(config[filter_key], list), (path.name, filter_key)

            if kind == "target" and "entity" in config:
                assert isinstance(config["entity"], list), path.name
                for entity_filter in config["entity"]:
                    for filter_key in ("domain", "device_class", "integration"):
                        if filter_key in entity_filter:
                            assert isinstance(entity_filter[filter_key], list), (
                                path.name,
                                filter_key,
                            )
