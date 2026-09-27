"""Mowing history from the ECOVACS cloud (``app/dln/api/log/clean_result``).

Found by sniffing the official app (30.05.2026): the history screen lists
sessions with ``clean_result/list`` and opens one with ``clean_result/data``,
which carries the whole map of that session (boundary, obstacles, areas, the
mowed track) in the same LZMA containers as the MQTT map messages. Neither is
used by any public project; mrbungle64/ecovacs-deebot.js and openHAB use only
``list`` for vacuums.

Lives in ``deebot_patch`` because it needs the authenticator's config (portal
URL, client device id), which deebot-client keeps private.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from .a1600_geometry import decode_subsets

if TYPE_CHECKING:
    from deebot_client.authentication import Authenticator

_LOGGER = logging.getLogger(__name__)
REALM = "ecouser.net"


class HistoryError(Exception):
    """The history endpoint did not answer as expected."""


def _app_url(authenticator: Authenticator) -> str:
    """``https://api-app.dc-<continent>.ww.ecouser.net`` from the portal URL."""
    portal = authenticator._auth_client._config.portal_url  # noqa: SLF001
    # portal-eu.ecouser.net -> eu ; portal-ww.ecouser.net -> ww
    host = portal.split("//", 1)[-1]
    continent = host.split(".", 1)[0].removeprefix("portal").lstrip("-") or "ww"
    return f"https://api-app.dc-{continent}.ww.ecouser.net"


async def _get(authenticator: Authenticator, path: str, params: dict[str, Any]) -> Any:
    config = authenticator._auth_client._config  # noqa: SLF001
    credentials = await authenticator.authenticate()
    headers = {"userid": credentials.user_id, "token": credentials.token}
    url = f"{_app_url(authenticator)}/app/dln/api/log/clean_result/{path}"
    async with config.session.get(url, params=params, headers=headers, timeout=20) as resp:
        text = await resp.text()
    try:
        body = json.loads(text)
    except ValueError as err:
        raise HistoryError(f"{path}: not JSON ({text[:120]})") from err
    if str(body.get("code")) not in ("0", "0000") and body.get("ret") != "ok":
        raise HistoryError(f"{path}: {body.get('code')} {body.get('message') or body.get('msg')}")
    return body.get("data")


async def list_sessions(
    authenticator: Authenticator, device_info: dict[str, Any], size: int = 20
) -> list[dict[str, Any]]:
    """Return the latest mowing sessions, newest first."""
    config = authenticator._auth_client._config  # noqa: SLF001
    credentials = await authenticator.authenticate()
    auth = {
        "with": "users",
        "userid": credentials.user_id,
        "realm": REALM,
        "token": credentials.token,
        "resource": config.device_id,
    }
    params = {
        "did": device_info["did"],
        "res": device_info.get("resource", ""),
        "country": config.country.lower(),
        "size": size,
        "auth": json.dumps(auth, separators=(",", ":")),
        "version": "v2",
        "logType": "clean",
        "before": int(time.time() * 1000),
        "lang": "EN",
        "channel": "google_play",
    }
    data = await _get(authenticator, "list", params)
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or []
    return data if isinstance(data, list) else []


def _decode_field(value: Any) -> Any:
    """Decode one ``{infoSize, value|traceValue|trackValue}`` geometry field."""
    if not isinstance(value, dict):
        return value
    for key in ("value", "trackValue", "traceValue"):
        if isinstance(value.get(key), str) and value[key]:
            try:
                return decode_subsets(value[key])
            except (ValueError, TypeError):
                return None
    return None


async def session_detail(authenticator: Authenticator, session_id: str) -> dict[str, Any]:
    """Return one session with its geometry fields decoded."""
    data = await _get(authenticator, "data", {"id": session_id, "channel": "google_play"})
    if not isinstance(data, dict):
        raise HistoryError("data: empty")
    return {key: _decode_field(value) for key, value in data.items()}


def track_segments(map_track: Any) -> list[list[list[int]]]:
    """Mowed spans from a session's ``mapTrack`` records ``<zone>;1;<row>;x,y;x,y…``."""
    segments: list[list[list[int]]] = []
    if not isinstance(map_track, list):
        return segments
    for record in map_track:
        if not isinstance(record, str):
            continue
        fields = record.split(";")
        if len(fields) < 5 or fields[1] != "1":
            continue
        points = []
        for part in fields[3:]:
            if "," in part:
                x, y = part.split(",")[:2]
                points.append([int(x), int(y)])
        segments.extend([a, b] for a, b in zip(points[::2], points[1::2]))
    return segments
