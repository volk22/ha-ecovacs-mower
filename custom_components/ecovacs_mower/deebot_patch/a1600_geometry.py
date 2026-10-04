"""Layered map decoding for GOAT A1600 LiDAR Pro (``e4gqia``, fw 1.13.x).

Pure Python, no deebot-client or Home Assistant imports, like ``geometry.py``.

The A1600 speaks the same chain-code ``onArI`` dialect as the O1200 on fw 1.11,
but its sections mean different things. Established from a capture of an
A1600 on fw 1.13.10 (26.09.2026), cross-checked against the app's schedule
payloads (``reid:``/``vid:`` ids) and ``getAreaSet``:

=========  ==================================================================
Section    A1600 fw 1.13.10
=========  ==================================================================
1          lawn area ids, no geometry (the area is the whole boundary)
2          islands inside the lawn — flower beds, trees; ``vid:<id>`` in the
           app's schedules, ``getAreaSet type "vw"`` lists the same ids
3          detected obstacles, ids from 100
4          empty
5          **open** polylines the mower trims along — ``reid:<id>``; they do
           not close, so drawing them as polygons produces a long chord
6          the closed lawn boundary, identical to ``onMI``
=========  ==================================================================

``onSpecialContour`` records are ``[mid, mssid, type, "<a>,<b>", "x,y;…"]``:
type ``"2"`` is a single point the mower avoids (the app's dashed circles,
deleted with ``setSpecialContour act "del"`` by ``mssid``), type ``"1"`` a
polygon no-go zone.

Unlike ``geometry.py`` this keeps every item's id, because the ids are what
the write commands address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Any

from .geometry import Point, Polygon, chain_to_points

# A chain-coded outline closes to within one or two grid steps; anything
# further apart than this is an open line.
_CLOSE_TOLERANCE_MM = 150


@dataclass(frozen=True)
class Item:
    """One id-carrying shape."""

    id: str
    points: Polygon

    def as_dict(self) -> dict[str, Any]:
        """Serialize."""
        return {"id": self.id, "points": [list(p) for p in self.points]}


@dataclass(frozen=True)
class AvoidPoint:
    """A point the mower routes around; deletable by ``mssid``."""

    mssid: str
    x: int
    y: int
    raw_pair: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize."""
        return {"mssid": self.mssid, "x": self.x, "y": self.y}


@dataclass(frozen=True)
class A1600Layers:
    """Decoded ``onArI``. ``None`` means "section not updated"."""

    area_ids: list[str] | None = None
    islands: list[Item] | None = None
    obstacles: list[Item] | None = None
    edges: list[Item] | None = None
    boundary: Polygon | None = None


@dataclass(frozen=True)
class SpecialContour:
    """Decoded ``onSpecialContour``."""

    avoid_points: list[AvoidPoint] = field(default_factory=list)
    nogo: list[Item] = field(default_factory=list)


def is_closed(points: Polygon) -> bool:
    """True when the first and last point meet (within a grid step or two)."""
    if len(points) < 3:
        return False
    return math.dist(points[0], points[-1]) <= _CLOSE_TOLERANCE_MM


def _items(raw: list[str]) -> list[Item]:
    result: list[Item] = []
    for entry in raw:
        item_id, _, spec = entry.partition(";")
        if spec:
            result.append(Item(item_id, chain_to_points(spec)))
    return result


def looks_like_a1600(entries: list[list[str]]) -> bool:
    """Recognise the A1600 section layout from the blob itself.

    Section 6 carrying one closed outline while section 5 carries at least one
    open line is what the A1600 sends and what the O1200 capture does not
    (there section 5 is the closed boundary). Deciding from the data keeps the
    O1200 path untouched without keying on the class string.
    """
    sections = {e[1]: e[3:] for e in entries if len(e) > 2 and e[2] == "1"}
    six = _items(sections.get("6", []))
    five = _items(sections.get("5", []))
    return (
        len(six) == 1
        and is_closed(six[0].points)
        and any(not is_closed(item.points) for item in five)
    )


def parse_layers(blob: bytes) -> A1600Layers:
    """Parse an A1600 ``onArI`` blob into id-carrying layers."""
    sections: dict[str, list[str]] = {}
    for entry in json.loads(blob):
        if len(entry) > 2 and entry[2] == "1":
            sections[entry[1]] = entry[3:]

    boundary: Polygon | None = None
    if "6" in sections:
        outlines = [i.points for i in _items(sections["6"]) if is_closed(i.points)]
        boundary = max(outlines, key=len) if outlines else None

    return A1600Layers(
        area_ids=[e.partition(";")[0] for e in sections["1"]] if "1" in sections else None,
        islands=_items(sections["2"]) if "2" in sections else None,
        obstacles=_items(sections["3"]) if "3" in sections else None,
        edges=_items(sections["5"]) if "5" in sections else None,
        boundary=boundary,
    )


def parse_special_contour(blob: bytes) -> SpecialContour:
    """Split ``onSpecialContour`` into avoid points and polygon no-go zones."""
    points: list[AvoidPoint] = []
    nogo: list[Item] = []
    for entry in json.loads(blob):
        if len(entry) < 5:
            continue
        mssid, kind, pair, spec = entry[1], entry[2], entry[3], entry[4]
        coords: list[Point] = []
        for part in spec.split(";"):
            if "," in part:
                x, y = part.split(",")[:2]
                coords.append((int(float(x)), int(float(y))))
        if kind == "2" and coords:
            points.append(AvoidPoint(mssid, coords[0][0], coords[0][1], pair))
        elif coords:
            nogo.append(Item(mssid, coords))
    return SpecialContour(avoid_points=points, nogo=nogo)


def decode_subsets(value: str) -> Any:
    """Decode an LZMA ``subsets`` field (getAreaSet, getSchedules)."""
    from .geometry import decompress

    return json.loads(decompress(value))
