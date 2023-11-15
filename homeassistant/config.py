"""Module to help with parsing and generating configuration files."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import reduce
import logging
import operator
import os
from pathlib import Path
import re
import shutil
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, overload
from urllib.parse import urlparse

from awesomeversion import AwesomeVersion
import voluptuous as vol
from voluptuous.humanize import MAX_VALIDATION_ERROR_ITEM_LENGTH

from . import auth
from .auth import mfa_modules as auth_mfa_modules, providers as auth_providers
from .const import (
    ATTR_ASSUMED_STATE,
    ATTR_FRIENDLY_NAME,
    ATTR_HIDDEN,
    CONF_ALLOWLIST_EXTERNAL_DIRS,
    CONF_ALLOWLIST_EXTERNAL_URLS,
    CONF_AUTH_MFA_MODULES,
    CONF_AUTH_PROVIDERS,
    CONF_COUNTRY,
    CONF_CURRENCY,
    CONF_CUSTOMIZE,
    CONF_CUSTOMIZE_DOMAIN,
    CONF_CUSTOMIZE_GLOB,
    CONF_ELEVATION,
    CONF_EXTERNAL_URL,
    CONF_ID,
    CONF_INTERNAL_URL,
    CONF_LANGUAGE,
    CONF_LATITUDE,
    CONF_LEGACY_TEMPLATES,
    CONF_LONGITUDE,
    CONF_MEDIA_DIRS,
    CONF_NAME,
    CONF_PACKAGES,
    CONF_TEMPERATURE_UNIT,
    CONF_TIME_ZONE,
    CONF_TYPE,
    CONF_UNIT_SYSTEM,
    LEGACY_CONF_WHITELIST_EXTERNAL_DIRS,
    __version__,
)
from .core import DOMAIN as CONF_CORE, ConfigSource, HomeAssistant, callback
from .exceptions import ConfigValidationError, HomeAssistantError
from .generated.currencies import HISTORIC_CURRENCIES
from .helpers import (
    config_per_platform,
    config_validation as cv,
    extract_domain_configs,
    issue_registry as ir,
)
from .helpers.entity_values import EntityValues
from .helpers.typing import ConfigType
from .loader import ComponentProtocol, Integration, IntegrationNotFound
from .requirements import RequirementsNotFound, async_get_integration_with_requirements
from .util.package import is_docker_env
from .util.unit_system import get_unit_system, validate_unit_system
from .util.yaml import SECRET_YAML, Secrets, load_yaml

_LOGGER = logging.getLogger(__name__)

DATA_PERSISTENT_ERRORS = "bootstrap_persistent_errors"
RE_YAML_ERROR = re.compile(r"homeassistant\.util\.yaml")
RE_ASCII = re.compile(r"\033\[[^m]*m")
YAML_CONFIG_FILE = "configuration.yaml"
VERSION_FILE = ".HA_VERSION"
CONFIG_DIR_NAME = ".homeassistant"
DATA_CUSTOMIZE = "hass_customize"

AUTOMATION_CONFIG_PATH = "automations.yaml"
SCRIPT_CONFIG_PATH = "scripts.yaml"
SCENE_CONFIG_PATH = "scenes.yaml"

LOAD_EXCEPTIONS = (ImportError, FileNotFoundError)
INTEGRATION_LOAD_EXCEPTIONS = (
    IntegrationNotFound,
    RequirementsNotFound,
    *LOAD_EXCEPTIONS,
)

SAFE_MODE_FILENAME = "safe-mode"

DEFAULT_CONFIG = f"""
# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include {AUTOMATION_CONFIG_PATH}
script: !include {SCRIPT_CONFIG_PATH}
scene: !include {SCENE_CONFIG_PATH}
"""
DEFAULT_SECRETS = """
# Use this file to store secrets like usernames and passwords.
# Learn more at https://www.home-assistant.io/docs/configuration/secrets/
some_password: welcome
"""
TTS_PRE_92 = """
tts:
  - platform: google
"""
TTS_92 = """
tts:
  - platform: google_translate
    service_name: google_say
"""


@dataclass
class ConfigExceptionInfo:
    """Configuration exception info class."""

    ex: Exception
    translation_key: str
    log_message: str | None = None
    p_name: str | None = None
    p_config: ConfigType | None = None
    p_integration_link: str | None = None
    log_exception: bool = False


def _no_duplicate_auth_provider(
    configs: Sequence[dict[str, Any]]
) -> Sequence[dict[str, Any]]:
    """No duplicate auth provider config allowed in a list.

    Each type of auth provider can only have one config without optional id.
    Unique id is required if same type of auth provider used multiple times.
    """
    config_keys: set[tuple[str, str | None]] = set()
    for config in configs:
        key = (config[CONF_TYPE], config.get(CONF_ID))
        if key in config_keys:
            raise vol.Invalid(
                f"Duplicate auth provider {config[CONF_TYPE]} found. "
                "Please add unique IDs "
                "if you want to have the same auth provider twice"
            )
        config_keys.add(key)
    return configs


def _no_duplicate_auth_mfa_module(
    configs: Sequence[dict[str, Any]]
) -> Sequence[dict[str, Any]]:
    """No duplicate auth mfa module item allowed in a list.

    Each type of mfa module can only have one config without optional id.
    A global unique id is required if same type of mfa module used multiple
    times.
    Note: this is different than auth provider
    """
    config_keys: set[str] = set()
    for config in configs:
        key = config.get(CONF_ID, config[CONF_TYPE])
        if key in config_keys:
            raise vol.Invalid(
                f"Duplicate mfa module {config[CONF_TYPE]} found. "
                "Please add unique IDs "
                "if you want to have the same mfa module twice"
            )
        config_keys.add(key)
    return configs


def _filter_bad_internal_external_urls(conf: dict) -> dict:
    """Filter internal/external URL with a path."""
    for key in CONF_INTERNAL_URL, CONF_EXTERNAL_URL:
        if key in conf and urlparse(conf[key]).path not in ("", "/"):
            # We warn but do not fix, because if this was incorrectly configured,
            # adjusting this value might impact security.
            _LOGGER.warning(
                "Invalid %s set. It's not allowed to have a path (/bla)", key
            )

    return conf


PACKAGES_CONFIG_SCHEMA = cv.schema_with_slug_keys(  # Package names are slugs
    vol.Schema({cv.string: vol.Any(dict, list, None)})  # Component config
)

CUSTOMIZE_DICT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FRIENDLY_NAME): cv.string,
        vol.Optional(ATTR_HIDDEN): cv.boolean,
        vol.Optional(ATTR_ASSUMED_STATE): cv.boolean,
    },
    extra=vol.ALLOW_EXTRA,
)

CUSTOMIZE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CUSTOMIZE, default={}): vol.Schema(
            {cv.entity_id: CUSTOMIZE_DICT_SCHEMA}
        ),
        vol.Optional(CONF_CUSTOMIZE_DOMAIN, default={}): vol.Schema(
            {cv.string: CUSTOMIZE_DICT_SCHEMA}
        ),
        vol.Optional(CONF_CUSTOMIZE_GLOB, default={}): vol.Schema(
            {cv.string: CUSTOMIZE_DICT_SCHEMA}
        ),
    }
)


def _raise_issue_if_historic_currency(hass: HomeAssistant, currency: str) -> None:
    if currency not in HISTORIC_CURRENCIES:
        ir.async_delete_issue(hass, "homeassistant", "historic_currency")
        return

    ir.async_create_issue(
        hass,
        "homeassistant",
        "historic_currency",
        is_fixable=False,
        learn_more_url="homeassistant://config/general",
        severity=ir.IssueSeverity.WARNING,
        translation_key="historic_currency",
        translation_placeholders={"currency": currency},
    )


def _raise_issue_if_no_country(hass: HomeAssistant, country: str | None) -> None:
    if country is not None:
        ir.async_delete_issue(hass, "homeassistant", "country_not_configured")
        return

    ir.async_create_issue(
        hass,
        "homeassistant",
        "country_not_configured",
        is_fixable=False,
        learn_more_url="homeassistant://config/general",
        severity=ir.IssueSeverity.WARNING,
        translation_key="country_not_configured",
    )


def _validate_currency(data: Any) -> Any:
    try:
        return cv.currency(data)
    except vol.InInvalid:
        with suppress(vol.InInvalid):
            currency = cv.historic_currency(data)
            return currency
        raise


CORE_CONFIG_SCHEMA = vol.All(
    CUSTOMIZE_CONFIG_SCHEMA.extend(
        {
            CONF_NAME: vol.Coerce(str),
            CONF_LATITUDE: cv.latitude,
            CONF_LONGITUDE: cv.longitude,
            CONF_ELEVATION: vol.Coerce(int),
            vol.Remove(CONF_TEMPERATURE_UNIT): cv.temperature_unit,
            CONF_UNIT_SYSTEM: validate_unit_system,
            CONF_TIME_ZONE: cv.time_zone,
            vol.Optional(CONF_INTERNAL_URL): cv.url,
            vol.Optional(CONF_EXTERNAL_URL): cv.url,
            vol.Optional(CONF_ALLOWLIST_EXTERNAL_DIRS): vol.All(
                cv.ensure_list, [vol.IsDir()]
            ),
            vol.Optional(LEGACY_CONF_WHITELIST_EXTERNAL_DIRS): vol.All(
                cv.ensure_list, [vol.IsDir()]
            ),
            vol.Optional(CONF_ALLOWLIST_EXTERNAL_URLS): vol.All(
                cv.ensure_list, [cv.url]
            ),
            vol.Optional(CONF_PACKAGES, default={}): PACKAGES_CONFIG_SCHEMA,
            vol.Optional(CONF_AUTH_PROVIDERS): vol.All(
                cv.ensure_list,
                [
                    auth_providers.AUTH_PROVIDER_SCHEMA.extend(
                        {
                            CONF_TYPE: vol.NotIn(
                                ["insecure_example"],
                                (
                                    "The insecure_example auth provider"
                                    " is for testing only."
                                ),
                            )
                        }
                    )
                ],
                _no_duplicate_auth_provider,
            ),
            vol.Optional(CONF_AUTH_MFA_MODULES): vol.All(
                cv.ensure_list,
                [
                    auth_mfa_modules.MULTI_FACTOR_AUTH_MODULE_SCHEMA.extend(
                        {
                            CONF_TYPE: vol.NotIn(
                                ["insecure_example"],
                                "The insecure_example mfa module is for testing only.",
                            )
                        }
                    )
                ],
                _no_duplicate_auth_mfa_module,
            ),
            vol.Optional(CONF_MEDIA_DIRS): cv.schema_with_slug_keys(vol.IsDir()),
            vol.Optional(CONF_LEGACY_TEMPLATES): cv.boolean,
            vol.Optional(CONF_CURRENCY): _validate_currency,
            vol.Optional(CONF_COUNTRY): cv.country,
            vol.Optional(CONF_LANGUAGE): cv.language,
        }
    ),
    _filter_bad_internal_external_urls,
)


def get_default_config_dir() -> str:
    """Put together the default configuration directory based on the OS."""
    data_dir = os.path.expanduser("~")
    return os.path.join(data_dir, CONFIG_DIR_NAME)


async def async_ensure_config_exists(hass: HomeAssistant) -> bool:
    """Ensure a configuration file exists in given configuration directory.

    Creating a default one if needed.
    Return boolean if configuration dir is ready to go.
    """
    config_path = hass.config.path(YAML_CONFIG_FILE)

    if os.path.isfile(config_path):
        return True

    print(  # noqa: T201
        "Unable to find configuration. Creating default one in", hass.config.config_dir
    )
    return await async_create_default_config(hass)


async def async_create_default_config(hass: HomeAssistant) -> bool:
    """Create a default configuration file in given configuration directory.

    Return if creation was successful.
    """
    return await hass.async_add_executor_job(
        _write_default_config, hass.config.config_dir
    )


def _write_default_config(config_dir: str) -> bool:
    """Write the default config."""
    config_path = os.path.join(config_dir, YAML_CONFIG_FILE)
    secret_path = os.path.join(config_dir, SECRET_YAML)
    version_path = os.path.join(config_dir, VERSION_FILE)
    automation_yaml_path = os.path.join(config_dir, AUTOMATION_CONFIG_PATH)
    script_yaml_path = os.path.join(config_dir, SCRIPT_CONFIG_PATH)
    scene_yaml_path = os.path.join(config_dir, SCENE_CONFIG_PATH)

    # Writing files with YAML does not create the most human readable results
    # So we're hard coding a YAML template.
    try:
        with open(config_path, "w", encoding="utf8") as config_file:
            config_file.write(DEFAULT_CONFIG)

        if not os.path.isfile(secret_path):
            with open(secret_path, "w", encoding="utf8") as secret_file:
                secret_file.write(DEFAULT_SECRETS)

        with open(version_path, "w", encoding="utf8") as version_file:
            version_file.write(__version__)

        if not os.path.isfile(automation_yaml_path):
            with open(automation_yaml_path, "w", encoding="utf8") as automation_file:
                automation_file.write("[]")

        if not os.path.isfile(script_yaml_path):
            with open(script_yaml_path, "w", encoding="utf8"):
                pass

        if not os.path.isfile(scene_yaml_path):
            with open(scene_yaml_path, "w", encoding="utf8"):
                pass

        return True

    except OSError:
        print(  # noqa: T201
            f"Unable to create default configuration file {config_path}"
        )
        return False


async def async_hass_config_yaml(hass: HomeAssistant) -> dict:
    """Load YAML from a Home Assistant configuration file.

    This function allows a component inside the asyncio loop to reload its
    configuration by itself. Include package merge.
    """
    secrets = Secrets(Path(hass.config.config_dir))

    # Not using async_add_executor_job because this is an internal method.
    config = await hass.loop.run_in_executor(
        None,
        load_yaml_config_file,
        hass.config.path(YAML_CONFIG_FILE),
        secrets,
    )
    core_config = config.get(CONF_CORE, {})
    await merge_packages_config(hass, config, core_config.get(CONF_PACKAGES, {}))
    return config


def load_yaml_config_file(
    config_path: str, secrets: Secrets | None = None
) -> dict[Any, Any]:
    """Parse a YAML configuration file.

    Raises FileNotFoundError or HomeAssistantError.

    This method needs to run in an executor.
    """
    conf_dict = load_yaml(config_path, secrets)

    if not isinstance(conf_dict, dict):
        msg = (
            f"The configuration file {os.path.basename(config_path)} "
            "does not contain a dictionary"
        )
        _LOGGER.error(msg)
        raise HomeAssistantError(msg)

    # Convert values to dictionaries if they are None
    for key, value in conf_dict.items():
        conf_dict[key] = value or {}
    return conf_dict


def process_ha_config_upgrade(hass: HomeAssistant) -> None:
    """Upgrade configuration if necessary.

    This method needs to run in an executor.
    """
    version_path = hass.config.path(VERSION_FILE)

    try:
        with open(version_path, encoding="utf8") as inp:
            conf_version = inp.readline().strip()
    except FileNotFoundError:
        # Last version to not have this file
        conf_version = "0.7.7"

    if conf_version == __version__:
        return

    _LOGGER.info(
        "Upgrading configuration directory from %s to %s", conf_version, __version__
    )

    version_obj = AwesomeVersion(conf_version)

    if version_obj < AwesomeVersion("0.50"):
        # 0.50 introduced persistent deps dir.
        lib_path = hass.config.path("deps")
        if os.path.isdir(lib_path):
            shutil.rmtree(lib_path)

    if version_obj < AwesomeVersion("0.92"):
        # 0.92 moved google/tts.py to google_translate/tts.py
        config_path = hass.config.path(YAML_CONFIG_FILE)

        with open(config_path, encoding="utf-8") as config_file:
            config_raw = config_file.read()

        if TTS_PRE_92 in config_raw:
            _LOGGER.info("Migrating google tts to google_translate tts")
            config_raw = config_raw.replace(TTS_PRE_92, TTS_92)
            try:
                with open(config_path, "w", encoding="utf-8") as config_file:
                    config_file.write(config_raw)
            except OSError:
                _LOGGER.exception("Migrating to google_translate tts failed")

    if version_obj < AwesomeVersion("0.94") and is_docker_env():
        # In 0.94 we no longer install packages inside the deps folder when
        # running inside a Docker container.
        lib_path = hass.config.path("deps")
        if os.path.isdir(lib_path):
            shutil.rmtree(lib_path)

    with open(version_path, "w", encoding="utf8") as outp:
        outp.write(__version__)


@callback
def async_log_schema_error(
    ex: vol.Invalid,
    domain: str,
    config: dict,
    hass: HomeAssistant,
    link: str | None = None,
) -> None:
    """Log a schema validation error."""
    if hass is not None:
        async_notify_setup_error(hass, domain, link)
    message = format_schema_error(ex, domain, config, link)
    _LOGGER.error(message)


@callback
def async_log_config_validator_error(
    ex: vol.Invalid | HomeAssistantError,
    domain: str,
    config: dict,
    hass: HomeAssistant,
    link: str | None = None,
) -> None:
    """Log an error from a custom config validator."""
    if isinstance(ex, vol.Invalid):
        async_log_schema_error(ex, domain, config, hass, link)
        return

    if hass is not None:
        async_notify_setup_error(hass, domain, link)
    message = format_homeassistant_error(ex, domain, config, link)
    _LOGGER.error(message, exc_info=ex)


def _get_annotation(item: Any) -> tuple[str, int | str] | None:
    if not hasattr(item, "__config_file__"):
        return None

    return (getattr(item, "__config_file__"), getattr(item, "__line__", "?"))


def _get_by_path(data: dict | list, items: list[str | int]) -> Any:
    """Access a nested object in root by item sequence.

    Returns None in case of error.
    """
    try:
        return reduce(operator.getitem, items, data)  # type: ignore[arg-type]
    except (KeyError, IndexError, TypeError):
        return None


def find_annotation(
    config: dict | list, path: list[str | int]
) -> tuple[str, int | str] | None:
    """Find file/line annotation for a node in config pointed to by path.

    If the node pointed to is a dict or list, prefer the annotation for the key in
    the key/value pair defining the dict or list.
    If the node is not annotated, try the parent node.
    """

    def find_annotation_for_key(
        item: dict, path: list[str | int], tail: str | int
    ) -> tuple[str, int | str] | None:
        for key in item:
            if key == tail:
                if annotation := _get_annotation(key):
                    return annotation
                break
        return None

    def find_annotation_rec(
        config: dict | list, path: list[str | int], tail: str | int | None
    ) -> tuple[str, int | str] | None:
        item = _get_by_path(config, path)
        if isinstance(item, dict) and tail is not None:
            if tail_annotation := find_annotation_for_key(item, path, tail):
                return tail_annotation

        if (
            isinstance(item, (dict, list))
            and path
            and (
                key_annotation := find_annotation_for_key(
                    _get_by_path(config, path[:-1]), path[:-1], path[-1]
                )
            )
        ):
            return key_annotation

        if annotation := _get_annotation(item):
            return annotation

        if not path:
            return None

        tail = path.pop()
        if annotation := find_annotation_rec(config, path, tail):
            return annotation
        return _get_annotation(item)

    return find_annotation_rec(config, list(path), None)


def stringify_invalid(
    ex: vol.Invalid,
    domain: str,
    config: dict,
    link: str | None,
    max_sub_error_length: int,
) -> str:
    """Stringify voluptuous.Invalid.

    This is an alternative to the custom __str__ implemented in
    voluptuous.error.Invalid. The modifications are:
    - Format the path delimited by -> instead of @data[]
    - Prefix with domain, file and line of the error
    - Suffix with a link to the documentation
    - Give a more user friendly output for unknown options
    - Give a more user friendly output for missing options
    """
    message_prefix = f"Invalid config for [{domain}]"
    if domain != CONF_CORE and link:
        message_suffix = f". Please check the docs at {link}"
    else:
        message_suffix = ""
    if annotation := find_annotation(config, ex.path):
        message_prefix += f" at {annotation[0]}, line {annotation[1]}"
    path = "->".join(str(m) for m in ex.path)
    if ex.error_message == "extra keys not allowed":
        return (
            f"{message_prefix}: '{ex.path[-1]}' is an invalid option for [{domain}], "
            f"check: {path}{message_suffix}"
        )
    if ex.error_message == "required key not provided":
        return (
            f"{message_prefix}: required key '{ex.path[-1]}' not provided"
            f"{message_suffix}."
        )
    # This function is an alternative to the stringification done by
    # vol.Invalid.__str__, so we need to call Exception.__str__ here
    # instead of str(ex)
    output = Exception.__str__(ex)
    if error_type := ex.error_type:
        output += " for " + error_type
    offending_item_summary = repr(_get_by_path(config, ex.path))
    if len(offending_item_summary) > max_sub_error_length:
        offending_item_summary = (
            f"{offending_item_summary[: max_sub_error_length - 3]}..."
        )
    return (
        f"{message_prefix}: {output} '{path}', got {offending_item_summary}"
        f"{message_suffix}."
    )


def humanize_error(
    validation_error: vol.Invalid,
    domain: str,
    config: dict,
    link: str | None,
    max_sub_error_length: int = MAX_VALIDATION_ERROR_ITEM_LENGTH,
) -> str:
    """Provide a more helpful + complete validation error message.

    This is a modified version of voluptuous.error.Invalid.__str__,
    the modifications make some minor changes to the formatting.
    """
    if isinstance(validation_error, vol.MultipleInvalid):
        return "\n".join(
            sorted(
                humanize_error(sub_error, domain, config, link, max_sub_error_length)
                for sub_error in validation_error.errors
            )
        )
    return stringify_invalid(
        validation_error, domain, config, link, max_sub_error_length
    )


@callback
def format_homeassistant_error(
    ex: HomeAssistantError, domain: str, config: dict, link: str | None = None
) -> str:
    """Format HomeAssistantError thrown by a custom config validator."""
    message = f"Invalid config for [{domain}]: {str(ex) or repr(ex)}"

    if domain != CONF_CORE and link:
        message += f" Please check the docs at {link}."

    return message


@callback
def format_schema_error(
    ex: vol.Invalid, domain: str, config: dict, link: str | None = None
) -> str:
    """Format configuration validation error."""
    return humanize_error(ex, domain, config, link)


async def async_process_ha_core_config(hass: HomeAssistant, config: dict) -> None:
    """Process the [homeassistant] section from the configuration.

    This method is a coroutine.
    """
    config = CORE_CONFIG_SCHEMA(config)

    # Only load auth during startup.
    if not hasattr(hass, "auth"):
        if (auth_conf := config.get(CONF_AUTH_PROVIDERS)) is None:
            auth_conf = [{"type": "homeassistant"}]

        mfa_conf = config.get(
            CONF_AUTH_MFA_MODULES,
            [{"type": "totp", "id": "totp", "name": "Authenticator app"}],
        )

        setattr(
            hass, "auth", await auth.auth_manager_from_config(hass, auth_conf, mfa_conf)
        )

    await hass.config.async_load()

    hac = hass.config

    if any(
        k in config
        for k in (
            CONF_LATITUDE,
            CONF_LONGITUDE,
            CONF_NAME,
            CONF_ELEVATION,
            CONF_TIME_ZONE,
            CONF_UNIT_SYSTEM,
            CONF_EXTERNAL_URL,
            CONF_INTERNAL_URL,
            CONF_CURRENCY,
            CONF_COUNTRY,
            CONF_LANGUAGE,
        )
    ):
        hac.config_source = ConfigSource.YAML

    for key, attr in (
        (CONF_LATITUDE, "latitude"),
        (CONF_LONGITUDE, "longitude"),
        (CONF_NAME, "location_name"),
        (CONF_ELEVATION, "elevation"),
        (CONF_INTERNAL_URL, "internal_url"),
        (CONF_EXTERNAL_URL, "external_url"),
        (CONF_MEDIA_DIRS, "media_dirs"),
        (CONF_LEGACY_TEMPLATES, "legacy_templates"),
        (CONF_CURRENCY, "currency"),
        (CONF_COUNTRY, "country"),
        (CONF_LANGUAGE, "language"),
    ):
        if key in config:
            setattr(hac, attr, config[key])

    _raise_issue_if_historic_currency(hass, hass.config.currency)
    _raise_issue_if_no_country(hass, hass.config.country)

    if CONF_TIME_ZONE in config:
        hac.set_time_zone(config[CONF_TIME_ZONE])

    if CONF_MEDIA_DIRS not in config:
        if is_docker_env():
            hac.media_dirs = {"local": "/media"}
        else:
            hac.media_dirs = {"local": hass.config.path("media")}

    # Init whitelist external dir
    hac.allowlist_external_dirs = {hass.config.path("www"), *hac.media_dirs.values()}
    if CONF_ALLOWLIST_EXTERNAL_DIRS in config:
        hac.allowlist_external_dirs.update(set(config[CONF_ALLOWLIST_EXTERNAL_DIRS]))

    elif LEGACY_CONF_WHITELIST_EXTERNAL_DIRS in config:
        _LOGGER.warning(
            "Key %s has been replaced with %s. Please update your config",
            LEGACY_CONF_WHITELIST_EXTERNAL_DIRS,
            CONF_ALLOWLIST_EXTERNAL_DIRS,
        )
        hac.allowlist_external_dirs.update(
            set(config[LEGACY_CONF_WHITELIST_EXTERNAL_DIRS])
        )

    # Init whitelist external URL list – make sure to add / to every URL that doesn't
    # already have it so that we can properly test "path ownership"
    if CONF_ALLOWLIST_EXTERNAL_URLS in config:
        hac.allowlist_external_urls.update(
            url if url.endswith("/") else f"{url}/"
            for url in config[CONF_ALLOWLIST_EXTERNAL_URLS]
        )

    # Customize
    cust_exact = dict(config[CONF_CUSTOMIZE])
    cust_domain = dict(config[CONF_CUSTOMIZE_DOMAIN])
    cust_glob = OrderedDict(config[CONF_CUSTOMIZE_GLOB])

    for name, pkg in config[CONF_PACKAGES].items():
        if (pkg_cust := pkg.get(CONF_CORE)) is None:
            continue

        try:
            pkg_cust = CUSTOMIZE_CONFIG_SCHEMA(pkg_cust)
        except vol.Invalid:
            _LOGGER.warning("Package %s contains invalid customize", name)
            continue

        cust_exact.update(pkg_cust[CONF_CUSTOMIZE])
        cust_domain.update(pkg_cust[CONF_CUSTOMIZE_DOMAIN])
        cust_glob.update(pkg_cust[CONF_CUSTOMIZE_GLOB])

    hass.data[DATA_CUSTOMIZE] = EntityValues(cust_exact, cust_domain, cust_glob)

    if CONF_UNIT_SYSTEM in config:
        hac.units = get_unit_system(config[CONF_UNIT_SYSTEM])


def _log_pkg_error(package: str, component: str, config: dict, message: str) -> None:
    """Log an error while merging packages."""
    message = f"Package {package} setup failed. Integration {component} {message}"

    pack_config = config[CONF_CORE][CONF_PACKAGES].get(package, config)
    message += (
        f" (See {getattr(pack_config, '__config_file__', '?')}:"
        f"{getattr(pack_config, '__line__', '?')})."
    )

    _LOGGER.error(message)


def _identify_config_schema(module: ComponentProtocol) -> str | None:
    """Extract the schema and identify list or dict based."""
    if not isinstance(module.CONFIG_SCHEMA, vol.Schema):
        return None

    schema = module.CONFIG_SCHEMA.schema

    if isinstance(schema, vol.All):
        for subschema in schema.validators:
            if isinstance(subschema, dict):
                schema = subschema
                break
        else:
            return None

    try:
        key = next(k for k in schema if k == module.DOMAIN)
    except (TypeError, AttributeError, StopIteration):
        return None
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected error identifying config schema")
        return None

    if hasattr(key, "default") and not isinstance(
        key.default, vol.schema_builder.Undefined
    ):
        default_value = module.CONFIG_SCHEMA({module.DOMAIN: key.default()})[
            module.DOMAIN
        ]

        if isinstance(default_value, dict):
            return "dict"

        if isinstance(default_value, list):
            return "list"

        return None

    domain_schema = schema[key]

    t_schema = str(domain_schema)
    if t_schema.startswith("{") or "schema_with_slug_keys" in t_schema:
        return "dict"
    if t_schema.startswith(("[", "All(<function ensure_list")):
        return "list"
    return None


def _recursive_merge(conf: dict[str, Any], package: dict[str, Any]) -> str | None:
    """Merge package into conf, recursively."""
    duplicate_key: str | None = None
    for key, pack_conf in package.items():
        if isinstance(pack_conf, dict):
            if not pack_conf:
                continue
            conf[key] = conf.get(key, OrderedDict())
            duplicate_key = _recursive_merge(conf=conf[key], package=pack_conf)

        elif isinstance(pack_conf, list):
            conf[key] = cv.remove_falsy(
                cv.ensure_list(conf.get(key)) + cv.ensure_list(pack_conf)
            )

        else:
            if conf.get(key) is not None:
                return key
            conf[key] = pack_conf
    return duplicate_key


async def merge_packages_config(
    hass: HomeAssistant,
    config: dict,
    packages: dict[str, Any],
    _log_pkg_error: Callable = _log_pkg_error,
) -> dict:
    """Merge packages into the top-level configuration. Mutate config."""
    PACKAGES_CONFIG_SCHEMA(packages)
    for pack_name, pack_conf in packages.items():
        for comp_name, comp_conf in pack_conf.items():
            if comp_name == CONF_CORE:
                continue
            # If component name is given with a trailing description, remove it
            # when looking for component
            domain = comp_name.partition(" ")[0]

            try:
                integration = await async_get_integration_with_requirements(
                    hass, domain
                )
                component = integration.get_component()
            except INTEGRATION_LOAD_EXCEPTIONS as ex:
                _log_pkg_error(pack_name, comp_name, config, str(ex))
                continue

            try:
                config_platform: ModuleType | None = integration.get_platform("config")
                # Test if config platform has a config validator
                if not hasattr(config_platform, "async_validate_config"):
                    config_platform = None
            except ImportError:
                config_platform = None

            merge_list = False

            # If integration has a custom config validator, it needs to provide a hint.
            if config_platform is not None:
                merge_list = config_platform.PACKAGE_MERGE_HINT == "list"

            if not merge_list:
                merge_list = hasattr(component, "PLATFORM_SCHEMA")

            if not merge_list and hasattr(component, "CONFIG_SCHEMA"):
                merge_list = _identify_config_schema(component) == "list"

            if merge_list:
                config[comp_name] = cv.remove_falsy(
                    cv.ensure_list(config.get(comp_name)) + cv.ensure_list(comp_conf)
                )
                continue

            if comp_conf is None:
                comp_conf = OrderedDict()

            if not isinstance(comp_conf, dict):
                _log_pkg_error(
                    pack_name, comp_name, config, "cannot be merged. Expected a dict."
                )
                continue

            if comp_name not in config or config[comp_name] is None:
                config[comp_name] = OrderedDict()

            if not isinstance(config[comp_name], dict):
                _log_pkg_error(
                    pack_name,
                    comp_name,
                    config,
                    "cannot be merged. Dict expected in main config.",
                )
                continue

            duplicate_key = _recursive_merge(conf=config[comp_name], package=comp_conf)
            if duplicate_key:
                _log_pkg_error(
                    pack_name, comp_name, config, f"has duplicate key '{duplicate_key}'"
                )

    return config


@overload
async def async_process_component_config(
    hass: HomeAssistant,
    config: ConfigType,
    integration: Integration,
) -> ConfigType | None:
    ...


@overload
async def async_process_component_config(
    hass: HomeAssistant,
    config: ConfigType,
    integration: Integration,
    *,
    raise_on_failure: Literal[True],
) -> ConfigType:
    ...


@overload
async def async_process_component_config(
    hass: HomeAssistant,
    config: ConfigType,
    integration: Integration,
    *,
    raise_on_failure: Literal[False] | bool,
) -> ConfigType | None:
    ...


async def async_process_component_config(
    hass: HomeAssistant,
    config: ConfigType,
    integration: Integration,
    *,
    raise_on_failure: bool = False,
) -> ConfigType | None:
    """Check component configuration and return processed configuration.

    This method must be run in the event loop.
    """

    def _log_and_raise_config_validation_error(
        config_exceptions: list[ConfigExceptionInfo],
    ) -> None:
        """Log and optionally raise an exception for an invalid config."""
        p_ex: ConfigExceptionInfo
        config_error_messages: list[tuple[str, ConfigExceptionInfo, str, str, str]] = []
        general_error_messages: list[tuple[str, ConfigExceptionInfo]] = []
        domain = integration.domain
        for p_ex in config_exceptions:
            link = p_ex.p_integration_link or integration.documentation
            ex = p_ex.ex
            p_name = p_ex.p_name
            p_config = p_ex.p_config
            if (log_message := p_ex.log_message) is None:
                if TYPE_CHECKING:
                    assert p_name is not None and p_config is not None
                if isinstance(ex, vol.Invalid):
                    log_message = format_schema_error(ex, p_name, p_config, link)
                else:
                    if TYPE_CHECKING:
                        assert isinstance(ex, HomeAssistantError)
                    log_message = format_homeassistant_error(ex, p_name, p_config, link)
                config_file = getattr(p_config, "__config_file__", "?")
                line = getattr(p_config, "__line__", "?")
                config_error_messages.append(
                    (domain, p_ex, log_message, config_file, line)
                )
            else:
                general_error_messages.append((domain, p_ex))
            _LOGGER.error(log_message, exc_info=p_ex.log_exception)

        if not raise_on_failure or not config_exceptions:
            return

        placeholders: dict[str, str]
        if len(config_error_messages) == 1 and not general_error_messages:
            domain, p_ex, log_message, config_file, line = config_error_messages[0]
            ex = p_ex.ex
            p_name = p_ex.p_name
            if TYPE_CHECKING:
                assert p_name is not None
            translation_key = p_ex.translation_key
            placeholders = {
                "domain": domain,
                "p_name": p_name,
                "error": str(ex),
                "errors": str(len(config_exceptions)),
                "config_file": config_file,
                "line": line,
            }
        elif len(general_error_messages) == 1 and not config_error_messages:
            domain, p_ex = general_error_messages[0]
            ex = p_ex.ex
            translation_key = p_ex.translation_key
            log_message = p_ex.log_message
            placeholders = {
                "domain": domain,
                "error": str(ex),
                "errors": str(len(config_exceptions)),
            }
        else:
            # We can only raise once, so we raise a generic error
            ex = Exception()
            translation_key = "integration_config_error"
            errors = str(len(config_exceptions))
            log_message = (
                f"Failed to process component config for integration {integration.domain} "
                f"due to multiple errors ({errors}), check the logs for more information."
            )
            placeholders = {
                "domain": integration.domain,
                "errors": errors,
            }
        raise ConfigValidationError(
            log_message,
            translation_domain="homeassistant",
            translation_key=translation_key,
            translation_placeholders=placeholders,
        ) from ex

    parsed_config, config_exceptions = await async_pre_process_component_config(
        hass, config, integration
    )
    _log_and_raise_config_validation_error(config_exceptions)
    return parsed_config


async def async_pre_process_component_config(  # noqa: C901
    hass: HomeAssistant,
    config: ConfigType,
    integration: Integration,
) -> tuple[ConfigType | None, list[ConfigExceptionInfo]]:
    """Check component configuration and return processed configuration.

    This method must be run in the event loop.
    """
    domain = integration.domain
    config_exceptions: list[ConfigExceptionInfo] = []

    try:
        component = integration.get_component()
    except LOAD_EXCEPTIONS as ex:
        log_message = f"Unable to import {domain}: {ex}"
        ex_info = ConfigExceptionInfo(ex, "component_import_err", log_message)
        config_exceptions.append(ex_info)
        return None, config_exceptions

    # Check if the integration has a custom config validator
    config_validator = None
    try:
        config_validator = integration.get_platform("config")
    except ImportError as err:
        # Filter out import error of the config platform.
        # If the config platform contains bad imports, make sure
        # that still fails.
        if err.name != f"{integration.pkg_path}.config":
            log_message = f"Error importing config platform {domain}: {err}"
            ex_info = ConfigExceptionInfo(
                err, "config_platform_import_err", log_message
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions

    if config_validator is not None and hasattr(
        config_validator, "async_validate_config"
    ):
        try:
            return (await config_validator.async_validate_config(hass, config)), []
        except vol.Invalid as ex:
            ex_info = ConfigExceptionInfo(
                ex, "config_validation_err", p_name=domain, p_config=config
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions
        except HomeAssistantError as ex:
            ex_info = ConfigExceptionInfo(
                ex,
                "config_validation_err",
                p_name=domain,
                p_config=config,
                log_exception=True,
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions
        except Exception as ex:  # pylint: disable=broad-except
            log_message = f"Unknown error calling {domain} config validator"
            ex_info = ConfigExceptionInfo(
                ex,
                "config_validator_unknown_err",
                log_message,
                log_exception=True,
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions

    # No custom config validator, proceed with schema validation
    if hasattr(component, "CONFIG_SCHEMA"):
        try:
            return component.CONFIG_SCHEMA(config), []
        except vol.Invalid as ex:
            ex_info = ConfigExceptionInfo(
                ex, "config_validation_err", p_name=domain, p_config=config
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions
        except Exception as ex:  # pylint: disable=broad-except
            log_message = f"Unknown error calling {domain} CONFIG_SCHEMA"
            ex_info = ConfigExceptionInfo(
                ex,
                "config_schema_unknown_err",
                log_message,
                log_exception=True,
            )
            config_exceptions.append(ex_info)
            return None, config_exceptions

    component_platform_schema = getattr(
        component, "PLATFORM_SCHEMA_BASE", getattr(component, "PLATFORM_SCHEMA", None)
    )

    if component_platform_schema is None:
        return config, []

    platforms: list[ConfigType] = []
    for p_name, p_config in config_per_platform(config, domain):
        # Validate component specific platform schema
        try:
            p_validated = component_platform_schema(p_config)
        except vol.Invalid as ex:
            ex_info = ConfigExceptionInfo(
                ex,
                "platform_config_validation_err",
                p_name=domain,
                p_config=p_config,
            )
            config_exceptions.append(ex_info)
            continue
        except Exception as ex:  # pylint: disable=broad-except
            log_message = (
                f"Unknown error validating {p_name} platform config with {domain} component"
                " platform schema"
            )
            ex_info = ConfigExceptionInfo(
                ex,
                "platform_validator_unknown_err",
                log_message,
                p_name=str(p_name),
            )
            config_exceptions.append(ex_info)
            continue

        # Not all platform components follow same pattern for platforms
        # So if p_name is None we are not going to validate platform
        # (the automation component is one of them)
        if p_name is None:
            platforms.append(p_validated)
            continue

        try:
            p_integration = await async_get_integration_with_requirements(hass, p_name)
        except (RequirementsNotFound, IntegrationNotFound) as ex:
            log_message = f"Platform error: {domain} - {ex}"
            ex_info = ConfigExceptionInfo(
                ex, "platform_component_load_err", log_message
            )
            config_exceptions.append(ex_info)
            continue

        try:
            platform = p_integration.get_platform(domain)
        except LOAD_EXCEPTIONS as ex:
            log_message = f"Platform error: {domain} - {ex}"
            ex_info = ConfigExceptionInfo(
                ex,
                "platform_component_load_err",
                log_message,
                log_exception=True,
            )
            config_exceptions.append(ex_info)
            continue

        # Validate platform specific schema
        if hasattr(platform, "PLATFORM_SCHEMA"):
            try:
                p_validated = platform.PLATFORM_SCHEMA(p_config)
            except vol.Invalid as ex:
                platform_name = f"{domain}.{p_name}"
                ex_info = ConfigExceptionInfo(
                    ex,
                    "platform_config_validation_err",
                    p_name=platform_name,
                    p_config=p_config,
                    p_integration_link=p_integration.documentation,
                )
                config_exceptions.append(ex_info)
                continue
            except Exception as ex:  # pylint: disable=broad-except
                log_message = (
                    f"Unknown error validating config for {p_name} platform "
                    f"for {domain} component with PLATFORM_SCHEMA"
                )
                ex_info = ConfigExceptionInfo(
                    ex,
                    "platform_schema_validator_err",
                    log_message,
                    p_name=p_name,
                    log_exception=True,
                )
                config_exceptions.append(ex_info)
                continue

        platforms.append(p_validated)

    # Create a copy of the configuration with all config for current
    # component removed and add validated config back in.
    config = config_without_domain(config, domain)
    config[domain] = platforms

    return config, config_exceptions


@callback
def config_without_domain(config: ConfigType, domain: str) -> ConfigType:
    """Return a config with all configuration for a domain removed."""
    filter_keys = extract_domain_configs(config, domain)
    return {key: value for key, value in config.items() if key not in filter_keys}


async def async_check_ha_config_file(hass: HomeAssistant) -> str | None:
    """Check if Home Assistant configuration file is valid.

    This method is a coroutine.
    """
    # pylint: disable-next=import-outside-toplevel
    from .helpers import check_config

    res = await check_config.async_check_ha_config_file(hass)

    if not res.errors:
        return None
    return res.error_str


@callback
def async_notify_setup_error(
    hass: HomeAssistant, component: str, display_link: str | None = None
) -> None:
    """Print a persistent notification.

    This method must be run in the event loop.
    """
    # pylint: disable-next=import-outside-toplevel
    from .components import persistent_notification

    if (errors := hass.data.get(DATA_PERSISTENT_ERRORS)) is None:
        errors = hass.data[DATA_PERSISTENT_ERRORS] = {}

    errors[component] = errors.get(component) or display_link

    message = "The following integrations and platforms could not be set up:\n\n"

    for name, link in errors.items():
        show_logs = f"[Show logs](/config/logs?filter={name})"
        part = f"[{name}]({link})" if link else name
        message += f" - {part} ({show_logs})\n"

    message += "\nPlease check your config and [logs](/config/logs)."

    persistent_notification.async_create(
        hass, message, "Invalid config", "invalid_config"
    )


def safe_mode_enabled(config_dir: str) -> bool:
    """Return if safe mode is enabled.

    If safe mode is enabled, the safe mode file will be removed.
    """
    safe_mode_path = os.path.join(config_dir, SAFE_MODE_FILENAME)
    safe_mode = os.path.exists(safe_mode_path)
    if safe_mode:
        os.remove(safe_mode_path)
    return safe_mode


async def async_enable_safe_mode(hass: HomeAssistant) -> None:
    """Enable safe mode."""

    def _enable_safe_mode() -> None:
        Path(hass.config.path(SAFE_MODE_FILENAME)).touch()

    await hass.async_add_executor_job(_enable_safe_mode)
