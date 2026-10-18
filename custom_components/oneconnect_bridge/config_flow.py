from __future__ import annotations

import secrets
import socket
from pathlib import Path
from typing import Any

import psutil
import voluptuous as vol
import yaml
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .business.entity_mapping import (
    apply_entity_selection,
    bound_entity_of,
    build_child_mapping,
    child_key_for_entity,
    is_managed_child,
    managed_children,
)
from .const import BRIDGEABLE_DOMAINS, CONF_INTERFACE_IP, CONF_MAC, CONF_SERIAL, DOMAIN
from .storage import GatewayStore


def _interface_options() -> dict[str, str]:
    options: dict[str, str] = {}
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            if address.address.startswith("127."):
                continue
            options[address.address] = f"{name} — {address.address}"
    return options


def _interface_mac(interface_ip: str) -> str:
    for _name, addresses in psutil.net_if_addrs().items():
        ipv4 = {address.address for address in addresses if address.family == socket.AF_INET}
        if interface_ip not in ipv4:
            continue
        for address in addresses:
            if getattr(address.family, "name", "") in {"AF_LINK", "AF_PACKET"}:
                value = address.address.replace(":", "").replace("-", "").upper()
                if len(value) == 12:
                    return value
    raise ValueError(f"no MAC address found for interface {interface_ip}")


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create the HA entry and let the gateway pair asynchronously."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        if not options:
            return self.async_abort(reason="no_ipv4_interface")

        errors: dict[str, str] = {}
        if user_input is not None:
            interface_ip = user_input[CONF_INTERFACE_IP]
            try:
                mac = await self.hass.async_add_executor_job(
                    _interface_mac, interface_ip
                )
            except ValueError:
                errors[CONF_INTERFACE_IP] = "mac_not_found"
            else:
                serial = secrets.token_hex(8).upper()
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=serial,
                    data={
                        CONF_INTERFACE_IP: interface_ip,
                        CONF_MAC: mac,
                        CONF_SERIAL: serial,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_INTERFACE_IP): vol.In(options)}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    """Gateway options flow: entity selection plus the LAN interface choice."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            if user_input["action"] == "add_entities":
                return await self.async_step_add_entities()
            return await self.async_step_interface()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="add_entities"): vol.In(
                        {
                            "add_entities": "添加实体（勾选 HA 实体接入智慧生活）",
                            "interface": "配对网卡设置",
                        }
                    )
                }
            ),
        )

    async def async_step_interface(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        if user_input is not None:
            interface_ip = user_input[CONF_INTERFACE_IP]
            mac = await self.hass.async_add_executor_job(_interface_mac, interface_ip)
            return self.async_create_entry(
                title="",
                data={CONF_INTERFACE_IP: interface_ip, CONF_MAC: mac},
            )
        return self.async_show_form(
            step_id="interface",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTERFACE_IP,
                        default=self._entry.data.get(CONF_INTERFACE_IP),
                    ): vol.In(options)
                }
            ),
        )

    async def async_step_add_entities(self, user_input=None):
        """HomeKit-Bridge 式实体勾选：写入 mapping.yaml 并触发子设备重载。

        - 列出可桥接 domain 的全部实体；已受管但当前不可用的实体保留选项
        - 勾选差集立即生效：新增 → 生成 children 注册上云，取消 → 删除并下云
        - 手写 children（无 managed_by 标记）原样保留，互不覆盖
        """
        serial = self._entry.data[CONF_SERIAL]
        store = GatewayStore(self.hass.config.path(), serial)
        mapping_path = await self.hass.async_add_executor_job(store.path, "mapping.yaml")
        raw = await self.hass.async_add_executor_job(_read_mapping, mapping_path)

        managed = managed_children(raw)
        current_entities = {
            entity_id
            for entity_id in (bound_entity_of(child) for child in managed.values())
            if entity_id is not None
        }

        options: dict[str, str] = {}
        for state in sorted(
            self.hass.states.async_all(BRIDGEABLE_DOMAINS),
            key=lambda item: item.name or item.entity_id,
        ):
            domain = state.entity_id.split(".", 1)[0]
            options[state.entity_id] = f"{state.name or state.entity_id} ({domain})"
        for entity_id in sorted(current_entities):
            if entity_id not in options:
                options[entity_id] = f"{entity_id}（当前不可用）"

        if user_input is not None:
            selected: set[str] = set(user_input.get("entities", []))
            try:
                await self.hass.async_add_executor_job(
                    _write_selection, mapping_path, raw, serial, self.hass, selected
                )
            except Exception as error:  # noqa: BLE001 - surfaced in the form
                return self.async_show_form(
                    step_id="add_entities",
                    data_schema=self._entities_schema(options, current_entities),
                    errors={"base": "mapping_write_failed"},
                    description_placeholders={"error": str(error)},
                )
            reload_callback = self.hass.data[DOMAIN].get("reload_callbacks", {}).get(
                self._entry.entry_id
            )
            if reload_callback is not None:
                try:
                    await reload_callback()
                except Exception as error:  # noqa: BLE001 - surfaced in the form
                    return self.async_show_form(
                        step_id="add_entities",
                        data_schema=self._entities_schema(options, selected),
                        errors={"base": "reload_failed"},
                        description_placeholders={"error": str(error)},
                    )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="add_entities",
            data_schema=self._entities_schema(options, current_entities),
            description_placeholders={"entity_count": str(len(options))},
        )

    @staticmethod
    def _entities_schema(options: dict[str, str], selected: set[str]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    "entities",
                    default=[entity_id for entity_id in options if entity_id in selected],
                ): cv.multi_select(options)
            }
        )


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write_selection(
    path: Path,
    raw: dict[str, Any],
    serial: str,
    hass: Any,
    selected: set[str],
) -> None:
    """Rebuild managed children for the selection and rewrite mapping.yaml."""

    existing_children = (
        raw.get("children") if isinstance(raw.get("children"), dict) else {}
    )
    selected_children: dict[str, dict[str, Any]] = {}
    for entity_id in sorted(selected):
        key = child_key_for_entity(entity_id)
        existing = existing_children.get(key)
        if isinstance(existing, dict) and is_managed_child(existing):
            # 保留已受管条目（含手写微调），仅在实体被取消勾选时删除
            selected_children[key] = existing
            continue
        state = hass.states.get(entity_id)
        if state is None:
            continue
        selected_children[key] = build_child_mapping(
            entity_id, name=state.name, attributes=state.attributes
        )
    document = apply_entity_selection(
        raw, gateway_serial=serial, selected_children=selected_children
    )
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
