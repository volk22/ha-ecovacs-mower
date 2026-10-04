"""Time platform — only the A1600 fork's entities (see a1600_entities.py)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry, a1600_entities


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the A1600 time entities."""
    if entities := a1600_entities.times(config_entry.runtime_data):
        async_add_entities(entities)
