"""Fixtures for cloud tests."""
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import DEFAULT, MagicMock, patch

from hass_nabucasa.const import DEFAULT_SERVERS, DEFAULT_VALUES
import jwt
import pytest

from homeassistant.components.cloud import CloudClient, const, prefs

from . import mock_cloud, mock_cloud_prefs


@pytest.fixture(name="cloud")
async def cloud_fixture() -> AsyncGenerator[MagicMock, None]:
    """Mock the cloud object."""
    with patch(
        "homeassistant.components.cloud.Cloud", autospec=True
    ) as mock_cloud_class:
        mock_cloud = mock_cloud_class.return_value
        mock_cloud.google_report_state = MagicMock()
        mock_cloud.cloudhooks = MagicMock()
        mock_cloud.remote = MagicMock()
        mock_cloud.auth = MagicMock()
        mock_cloud.iot = MagicMock()
        mock_cloud.voice = MagicMock()

        def set_up_mock_cloud(
            cloud_client: CloudClient, mode: str, **kwargs: Any
        ) -> DEFAULT:
            """Set up mock cloud."""
            mock_cloud.client = cloud_client

            servers = {
                f"{name}_server": server
                for name, server in DEFAULT_SERVERS[mode].items()
            }
            default_values = DEFAULT_VALUES[mode]
            mock_cloud.configure_mock(**default_values, **servers)

            mock_cloud.mode = mode
            mock_cloud.id_token = "test_id_token"
            mock_cloud.access_token = "test_access_token"
            mock_cloud.refresh_token = "test_refresh_token"
            mock_cloud.started = None
            mock_cloud.client.cloud = DEFAULT

            return DEFAULT

        mock_cloud_class.side_effect = set_up_mock_cloud

        yield mock_cloud


@pytest.fixture(autouse=True)
def mock_tts_cache_dir_autouse(mock_tts_cache_dir):
    """Mock the TTS cache dir with empty dir."""
    return mock_tts_cache_dir


@pytest.fixture(autouse=True)
def mock_user_data():
    """Mock os module."""
    with patch("hass_nabucasa.Cloud._write_user_info") as writer:
        yield writer


@pytest.fixture
def mock_cloud_fixture(hass):
    """Fixture for cloud component."""
    hass.loop.run_until_complete(mock_cloud(hass))
    return mock_cloud_prefs(hass)


@pytest.fixture
async def cloud_prefs(hass):
    """Fixture for cloud preferences."""
    cloud_prefs = prefs.CloudPreferences(hass)
    await cloud_prefs.async_initialize()
    return cloud_prefs


@pytest.fixture
async def mock_cloud_setup(hass):
    """Set up the cloud."""
    await mock_cloud(hass)


@pytest.fixture
def mock_cloud_login(hass, mock_cloud_setup):
    """Mock cloud is logged in."""
    hass.data[const.DOMAIN].id_token = jwt.encode(
        {
            "email": "hello@home-assistant.io",
            "custom:sub-exp": "2300-01-03",
            "cognito:username": "abcdefghjkl",
        },
        "test",
    )
    with patch.object(hass.data[const.DOMAIN].auth, "async_check_token"):
        yield


@pytest.fixture(name="mock_auth")
def mock_auth_fixture():
    """Mock check token."""
    with patch("hass_nabucasa.auth.CognitoAuth.async_check_token"), patch(
        "hass_nabucasa.auth.CognitoAuth.async_renew_access_token"
    ):
        yield


@pytest.fixture
def mock_expired_cloud_login(hass, mock_cloud_setup):
    """Mock cloud is logged in."""
    hass.data[const.DOMAIN].id_token = jwt.encode(
        {
            "email": "hello@home-assistant.io",
            "custom:sub-exp": "2018-01-01",
            "cognito:username": "abcdefghjkl",
        },
        "test",
    )
