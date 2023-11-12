"""Calendar for holidays."""
from __future__ import annotations

from datetime import datetime

from holidays import (
    HolidayBase,
    __version__ as python_holidays_version,
    country_holidays,
)

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LANGUAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_COUNTRY, CONF_PROVINCE, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Holidays calendar entity."""
    country: str = entry.options[CONF_COUNTRY]
    province: str | None = entry.options.get(CONF_PROVINCE)
    language: str | None = entry.options.get(CONF_LANGUAGE)
    sensor_name: str = entry.title

    year: int = dt_util.now().year

    obj_holidays: HolidayBase = country_holidays(
        country,
        subdiv=province,
        years=year,
        language=language,
    )

    async_add_entities(
        [
            IsWorkdaySensor(
                obj_holidays,
                sensor_name,
                entry.entry_id,
            )
        ],
        True,
    )


class IsWorkdaySensor(CalendarEntity):
    """Implementation of a Holidays sensor."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = DOMAIN

    def __init__(
        self,
        obj_holidays: HolidayBase,
        name: str,
        entry_id: str,
    ) -> None:
        """Initialize the Workday sensor."""
        self._obj_holidays = obj_holidays
        self._attr_unique_id = entry_id
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, entry_id)},
            manufacturer="python-holidays",
            model=python_holidays_version,
            name=name,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        results = [d for d in sorted(self._obj_holidays) if d >= dt_util.now()]
        if not results:
            return None

        return CalendarEvent(
            start=results[0],
            end=results[0],
            summary=self._obj_holidays.get(results[0]),
            location=self._obj_holidays.country,
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        results = [d for d in sorted(self._obj_holidays) if start_date <= d <= end_date]
        if not results:
            return []
        return [
            CalendarEvent(
                start=result,
                end=result,
                summary=self._obj_holidays.get(result),
                location=self._obj_holidays.country,
            )
            for result in results
        ]
