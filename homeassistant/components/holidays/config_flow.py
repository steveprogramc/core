"""Adds config flow for Holidays integration."""
from __future__ import annotations

from typing import Any

from holidays import country_holidays, list_supported_countries
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_LANGUAGE
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.selector import (
    CountrySelector,
    CountrySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_COUNTRY, CONF_PROVINCE, DOMAIN, LOGGER


def add_province_and_language_to_schema(
    schema: vol.Schema,
    country: str,
) -> vol.Schema:
    """Update schema with province and language from country."""

    all_countries = list_supported_countries(include_aliases=False)

    language_schema = {}
    province_schema = {}

    _country = country_holidays(country=country)
    if country_default_language := (_country.default_language):
        selectable_languages = _country.supported_languages
        language_schema = {
            vol.Optional(
                CONF_LANGUAGE, default=country_default_language
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(selectable_languages),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }

    if provinces := all_countries.get(country):
        province_schema = {
            vol.Optional(CONF_PROVINCE): SelectSelector(
                SelectSelectorConfig(
                    options=provinces,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_PROVINCE,
                )
            ),
        }

    return vol.Schema({**DATA_SCHEMA_OPT.schema, **language_schema, **province_schema})


DATA_SCHEMA_SETUP = vol.Schema(
    {
        vol.Required(CONF_COUNTRY): CountrySelector(
            CountrySelectorConfig(
                countries=list(list_supported_countries(include_aliases=False)),
            )
        ),
    }
)

DATA_SCHEMA_OPT = vol.Schema({})


class WorkdayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Workday integration."""

    VERSION = 1

    data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> WorkdayOptionsFlowHandler:
        """Get the options flow for this handler."""
        return WorkdayOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data = user_input
            return await self.async_step_options()
        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA_SETUP,
            errors=errors,
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle remaining flow."""
        errors: dict[str, str] = {}
        if user_input is not None:
            combined_input: dict[str, Any] = {**self.data, **user_input}

            abort_match = {
                CONF_COUNTRY: combined_input.get(CONF_COUNTRY),
                CONF_PROVINCE: combined_input.get(CONF_PROVINCE),
            }
            LOGGER.debug("abort_check in options with %s", combined_input)
            self._async_abort_entries_match(abort_match)

            title = combined_input[CONF_COUNTRY]
            if province := combined_input.get(CONF_PROVINCE):
                title = f"{combined_input[CONF_COUNTRY]} - {province}"

            return self.async_create_entry(
                title=title,
                data={},
                options=combined_input,
            )

        schema = await self.hass.async_add_executor_job(
            add_province_and_language_to_schema,
            DATA_SCHEMA_OPT,
            self.data[CONF_COUNTRY],
        )
        new_schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="options",
            data_schema=new_schema,
            errors=errors,
            description_placeholders={
                "country": self.data[CONF_COUNTRY],
            },
        )


class WorkdayOptionsFlowHandler(OptionsFlowWithConfigEntry):
    """Handle Holidays options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Holidays options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            combined_input: dict[str, Any] = {**self.options, **user_input}
            if CONF_PROVINCE not in user_input:
                # Province not present, delete old value (if present) too
                combined_input.pop(CONF_PROVINCE, None)

            LOGGER.debug("abort_check in options with %s", combined_input)
            try:
                self._async_abort_entries_match(
                    {
                        CONF_COUNTRY: self._config_entry.options[CONF_COUNTRY],
                        CONF_PROVINCE: combined_input.get(CONF_PROVINCE),
                    }
                )
            except AbortFlow as err:
                errors = {"base": err.reason}
            else:
                return self.async_create_entry(data=combined_input)

        schema: vol.Schema = await self.hass.async_add_executor_job(
            add_province_and_language_to_schema,
            DATA_SCHEMA_OPT,
            self.options[CONF_COUNTRY],
        )

        new_schema = self.add_suggested_values_to_schema(
            schema, user_input or self.options
        )
        LOGGER.debug("Errors have occurred in options %s", errors)
        return self.async_show_form(
            step_id="init",
            data_schema=new_schema,
            errors=errors,
            description_placeholders={
                "country": self.options[CONF_COUNTRY],
            },
        )
