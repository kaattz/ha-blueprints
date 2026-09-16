from __future__ import annotations

from datetime import datetime, time
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
