"""entity_mapping 纯逻辑测试 + 与 load_enabled_mapping 的集成回归。"""

from pathlib import Path

import pytest

from custom_components.oneconnect_bridge.business.entity_mapping import (
    MANAGED_BY,
    MANAGED_BY_FIELD,
    apply_entity_selection,
    bound_entity_of,
    build_child_mapping,
    child_key_for_entity,
    is_managed_child,
    managed_children,
)
from custom_components.oneconnect_bridge.business.mapping_config import (
    load_enabled_mapping,
)

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components/oneconnect_bridge/catalog.yaml"
)


def _compile(children: dict) -> object:
    """把生成的 children 写成 mapping 文档并走真实校验编译。"""

    document = apply_entity_selection(
        {"children": {}}, gateway_serial="TESTSERIAL", selected_children=children
    )
    assert document["schema"] == 1
    assert document["enabled"] is True
    assert document["gateway_serial"] == "TESTSERIAL"
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        import yaml

        yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False)
        path = Path(handle.name)
    try:
        return load_enabled_mapping(path, catalog_path=CATALOG_PATH)
    finally:
        path.unlink()


def test_child_key_sanitized():
    assert child_key_for_entity("switch.pc_power") == "switch_pc_power"
    assert child_key_for_entity("light.客厅灯") == "light____"
    # 结果必须落在存储层允许的字符集内
    for char in child_key_for_entity("light.客厅.灯"):
        assert char.isascii() and (char.isalnum() or char in "_-")


def test_switch_child_roundtrip():
    child = build_child_mapping("switch.pc_power", name="电脑电源")
    assert child[MANAGED_BY_FIELD] == MANAGED_BY
    plan = _compile({"switch_pc_power": child})
    assert plan is not None
    entry = plan.children[0]
    assert entry.key == "switch_pc_power"
    assert entry.product.prod_id == "ZG2R"
    assert entry.friendly_name == "电脑电源"
    assert entry.services[0].service_type == "switch"
    assert entry.services[0].service_id == "switch1"
    on_binding = next(b for b in entry.bindings if b.field_name == "on")
    assert on_binding.entity_id == "switch.pc_power"
    assert on_binding.source == "state"
    assert on_binding.ha_to_huawei["map"] == {"on": 1, "off": 0}


def test_light_products_by_capability():
    cases = [
        ({"supported_color_modes": ["rgb", "color_temp"]}, "ZG0O"),
        ({"supported_color_modes": ["color_temp"]}, "ZG13"),
        ({"supported_color_modes": ["brightness"]}, "ZG14"),
        ({"supported_color_modes": ["onoff"]}, "ZG0S"),
        ({}, "ZG0S"),
        ({"brightness": 128}, "ZG14"),
    ]
    for attributes, expected in cases:
        child = build_child_mapping(
            "light.test", name="测试灯", attributes=attributes
        )
        plan = _compile({"light_test": child})
        assert plan.children[0].product.prod_id == expected, attributes
        service_ids = {s.service_id for s in plan.children[0].services}
        if expected == "ZG0O":
            assert service_ids == {"subProduct", "switch", "brightness", "cct", "colour"}
        elif expected == "ZG13":
            assert service_ids == {"switch", "brightness", "cct"}
        elif expected == "ZG14":
            assert service_ids == {"switch", "brightness"}
        else:
            assert service_ids == {"switch"}


def test_full_color_child_bindings():
    child = build_child_mapping(
        "light.rgb", name="全彩灯", attributes={"supported_color_modes": ["rgb"]}
    )
    plan = _compile({"light_rgb": child})
    entry = plan.children[0]
    sub = next(b for b in entry.bindings if b.service_id == "subProduct")
    assert sub.source == "constant" and sub.value == "05"
    colour_fields = {
        b.field_name: b for b in entry.bindings if b.service_id == "colour"
    }
    assert set(colour_fields) == {"red", "green", "blue"}
    assert all(b.component in {"red", "green", "blue"} for b in colour_fields.values())


def test_lock_child_maps_locked_state():
    child = build_child_mapping("lock.tank300", name="坦克300")
    plan = _compile({"lock_tank300": child})
    entry = plan.children[0]
    assert entry.product.prod_id == "ZG2R"
    on_binding = next(b for b in entry.bindings if b.field_name == "on")
    assert on_binding.ha_to_huawei["map"]["locked"] == 1
    assert on_binding.ha_to_huawei["map"]["unlocked"] == 0
    assert on_binding.ha_to_huawei["map"]["jammed"] == 0


def test_momentary_children_use_constant_off():
    for entity_id in ("script.leave_home", "scene.movie_night", "button.restart"):
        domain = entity_id.split(".")[0]
        child = build_child_mapping(entity_id, name="瞬时")
        plan = _compile({f"ha_{domain}": child})
        on_binding = next(b for b in plan.children[0].bindings if b.field_name == "on")
        assert on_binding.source == "constant"
        assert on_binding.value == 0
        assert on_binding.entity_id == entity_id


def test_unsupported_domain_rejected():
    with pytest.raises(ValueError):
        build_child_mapping("climate.ac", name="空调")


def test_apply_selection_preserves_handwritten_children():
    handwritten = {
        "oslo_one_key_switch": {
            "display": {"friendly_name": "奥斯陆一键开关"},
            "product": {"ref": "ZG2R", "services": [{"st": "switch", "sid": "switch1"}]},
            "bindings": [],
        }
    }
    managed = build_child_mapping("switch.pc", name="PC")
    raw = {
        "schema": 1,
        "enabled": True,
        "children": {**handwritten, "switch_pc": managed},
    }
    # 取消勾选 switch.pc：手写条目保留，受管条目删除
    result = apply_entity_selection(raw, gateway_serial="S1", selected_children={})
    assert "oslo_one_key_switch" in result["children"]
    assert "switch_pc" not in result["children"]


def test_apply_selection_rejects_key_conflict():
    raw = {"children": {"switch_pc": {"product": {"ref": "ZG2R"}}}}
    generated = build_child_mapping("switch.pc", name="PC")
    with pytest.raises(ValueError):
        apply_entity_selection(
            raw, gateway_serial="S1", selected_children={"switch_pc": generated}
        )


def test_apply_selection_enforces_limit():
    raw: dict = {}
    selected = {
        f"key_{index}": {"product": {"ref": "ZG2R"}} for index in range(65)
    }
    with pytest.raises(ValueError):
        apply_entity_selection(raw, gateway_serial="S1", selected_children=selected)


def test_managed_children_and_bound_entity():
    raw = {
        "children": {
            "hand": {"product": {"ref": "ZG2R"}},
            "managed": build_child_mapping("switch.pc", name="PC"),
        }
    }
    managed = managed_children(raw)
    assert list(managed) == ["managed"]
    assert bound_entity_of(managed["managed"]) == "switch.pc"
    assert bound_entity_of(raw["children"]["hand"]) is None
    assert is_managed_child(managed["managed"])
    assert not is_managed_child(raw["children"]["hand"])
