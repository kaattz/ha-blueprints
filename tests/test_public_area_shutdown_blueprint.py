from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = ROOT / "公区设备智能关闭(全员就寝).yaml"


class BlueprintLoader(yaml.SafeLoader):
    pass


def _input(loader: BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


BlueprintLoader.add_constructor("!input", _input)


def load_blueprint() -> dict:
    return yaml.load(BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def triggers(document: dict) -> list[dict]:
    return document["trigger"]


def trigger_by_id(document: dict, trigger_id: str) -> dict:
    return next(t for t in triggers(document) if t.get("id") == trigger_id)


def entry_condition(document: dict) -> dict:
    """The first action step is the trigger-id gate guarding the shutdown flow."""
    return document["action"][0]


def allowed_trigger_ids(document: dict) -> set[str]:
    condition = entry_condition(document)
    assert condition["condition"] == "or", condition
    return {
        branch["id"]
        for branch in condition["conditions"]
        if branch.get("condition") == "trigger"
    }


# --- 改动 1: 重启/重载对账触发 ---------------------------------------------
# 背景：模板触发器只在 false→true 的跳变瞬间触发。若 HA 重启时条件已成立
# （跳变发生在停机期间），就永远等不到下一次跳变，导致整晚静默。


def test_startup_reconcile_trigger_exists() -> None:
    document = load_blueprint()
    trigger = trigger_by_id(document, "ha_start_reconcile")

    assert trigger["platform"] == "homeassistant"
    assert trigger["event"] == "start"


def test_automation_reload_reconcile_trigger_exists() -> None:
    document = load_blueprint()
    trigger = trigger_by_id(document, "automation_reload_reconcile")

    assert trigger["platform"] == "event"
    assert trigger["event_type"] == "automation_reloaded"


def test_reconcile_triggers_reach_the_shutdown_flow() -> None:
    """Regression: the trigger-id gate must let reconcile runs through.

    If the entry gate only allowed all_in_bed_true, the new reconcile triggers
    would be silently dropped and the overnight-blackout bug would persist.
    """
    document = load_blueprint()

    assert allowed_trigger_ids(document) == {
        "all_in_bed_true",
        "ha_start_reconcile",
        "automation_reload_reconcile",
    }


def test_cancel_trigger_still_cannot_reach_the_shutdown_flow() -> None:
    """The cancel trigger must keep being rejected by the entry gate."""
    document = load_blueprint()

    assert "all_in_bed_false" not in allowed_trigger_ids(document)


def test_entry_gate_is_not_a_bare_trigger_condition() -> None:
    """Guard against a revert to the single-id gate that caused the bug."""
    condition = entry_condition(load_blueprint())

    assert condition["condition"] == "or"


def test_reconcile_triggers_do_not_bypass_safety_checks() -> None:
    """Reconcile only adds a way in; every downstream guard must remain."""
    document = load_blueprint()
    rendered = yaml.dump(document["action"], allow_unicode=True)

    # time window, re-confirm in bed, deadline, public-area wait, final re-check
    assert "shutdown_window_start" in rendered
    assert "public_area_wait_deadline" in rendered
    assert "wait_template" in rendered
    assert rendered.count("namespace(all_in_bed=true") >= 2

    wait_step = next(step for step in document["action"] if "wait_template" in step)
    assert wait_step["continue_on_timeout"] is False


# --- 改动 2: 取消触发防抖 ---------------------------------------------------
# 背景：亚秒级抖动（例如 KNX 实体瞬时 unavailable）会翻转 all_in_bed_false，
# 在 mode: restart 下立即掐掉正在等待公区清空的一轮（今晚 21:28:37 即如此）。


def test_cancel_trigger_has_debounce_for() -> None:
    document = load_blueprint()
    trigger = trigger_by_id(document, "all_in_bed_false")

    assert trigger["for"] == {"__input__": "cancel_debounce"}


def test_cancel_debounce_input_is_declared_with_default() -> None:
    document = load_blueprint()
    definition = document["blueprint"]["input"]["section_global"]["input"]["cancel_debounce"]

    assert definition["default"] == {"minutes": 1}
    assert "duration" in definition["selector"]


def test_confirm_delay_trigger_has_no_debounce() -> None:
    """all_in_bed_true must stay edge-triggered without a `for` delay."""
    document = load_blueprint()
    trigger = trigger_by_id(document, "all_in_bed_true")

    assert "for" not in trigger


def test_both_bed_templates_share_the_same_predicate() -> None:
    """The debounce must watch the same condition it cancels.

    Both templates must derive from the identical predicate; the false one is
    exactly its negation. A divergence here would let the debounce cancel a run
    on a different condition than the one that started it.
    """
    document = load_blueprint()
    true_template = trigger_by_id(document, "all_in_bed_true")["value_template"]
    false_template = trigger_by_id(document, "all_in_bed_false")["value_template"]

    shared = (
        "is_state(item.tracker, 'home')",
        "ns.someone_home",
        "ns.all_in_bed",
        "expand(item.bed)",
    )
    for fragment in shared:
        assert fragment in true_template, fragment
        assert fragment in false_template, fragment

    # The cancel template is the exact negation of the confirm template.
    assert "not (ns.someone_home and ns.all_in_bed)" in false_template
    assert "{{ ns.someone_home and ns.all_in_bed }}" in true_template


def test_mode_is_restart_for_cancel_to_work() -> None:
    document = load_blueprint()

    assert document["mode"] == "restart"
