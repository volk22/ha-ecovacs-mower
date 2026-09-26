"""Entities for the A1600 fork: per-area cutting settings, protections, schedule.

All of them read :class:`.a1600_runtime.A1600State` and refresh on its
dispatcher signal; writes go through the captured commands in
``deebot_patch/a1600.py``. Built only for devices with an A1600 runtime, so
other GOAT classes are untouched.

Value meanings marked "unverified" come from O1200 research
(PhilippF1992/deebot-goat-docs) and still need checking against the app on
an A1600; the raw level is always shown alongside.
"""

from __future__ import annotations

from datetime import time as dtime
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .a1600_runtime import SIGNAL_UPDATE, A1600Runtime, runtimes
from .const import DOMAIN
from .deebot_patch.a1600 import SetAnimProtect, SetAreaParameter, SetAutoCutDirection

if TYPE_CHECKING:
    from .controller import EcovacsController

AREA_ID = "1"  # the A1600 in the capture has one lawn area

# cutMode values seen from the app: 6 and 7. O1200: 7 = gentle, 4 = efficient.
CUT_MODES = {"6": "mode 6", "7": "mode 7 (gentle?)", "4": "mode 4 (efficient?)"}
OBSTACLE_HEIGHTS = {"1": "≤10 cm", "2": "≤15 cm", "3": "≤20 cm"}
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _runtimes_for(controller: EcovacsController) -> list[A1600Runtime]:
    return [r for r in runtimes(controller._hass).values() if r.controller is controller]  # noqa: SLF001


class _A1600Entity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, runtime: A1600Runtime, key: str, name: str) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.did}_a1600_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.did)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self.runtime.did), self.async_write_ha_state
            )
        )

    @property
    def _params(self) -> dict[str, Any] | None:
        return self.runtime.state.area_param(AREA_ID)

    async def _set_param(self, **changes: Any) -> None:
        p = self._params
        if p is None:
            return
        merged = {
            "mow_height_level": p["mowHeightLevel"],
            "cut_mode": p["cutMode"],
            "obstacle_height": p["obstacleHeight"],
            "angle": p["angle"],
            **changes,
        }
        await self.runtime.execute(SetAreaParameter(area_id=AREA_ID, **merged))
        self.runtime.refresh_settings()


# --------------------------------------------------------------------- number


class MowHeightLevel(_A1600Entity, NumberEntity):
    """Cutting height level (app shows cm; mapping unverified on A1600)."""

    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_native_min_value = 1
    _attr_native_max_value = 12
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "mow_height_level", "Mowing height level")

    @property
    def native_value(self) -> float | None:
        return p["mowHeightLevel"] if (p := self._params) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if (p := self._params) is None:
            return {}
        # O1200 mapping: cm = 8.5 - 0.5 * level. Unverified on the A1600.
        return {"approx_cm_unverified": 8.5 - 0.5 * p["mowHeightLevel"]}

    async def async_set_native_value(self, value: float) -> None:
        await self._set_param(mow_height_level=int(value))


class CutAngle(_A1600Entity, NumberEntity):
    """Cutting direction as the app shows it; wire value = (270 - app) mod 360 (unverified)."""

    _attr_icon = "mdi:angle-acute"
    _attr_native_min_value = 0
    _attr_native_max_value = 359
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "°"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "cut_angle", "Cutting angle (raw)")

    @property
    def native_value(self) -> float | None:
        return p["angle"] if (p := self._params) else None

    async def async_set_native_value(self, value: float) -> None:
        await self._set_param(angle=int(value))


# --------------------------------------------------------------------- select


class CutMode(_A1600Entity, SelectEntity):
    _attr_icon = "mdi:speedometer"
    _attr_options = list(CUT_MODES.values())

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "cut_mode", "Cutting mode")

    @property
    def current_option(self) -> str | None:
        if (p := self._params) is None:
            return None
        return CUT_MODES.get(str(p["cutMode"]), f"mode {p['cutMode']}")

    async def async_select_option(self, option: str) -> None:
        raw = next(k for k, v in CUT_MODES.items() if v == option)
        await self._set_param(cut_mode=int(raw))


class ObstacleHeight(_A1600Entity, SelectEntity):
    _attr_icon = "mdi:image-filter-hdr"
    _attr_options = list(OBSTACLE_HEIGHTS.values())

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "obstacle_height", "Obstacle height")

    @property
    def current_option(self) -> str | None:
        if (p := self._params) is None:
            return None
        return OBSTACLE_HEIGHTS.get(str(p["obstacleHeight"]))

    async def async_select_option(self, option: str) -> None:
        raw = next(k for k, v in OBSTACLE_HEIGHTS.items() if v == option)
        await self._set_param(obstacle_height=int(raw))


# --------------------------------------------------------------------- switch


class AutoCutDirection(_A1600Entity, SwitchEntity):
    _attr_icon = "mdi:rotate-3d-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "auto_cut_direction", "Automatic cutting direction")

    @property
    def is_on(self) -> bool | None:
        return self.runtime.state.auto_cut_direction

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.execute(SetAutoCutDirection(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.execute(SetAutoCutDirection(False))


class AnimalProtection(_A1600Entity, SwitchEntity):
    _attr_icon = "mdi:paw"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "animal_protection_setting", "Animal protection (night pause)")

    @property
    def is_on(self) -> bool | None:
        a = self.runtime.state.anim_protect
        return a["enable"] if a else None

    async def _write(self, **changes: Any) -> None:
        a = self.runtime.state.anim_protect
        if a is None:
            return
        await self.runtime.execute(SetAnimProtect(**{**a, **changes}))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(enable=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(enable=False)


# ----------------------------------------------------------------------- time


def _parse_hm(value: str) -> dtime | None:
    try:
        h, m = value.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return None


class AnimalProtectionTime(_A1600Entity, TimeEntity):
    _attr_icon = "mdi:clock-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: A1600Runtime, which: str) -> None:
        self._which = which
        super().__init__(runtime, f"animal_protection_{which}", f"Animal protection {which}")

    @property
    def native_value(self) -> dtime | None:
        a = self.runtime.state.anim_protect
        return _parse_hm(a[self._which]) if a else None

    async def async_set_value(self, value: dtime) -> None:
        a = self.runtime.state.anim_protect
        if a is None:
            return
        # The app writes H:M without leading zeros ("19:0").
        await self.runtime.execute(
            SetAnimProtect(**{**a, self._which: f"{value.hour}:{value.minute}"})
        )


# --------------------------------------------------------------------- sensor


class ScheduleSensor(_A1600Entity, SensorEntity):
    """Number of enabled schedule tasks; the full list is in the attributes."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "schedule", "Schedule")

    @property
    def native_value(self) -> int | None:
        plans = self.runtime.state.plans
        if not plans:
            return None
        return sum(1 for t in plans[0]["tasks"] if int(t.get("isOpen", 0)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plans = self.runtime.state.plans
        if not plans:
            return {}
        tasks = []
        for t in plans[0]["tasks"]:
            kind = {1: "whole lawn", 2: "areas", 3: "edges"}.get(int(t.get("mowType", 0)), "?")
            tasks.append(
                {
                    "ssid": t.get("ssid"),
                    "day": WEEKDAYS[int(t.get("sDay", 0)) % 7],
                    "start": t.get("sTime"),
                    "end": t.get("eTime"),
                    "type": kind,
                    "enabled": bool(int(t.get("isOpen", 0))),
                    "duration_min": round(int(t["duration"]) / 60) if t.get("duration") else None,
                }
            )
        return {"plan": plans[0].get("name"), "active": bool(plans[0].get("using")), "tasks": tasks}


class AvoidPointsSensor(_A1600Entity, SensorEntity):
    """How many avoid points the mower keeps; ids and coordinates in attributes."""

    _attr_icon = "mdi:map-marker-alert"

    def __init__(self, runtime: A1600Runtime) -> None:
        super().__init__(runtime, "avoid_points", "Avoid points")

    @property
    def native_value(self) -> int:
        return len(self.runtime.state.avoid_points)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"points": self.runtime.state.avoid_points, "map_updated": self.runtime.state.map_updated}


# -------------------------------------------------------------- per platform


def numbers(controller: EcovacsController) -> list[Entity]:
    return [e for r in _runtimes_for(controller) for e in (MowHeightLevel(r), CutAngle(r))]


def selects(controller: EcovacsController) -> list[Entity]:
    return [e for r in _runtimes_for(controller) for e in (CutMode(r), ObstacleHeight(r))]


def switches(controller: EcovacsController) -> list[Entity]:
    return [e for r in _runtimes_for(controller) for e in (AutoCutDirection(r), AnimalProtection(r))]


def times(controller: EcovacsController) -> list[Entity]:
    return [
        e
        for r in _runtimes_for(controller)
        for e in (AnimalProtectionTime(r, "start"), AnimalProtectionTime(r, "end"))
    ]


def sensors(controller: EcovacsController) -> list[Entity]:
    return [e for r in _runtimes_for(controller) for e in (ScheduleSensor(r), AvoidPointsSensor(r))]
