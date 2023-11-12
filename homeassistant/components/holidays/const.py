"""Add constants for Holidays integration."""
from __future__ import annotations

import logging

from homeassistant.const import Platform

LOGGER = logging.getLogger(__package__)

DOMAIN = "holidays"
PLATFORMS = [Platform.CALENDAR]

CONF_COUNTRY = "country"
CONF_PROVINCE = "province"
