"""Home Assistant side of the A1600 fork: state, map API, services, card.

Holds what ``deebot_patch/a1600.py`` decodes (id-carrying map layers, avoid
points, areas, per-area parameters, schedules, protections), persists the
geometry, serves it to the Lovelace card as JSON, and exposes the write
commands as services. Everything is keyed by device ``did``.

Hooked in with one call from ``controller._setup_map`` and one from
``async_setup``; nothing else in upstream code depends on this module.

Switches (``configuration.yaml`` is not needed — all of these are code-level):
* ``MAP_REFRESH_INTERVAL`` — how often the map is requested (``getMI``).
* The card is registered as a frontend module automatically; remove the
  ``add_extra_js_url`` call to stop that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
import voluptuous as vol

from deebot_client.events import StateEvent
from deebot_client.models import State

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .deebot_patch.a1600 import (
    A1600_CLASSES,
    A1600LayersEvent,
    AnimProtectEvent,
    AppPing,
    AreaParametersEvent,
    AreaSetEvent,
    AutoCutDirectionEvent,
    AvoidPointsEvent,
    DeleteAvoidPoint,
    SchedulesEvent,
    SetAreaParameter,
    SetSchedules,
)
from .deebot_patch.a1600_history import HistoryError, list_sessions, session_detail, track_segments
from .deebot_patch.map_messages import MowerMapInfoEvent

if TYPE_CHECKING:
    from deebot_client.device import Device

    from .controller import EcovacsController

_LOGGER = logging.getLogger(__name__)

MAP_REFRESH_INTERVAL = timedelta(minutes=30)
# The mower streams onPos/onMapTrack only while an appping lease is live; the
# app renews it about every 240 s (lease ~300 s). Renewed while a job runs.
LEASE_INTERVAL = timedelta(seconds=240)
HISTORY_REFRESH_INTERVAL = timedelta(hours=2)
STORE_VERSION = 1
SAVE_DELAY = 10
SIGNAL_UPDATE = f"{DOMAIN}_a1600_update_{{}}"
CARD_URL = f"/{DOMAIN}_static"
CARD_FILE = "ecovacs-goat-map-card.js"
_DATA_KEY = f"{DOMAIN}_a1600"


@dataclass
class A1600State:
    """Latest decoded A1600 data for one mower."""

    did: str
    boundary: list[list[int]] = field(default_factory=list)
    islands: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    obstacles: list[dict[str, Any]] = field(default_factory=list)
    area_ids: list[str] = field(default_factory=list)
    avoid_points: list[dict[str, Any]] = field(default_factory=list)
    nogo: list[dict[str, Any]] = field(default_factory=list)
    areas: list[list[str]] = field(default_factory=list)  # getAreaSet ar rows
    island_rows: list[list[str]] = field(default_factory=list)  # getAreaSet vw rows
    area_params: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    anim_protect: dict[str, Any] | None = None
    auto_cut_direction: bool | None = None
    map_updated: str | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    last_session_lanes: list[list[list[int]]] = field(default_factory=list)
    history_error: str | None = None

    _STORED = (
        "boundary", "islands", "edges", "obstacles", "area_ids",
        "avoid_points", "nogo", "areas", "island_rows", "map_updated",
        "sessions", "last_session_lanes",
    )

    def stored(self) -> dict[str, Any]:
        """Geometry worth keeping across restarts; settings re-read on start."""
        return {key: getattr(self, key) for key in self._STORED}

    def restore(self, data: dict[str, Any]) -> None:
        """Load a stored snapshot."""
        for key in self._STORED:
            if key in data:
                setattr(self, key, data[key])

    def zones(self) -> list[dict[str, Any]]:
        """Lawn areas with a label anchor, from ``getAreaSet ar`` rows.

        Row shape ``[mid, id, name, neighbours, x, y, flags]``.
        """
        result = []
        for row in self.areas:
            if len(row) >= 6:
                try:
                    result.append(
                        {"id": row[1], "name": row[2], "x": int(float(row[4])), "y": int(float(row[5]))}
                    )
                except ValueError:
                    continue
        return result

    def area_param(self, area_id: str) -> dict[str, Any] | None:
        """Parameters of one area."""
        return next((p for p in self.area_params if str(p.get("areaID")) == str(area_id)), None)


class A1600Runtime:
    """Per-device glue: subscriptions, persistence, periodic map refresh."""

    def __init__(self, hass: HomeAssistant, controller: EcovacsController, device: Device) -> None:
        self.hass = hass
        self.controller = controller
        self.device = device
        self.did: str = device.device_info["did"]
        self.state = A1600State(self.did)
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.a1600_{self.did}")
        self._unsubs: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        """Restore, subscribe (which sends the refresh commands), start the timer."""
        if data := await self._store.async_load():
            self.state.restore(data)
        events = self.device.events
        events.subscribe(A1600LayersEvent, self._on_layers)
        events.subscribe(AvoidPointsEvent, self._on_avoid)
        events.subscribe(AreaSetEvent, self._on_area_set)
        events.subscribe(AreaParametersEvent, self._on_params)
        events.subscribe(SchedulesEvent, self._on_schedules)
        events.subscribe(AnimProtectEvent, self._on_anim)
        events.subscribe(AutoCutDirectionEvent, self._on_auto_cut)
        # The map arrives in answer to the getMI upstream sends when it subscribes
        # to MowerMapInfoEvent — before these subscriptions exist. EventBus keeps
        # the last event even with no subscriber and drops an equal one later, so
        # a re-sent identical map would never reach us: seed from what it kept.
        for event_type, handler in (
            (A1600LayersEvent, self._on_layers),
            (AvoidPointsEvent, self._on_avoid),
            (AreaSetEvent, self._on_area_set),
            (AreaParametersEvent, self._on_params),
            (SchedulesEvent, self._on_schedules),
            (AnimProtectEvent, self._on_anim),
            (AutoCutDirectionEvent, self._on_auto_cut),
        ):
            if (last := events.get_last_event(event_type)) is not None:
                await handler(last)
        # Upstream's startup getMI went out without the appping lease, which the
        # mower needs before it pushes the map; ask again properly. This also
        # re-asks areas and islands, which share one event type (only the later
        # of the two was kept; alternating kinds are never equal).
        self.refresh_map()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._periodic_refresh, MAP_REFRESH_INTERVAL)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._periodic_history, HISTORY_REFRESH_INTERVAL)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._renew_lease, LEASE_INTERVAL)
        )
        events.subscribe(StateEvent, self._on_state)
        self.hass.async_create_task(self.async_refresh_history())

    async def async_stop(self) -> None:
        """Flush and detach."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self.state.stored())

    @callback
    def _periodic_refresh(self, _now: Any = None) -> None:
        self.refresh_map()

    def refresh_map(self) -> None:
        """Ask the mower for its map: ``appping`` lease first, then ``getMI`` and areas."""
        self.hass.async_create_task(self._async_refresh_map())

    async def _async_refresh_map(self) -> None:
        await self.device.execute_command(AppPing())
        self.device.events.request_refresh(MowerMapInfoEvent)
        self.device.events.request_refresh(AreaSetEvent)

    def _job_running(self) -> bool:
        last = self.device.events.get_last_event(StateEvent)
        return last is not None and last.state in (State.CLEANING, State.RETURNING, State.PAUSED)

    @callback
    def _renew_lease(self, _now: Any = None) -> None:
        if self._job_running():
            self.hass.async_create_task(self.device.execute_command(AppPing()))

    async def _on_state(self, event: StateEvent) -> None:
        # Start the live stream as soon as a job starts, not up to 4 min later.
        if event.state in (State.CLEANING, State.RETURNING):
            await self.device.execute_command(AppPing())

    @callback
    def _periodic_history(self, _now: Any = None) -> None:
        self.hass.async_create_task(self.async_refresh_history())

    async def async_refresh_history(self) -> list[dict[str, Any]]:
        """Fetch the session list, and the mowed track of the newest session."""
        authenticator = self.controller._authenticator  # noqa: SLF001
        try:
            raw = await list_sessions(authenticator, self.device.device_info)
            sessions = [_session_summary(item) for item in raw]
            lanes: list[list[list[int]]] = self.state.last_session_lanes
            if sessions and sessions[0]["id"] and (
                not self.state.sessions or self.state.sessions[0]["id"] != sessions[0]["id"] or not lanes
            ):
                detail = await session_detail(authenticator, sessions[0]["id"])
                lanes = track_segments(detail.get("mapTrack"))
        except (HistoryError, OSError, TimeoutError, ValueError, KeyError) as err:
            self.state.history_error = str(err)
            _LOGGER.warning("Mowing history unavailable: %s", err)
            self._changed()
            return self.state.sessions
        self.state.sessions = sessions
        self.state.last_session_lanes = lanes
        self.state.history_error = None
        self._changed(save=True)
        return sessions

    def refresh_settings(self) -> None:
        """Re-read parameters and schedules after a write."""
        self.device.events.request_refresh(AreaParametersEvent)
        self.device.events.request_refresh(SchedulesEvent)

    def _changed(self, save: bool = False) -> None:
        if save:
            self._store.async_delay_save(self.state.stored, SAVE_DELAY)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.did))

    async def _on_layers(self, event: A1600LayersEvent) -> None:
        layers = event.layers
        s = self.state
        if layers.boundary is not None:
            s.boundary = [list(p) for p in layers.boundary]
        if layers.islands is not None:
            s.islands = [i.as_dict() for i in layers.islands]
        if layers.edges is not None:
            s.edges = [i.as_dict() for i in layers.edges]
        if layers.obstacles is not None:
            s.obstacles = [i.as_dict() for i in layers.obstacles]
        if layers.area_ids is not None:
            s.area_ids = layers.area_ids
        from homeassistant.util import dt as dt_util

        s.map_updated = dt_util.utcnow().isoformat()
        self._changed(save=True)

    async def _on_avoid(self, event: AvoidPointsEvent) -> None:
        self.state.avoid_points = [p.as_dict() for p in event.points]
        self.state.nogo = [i.as_dict() for i in event.nogo]
        self._changed(save=True)

    async def _on_area_set(self, event: AreaSetEvent) -> None:
        if event.kind == "ar":
            self.state.areas = event.rows
        elif event.kind == "vw":
            self.state.island_rows = event.rows
        self._changed(save=True)

    async def _on_params(self, event: AreaParametersEvent) -> None:
        self.state.area_params = event.params
        self._changed()

    async def _on_schedules(self, event: SchedulesEvent) -> None:
        self.state.plans = event.plans
        self._changed()

    async def _on_anim(self, event: AnimProtectEvent) -> None:
        self.state.anim_protect = {"enable": event.enable, "start": event.start, "end": event.end}
        self._changed()

    async def _on_auto_cut(self, event: AutoCutDirectionEvent) -> None:
        self.state.auto_cut_direction = event.enable
        self._changed()

    async def execute(self, command: Any) -> None:
        """Send a write command and fail the service call if it is not acknowledged."""
        if not await self.device.execute_command(command):
            raise HomeAssistantError(
                f"The mower did not confirm {command.NAME}; see the deebot_client log"
            )

    def as_json(self) -> dict[str, Any]:
        """The card's data contract (see frontend/ecovacs-goat-map-card.js)."""
        s = self.state
        mower_map = self.controller.maps.get(self.did)
        data: dict[str, Any] = {
            "did": self.did,
            "updated": s.map_updated,
            "boundary": s.boundary,
            "islands": s.islands,
            "edges": s.edges,
            "obstacles": s.obstacles,
            "avoid_points": s.avoid_points,
            "nogo": s.nogo,
            "zones": s.zones() or [{"id": a, "name": "", "x": None, "y": None} for a in s.area_ids],
            "dock": [0, 0],
            "position": None,
            "heading": 0,
            "track": [],
            "lanes": [],
        }
        if mower_map is not None:
            data["dock"] = list(mower_map.dock)
            data["position"] = list(mower_map.position) if mower_map.position else None
            data["heading"] = mower_map.heading
            data["track"] = [list(p) for p in mower_map.track]
            data["lanes"] = [
                [list(a), list(b)] for segments in mower_map.lanes.values() for a, b in segments
            ]
        if not data["lanes"]:
            # Docked: show what the last session mowed.
            data["lanes"] = s.last_session_lanes
        return data


def _session_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Keep the useful fields of one ``clean_result/list`` entry."""
    from homeassistant.util import dt as dt_util

    ts = item.get("ts") or item.get("start")
    try:
        start = dt_util.utc_from_timestamp(int(ts) / (1000 if int(ts) > 1e11 else 1)).isoformat()
    except (TypeError, ValueError):
        start = None
    area = item.get("area")
    last = item.get("last")
    return {
        "id": item.get("id"),
        "start": start,
        # area comes in cm² (262.5 m² arrives as 2625400), "last" in seconds.
        "area_m2": round(int(area) / 10000, 1) if str(area or "").isdigit() else area,
        "duration_min": round(int(last) / 60) if str(last or "").isdigit() else last,
        "stop_reason": item.get("stopReason"),
        "type": item.get("type"),
    }


# ------------------------------------------------------------------ registry


def runtimes(hass: HomeAssistant) -> dict[str, A1600Runtime]:
    """All A1600 runtimes by did."""
    return hass.data.setdefault(_DATA_KEY, {})


async def async_setup_device(hass: HomeAssistant, controller: EcovacsController, device: Device) -> None:
    """Start the A1600 runtime for one device (called from ``_setup_map``)."""
    if device.device_info["class"] not in A1600_CLASSES:
        return
    runtime = A1600Runtime(hass, controller, device)
    runtimes(hass)[runtime.did] = runtime
    await runtime.async_start()


async def async_teardown(hass: HomeAssistant, controller: EcovacsController) -> None:
    """Stop the runtimes that belong to *controller*."""
    for did, runtime in list(runtimes(hass).items()):
        if runtime.controller is controller:
            await runtime.async_stop()
            runtimes(hass).pop(did, None)


def _runtime_for_entity(hass: HomeAssistant, entity_id: str) -> A1600Runtime:
    ent = er.async_get(hass).async_get(entity_id)
    if ent is None or ent.device_id is None:
        raise ServiceValidationError(f"{entity_id} is not an ecovacs_mower entity")
    device = dr.async_get(hass).async_get(ent.device_id)
    if device is not None:
        for domain, did in device.identifiers:
            if domain == DOMAIN and did in runtimes(hass):
                return runtimes(hass)[did]
    raise ServiceValidationError(f"{entity_id} does not belong to a GOAT A1600")


# -------------------------------------------------------------------- HTTP


class A1600MapView(HomeAssistantView):
    """``GET /api/ecovacs_mower/map?entity_id=…`` → map JSON for the card."""

    url = f"/api/{DOMAIN}/map"
    name = f"api:{DOMAIN}:map"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the map of the mower the entity belongs to."""
        hass: HomeAssistant = request.app["hass"]
        entity_id = request.query.get("entity_id", "")
        try:
            runtime = _runtime_for_entity(hass, entity_id)
        except ServiceValidationError as err:
            return self.json_message(str(err), status_code=404)
        data = runtime.as_json()
        data["entity_id"] = entity_id
        if (st := hass.states.get(entity_id)) is not None:
            data["state"] = st.state
            data["mowing"] = st.state == "mowing"
        return self.json(data)


# ----------------------------------------------------------------- services

_ENTITY = {vol.Required("entity_id"): cv.entity_id}


async def async_setup_global(hass: HomeAssistant) -> None:
    """Register the view, services and card once per HA instance."""
    if hass.data.get(f"{_DATA_KEY}_global"):
        return
    hass.data[f"{_DATA_KEY}_global"] = True

    hass.http.register_view(A1600MapView())
    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(frontend_dir), cache_headers=False)]
    )
    from homeassistant.components.frontend import add_extra_js_url

    version = (frontend_dir / CARD_FILE).stat().st_mtime_ns if (frontend_dir / CARD_FILE).exists() else 0
    add_extra_js_url(hass, f"{CARD_URL}/{CARD_FILE}?v={version}")

    async def refresh_map(call: ServiceCall) -> None:
        _runtime_for_entity(hass, call.data["entity_id"]).refresh_map()

    async def delete_avoid_point(call: ServiceCall) -> None:
        runtime = _runtime_for_entity(hass, call.data["entity_id"])
        mssid = str(call.data["mssid"])
        point = next((p for p in runtime.state.avoid_points if str(p["mssid"]) == mssid), None)
        if point is None:
            raise ServiceValidationError(f"No avoid point with mssid {mssid}")
        _LOGGER.info("Deleting avoid point %s at %s,%s", mssid, point["x"], point["y"])
        await runtime.execute(DeleteAvoidPoint(mssid, point["x"], point["y"]))

    async def set_area_parameter(call: ServiceCall) -> None:
        runtime = _runtime_for_entity(hass, call.data["entity_id"])
        area_id = str(call.data.get("area_id", "1"))
        current = runtime.state.area_param(area_id)
        if current is None:
            raise ServiceValidationError(f"Parameters of area {area_id} are not known yet")
        await runtime.execute(
            SetAreaParameter(
                area_id=area_id,
                mow_height_level=call.data.get("mow_height_level", current["mowHeightLevel"]),
                cut_mode=call.data.get("cut_mode", current["cutMode"]),
                obstacle_height=call.data.get("obstacle_height", current["obstacleHeight"]),
                angle=call.data.get("angle", current["angle"]),
            )
        )
        runtime.refresh_settings()

    async def schedule_task(call: ServiceCall) -> None:
        runtime = _runtime_for_entity(hass, call.data["entity_id"])
        action = call.data["action"]
        plan = next(iter(runtime.state.plans), None)
        if plan is None:
            raise ServiceValidationError("Schedules are not known yet")
        ssid = call.data.get("ssid")
        existing = next((t for t in plan["tasks"] if str(t.get("ssid")) == str(ssid)), None)
        if action in ("mod", "del", "enable", "disable") and existing is None:
            raise ServiceValidationError(f"No schedule task with ssid {ssid}")
        if action in ("enable", "disable"):
            task, act = {**existing, "isOpen": 1 if action == "enable" else 0}, "mod"
        elif action == "del":
            task, act = dict(existing), "del"
        else:
            fields = {
                k: call.data[k]
                for k in ("sDay", "eDay", "sTime", "eTime", "mowType", "ids", "duration", "isOpen")
                if k in call.data
            }
            if action == "add":
                task = {"workType": 1, "isOpen": 1, "mowType": 1, **fields}
                task.setdefault("eDay", task.get("sDay"))
            else:
                task = {**existing, **fields}
            act = action
        await runtime.execute(SetSchedules(plan, task, act))
        runtime.refresh_settings()

    hass.services.async_register(DOMAIN, "refresh_map", refresh_map, vol.Schema(_ENTITY))

    async def fetch_history(call: ServiceCall) -> dict[str, Any]:
        sessions = await _runtime_for_entity(hass, call.data["entity_id"]).async_refresh_history()
        return {"sessions": sessions}

    from homeassistant.core import SupportsResponse

    hass.services.async_register(
        DOMAIN, "fetch_history", fetch_history, vol.Schema(_ENTITY),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "delete_avoid_point",
        delete_avoid_point,
        vol.Schema({**_ENTITY, vol.Required("mssid"): vol.Coerce(str)}),
    )
    hass.services.async_register(
        DOMAIN,
        "set_area_parameter",
        set_area_parameter,
        vol.Schema(
            {
                **_ENTITY,
                vol.Optional("area_id", default="1"): vol.Coerce(str),
                vol.Optional("mow_height_level"): vol.All(int, vol.Range(1, 12)),
                vol.Optional("cut_mode"): vol.In([4, 6, 7]),
                vol.Optional("obstacle_height"): vol.In([1, 2, 3]),
                vol.Optional("angle"): vol.All(int, vol.Range(0, 359)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "schedule_task",
        schedule_task,
        vol.Schema(
            {
                **_ENTITY,
                vol.Required("action"): vol.In(["add", "mod", "del", "enable", "disable"]),
                vol.Optional("ssid"): vol.Coerce(str),
                vol.Optional("sDay"): vol.All(int, vol.Range(0, 6)),
                vol.Optional("eDay"): vol.All(int, vol.Range(0, 6)),
                vol.Optional("sTime"): cv.matches_regex(r"^\d{2}:\d{2}$"),
                vol.Optional("eTime"): cv.matches_regex(r"^\d{2}:\d{2}$"),
                vol.Optional("mowType"): vol.In([1, 2, 3]),
                vol.Optional("ids"): str,
                vol.Optional("duration"): int,
            }
        ),
    )
