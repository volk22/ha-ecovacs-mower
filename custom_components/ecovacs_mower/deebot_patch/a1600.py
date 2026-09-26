"""GOAT A1600 LiDAR Pro (``e4gqia``) additions: map layers, settings, schedules.

Everything here was captured from the official app talking to an A1600 on
fw 1.13.10 (26.09.2026): the app's requests reach the device through the same
cloud MQTT relay Home Assistant subscribes to, so every payload below is a
verbatim copy of what the app sent, not a guess. Write commands are limited to
payloads seen on the wire.

Kept in its own module so the upstream files stay nearly untouched and a
rebase onto a new nord release stays cheap. Hooks into upstream:
``apply()`` calls :func:`register_messages`, ``patch_device_info`` calls
:func:`refresh_events` for A1600 classes.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.common import ExecuteCommand, JsonCommandWithMessageHandling
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict
from deebot_client.messages.json import MESSAGES

from .a1600_geometry import (
    A1600Layers,
    AvoidPoint,
    Item,
    decode_subsets,
    looks_like_a1600,
    parse_layers,
    parse_special_contour,
)
from .commands import GetMI
from .map_messages import (
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
    OnArI,
    OnSpecialContour,
)

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)

A1600_CLASSES = ("e4gqia",)


# --------------------------------------------------------------------- events


@dataclass(frozen=True)
class A1600LayersEvent(Event):
    """Id-carrying map layers; ``None`` fields mean "unchanged"."""

    layers: A1600Layers


@dataclass(frozen=True)
class AvoidPointsEvent(Event):
    """Avoid points and polygon no-go zones; complete current sets."""

    points: list[AvoidPoint]
    nogo: list[Item]


@dataclass(frozen=True)
class AreaSetEvent(Event):
    """``getAreaSet`` rows for one layer type (``ar`` areas, ``vw`` islands)."""

    kind: str
    rows: list[list[str]]


@dataclass(frozen=True)
class AreaParametersEvent(Event):
    """Per-area cutting parameters, one dict per area."""

    params: list[dict[str, Any]]


@dataclass(frozen=True)
class SchedulesEvent(Event):
    """Schedule plans with their decoded task lists."""

    plans: list[dict[str, Any]]


@dataclass(frozen=True)
class AnimProtectEvent(Event):
    """Animal protection window."""

    enable: bool
    start: str
    end: str


@dataclass(frozen=True)
class AutoCutDirectionEvent(Event):
    """Whether the mower varies its cutting direction on its own."""

    enable: bool


# ------------------------------------------------------------------- messages


class OnArIA1600(OnArI):
    """``onArI`` that understands the A1600 section layout.

    For an A1600 blob, upstream's reading puts an open trim line in
    ``boundary`` (drawn closed, it is the long chord across the lawn) and the
    real outline in ``corridors``. Here the upstream events are published with
    the corrected geometry, so the stock image entity draws the right lawn, and
    the id-carrying layers go out as :class:`A1600LayersEvent`. Any other blob
    falls through to upstream unchanged.
    """

    NAME = "onArI"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        entries = json.loads(blob)
        if not looks_like_a1600(entries):
            # Heartbeats with only obstacles updated carry no section 5/6;
            # those still decode as A1600 layers for the obstacle ids.
            sections = {e[1] for e in entries if len(e) > 2 and e[2] == "1"}
            if sections and sections <= {"3"}:
                layers = parse_layers(blob)
                event_bus.notify(A1600LayersEvent(layers=layers))
            return super()._notify(event_bus, blob)

        layers = parse_layers(blob)
        event_bus.notify(A1600LayersEvent(layers=layers))
        if layers.boundary is not None:
            event_bus.notify(
                MowerMapInfoEvent(
                    boundary=layers.boundary,
                    # Islands are holes in the lawn, not zones; upstream's
                    # renderer has no hole layer, so they are left out of it.
                    zones=[],
                    corridors=[],
                )
            )
        if layers.obstacles is not None:
            event_bus.notify(
                MowerObstaclesEvent(obstacles=[i.points for i in layers.obstacles])
            )
        return HandlingResult.success()


class OnSpecialContourA1600(OnSpecialContour):
    """``onSpecialContour`` split into avoid points (type 2) and no-go polygons.

    Upstream treats every record as a no-go polygon, so each avoid point became
    a one-point "polygon". Only real polygons go to upstream's no-go event now.
    """

    NAME = "onSpecialContour"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        contour = parse_special_contour(blob)
        event_bus.notify(AvoidPointsEvent(points=contour.avoid_points, nogo=contour.nogo))
        event_bus.notify(MowerNoGoZonesEvent(zones=[i.points for i in contour.nogo]))
        return HandlingResult.success()


def _decode_rows(data: dict[str, Any]) -> list[list[str]]:
    subsets = data.get("subsets")
    if not subsets:
        return []
    try:
        rows = decode_subsets(subsets)
    except (ValueError, TypeError):
        _LOGGER.debug("Undecodable subsets in %s", data)
        return []
    return rows if isinstance(rows, list) else []


class OnAreaSet(MessageBodyDataDict):
    """``getAreaSet`` answer / ``onAreaSet`` push."""

    NAME = "onAreaSet"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        kind = str(data.get("type", ""))
        event_bus.notify(AreaSetEvent(kind=kind, rows=_decode_rows(data)))
        return HandlingResult.success()


class OnAreaParameter(MessageBodyDataDict):
    """``getAreaParameter`` answer / ``onAreaParameter`` push."""

    NAME = "onAreaParameter"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        params = data.get("areaParameters")
        if isinstance(params, list):
            event_bus.notify(AreaParametersEvent(params=[dict(p) for p in params]))
        return HandlingResult.success()


class OnSchedules(MessageBodyDataDict):
    """``getSchedules`` answer / ``onSchedules`` push; task lists are LZMA blobs."""

    NAME = "onSchedules"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        plans: list[dict[str, Any]] = []
        for plan in data.get("list") or []:
            tasks = _decode_rows(plan)
            plans.append(
                {
                    "sid": str(plan.get("sid", "")),
                    "name": plan.get("name", ""),
                    "using": int(plan.get("using", 0)),
                    "rotation": int(plan.get("rotation", 0)),
                    "tasks": tasks,
                }
            )
        event_bus.notify(SchedulesEvent(plans=plans))
        return HandlingResult.success()


class OnAnimProtect(MessageBodyDataDict):
    """``getAnimProtect`` answer / ``onAnimProtect`` push."""

    NAME = "onAnimProtect"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        event_bus.notify(
            AnimProtectEvent(
                enable=bool(int(data.get("enable", 0))),
                start=str(data.get("start", "")),
                end=str(data.get("end", "")),
            )
        )
        return HandlingResult.success()


class OnAutoCutDirection(MessageBodyDataDict):
    """``getAutoCutDirection`` answer / ``onAutoCutDirection`` push."""

    NAME = "onAutoCutDirection"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        event_bus.notify(AutoCutDirectionEvent(enable=bool(int(data.get("enable", 0)))))
        return HandlingResult.success()


# ------------------------------------------------------------------- commands
#
# Get commands inherit the push handler, so the answer and the push parse the
# same way (the pattern upstream uses for GetProtectState).


class _Get(JsonCommandWithMessageHandling, ABC):
    """Base for gets whose answer carries data like the matching push."""


class GetAreaSet(_Get, OnAreaSet):
    """``getAreaSet {mid, aid:"0", type}`` — ``ar`` areas or ``vw`` islands."""

    NAME = "getAreaSet"

    def __init__(self, kind: str, mid: str = "1") -> None:
        super().__init__({"mid": mid, "aid": "0", "type": kind})


class GetAreaParameter(_Get, OnAreaParameter):
    """``getAreaParameter {}``."""

    NAME = "getAreaParameter"


class GetSchedules(_Get, OnSchedules):
    """``getSchedules {}``."""

    NAME = "getSchedules"


class GetAnimProtect(_Get, OnAnimProtect):
    """``getAnimProtect {}``."""

    NAME = "getAnimProtect"


class GetAutoCutDirection(_Get, OnAutoCutDirection):
    """``getAutoCutDirection {}``."""

    NAME = "getAutoCutDirection"


class AppPing(ExecuteCommand):
    """``appping {}`` — the app's "someone is looking" lease.

    The app sends it before ``getMI`` when its map opens. Without a live lease
    the mower acks ``getMI`` but pushes no map (observed 26.09.2026: with the
    app closed, getMI -> ack and nothing; with the lease, onMI/onArI follow).
    """

    NAME = "appping"


class GetSpecialContour(ExecuteCommand):
    """``getSpecialContour {mid, type:0}``; the answer is an ack, data is pushed."""

    NAME = "getSpecialContour"

    def __init__(self, mid: str = "1") -> None:
        super().__init__({"mid": mid, "type": 0})


class DeleteAvoidPoint(ExecuteCommand):
    """Delete one avoid point, exactly as the app does.

    Captured: ``setSpecialContour {"mssid":"3","value":"23494,-18808","mid":"1",
    "aid":"1","type":2,"act":"del"}``; the device acks and pushes a fresh
    ``onSpecialContour`` without the point.
    """

    NAME = "setSpecialContour"

    def __init__(self, mssid: str, x: int, y: int, mid: str = "1", aid: str = "1") -> None:
        super().__init__(
            {"mssid": str(mssid), "value": f"{x},{y}", "mid": mid, "aid": aid, "type": 2, "act": "del"}
        )


class SetAreaParameter(ExecuteCommand):
    """``setAreaParameter`` — the app always sends all five fields."""

    NAME = "setAreaParameter"

    def __init__(
        self, *, area_id: str, mow_height_level: int, cut_mode: int, obstacle_height: int, angle: int
    ) -> None:
        super().__init__(
            {
                "cutMode": int(cut_mode),
                "areaID": str(area_id),
                "obstacleHeight": int(obstacle_height),
                "mowHeightLevel": int(mow_height_level),
                "angle": int(angle),
            }
        )


class SetSchedules(ExecuteCommand):
    """``setSchedules {schedAct:"mod", sid, name, using, rotation, subsets:[task]}``.

    One task per call, with ``taskAct`` ``add``/``mod``/``del`` inside it, as
    the app does. The plan-level fields are echoed back unchanged.
    """

    NAME = "setSchedules"

    def __init__(self, plan: dict[str, Any], task: dict[str, Any], task_act: str) -> None:
        super().__init__(
            {
                "schedAct": "mod",
                "sid": plan["sid"],
                "name": plan.get("name", ""),
                "using": int(plan.get("using", 1)),
                "rotation": int(plan.get("rotation", 0)),
                "subsets": [{**task, "taskAct": task_act}],
            }
        )


class SetAnimProtect(ExecuteCommand):
    """``setAnimProtect {enable, start:"19:0", end:"7:0"}`` (app's H:M format)."""

    NAME = "setAnimProtect"

    def __init__(self, *, enable: bool, start: str, end: str) -> None:
        super().__init__({"enable": 1 if enable else 0, "start": start, "end": end})


class SetAutoCutDirection(ExecuteCommand):
    """``setAutoCutDirection {enable}``."""

    NAME = "setAutoCutDirection"

    def __init__(self, enable: bool) -> None:
        super().__init__({"enable": 1 if enable else 0})


# -------------------------------------------------------------------- wiring


def register_messages() -> None:
    """Register the A1600 message handlers (called from ``apply()``)."""
    for message in (
        OnArIA1600,
        OnSpecialContourA1600,
        OnAreaSet,
        OnAreaParameter,
        OnSchedules,
        OnAnimProtect,
        OnAutoCutDirection,
    ):
        MESSAGES[message.NAME] = message


def refresh_events() -> dict[type[Event], list[Any]]:
    """Refresh commands for the A1600 events (merged in ``patch_device_info``).

    ``MowerMapInfoEvent`` gets ``getMI`` alone: ``getMapInfo_V2`` never answers
    on this firmware. ``getSpecialContour`` rides along so avoid points arrive
    with the map.
    """
    return {
        # appping is sent first, sequentially, by the runtime (see
        # A1600Runtime.refresh_map); refresh commands here run concurrently.
        MowerMapInfoEvent: [GetMI(), GetSpecialContour()],
        AreaSetEvent: [GetAreaSet("ar"), GetAreaSet("vw")],
        AreaParametersEvent: [GetAreaParameter()],
        SchedulesEvent: [GetSchedules()],
        AnimProtectEvent: [GetAnimProtect()],
        AutoCutDirectionEvent: [GetAutoCutDirection()],
    }
