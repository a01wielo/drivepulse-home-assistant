"""Azure protocol, shared sensors, persisted cursor, replay and recoverable failures."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import mock_aiohttp_client

from custom_components.drivepulse.azure import (
    AzureAuthError,
    AzureClient,
    AzureError,
    AzureRetryError,
    AzureSync,
    normalize_api_url,
)
from custom_components.drivepulse.const import (
    CONF_API_URL,
    CONF_MODE,
    CONF_READ_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_VEHICLE,
    DOMAIN,
    MODE_AZURE,
)
from custom_components.drivepulse.diagnostics import async_get_config_entry_diagnostics

from .conftest import find, message

API = "https://example.azurewebsites.net/api/telemetry"
TOKEN = "secret-read-token-" + "r" * 40
SETTINGS = {
    CONF_MODE: MODE_AZURE,
    CONF_API_URL: API,
    CONF_READ_TOKEN: TOKEN,
    CONF_VEHICLE: "default",
    CONF_SCAN_INTERVAL: 30,
}
MATCH = re.compile(r"https://example\.azurewebsites\.net/api/telemetry\?.*")


def page(items=(), cursor="cursor-1", more=False):
    return {"items": list(items), "nextCursor": cursor, "hasMore": more}


def event(id="e1", payload=None):
    return {
        "id": id,
        "receivedAt": "2026-11-04T20:00:00Z",
        "payload": message() if payload is None else payload,
    }


@pytest.fixture
async def azure_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="DrivePulse Azure", data=SETTINGS)
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.drivepulse.azure.AzureClient.read",
            new=AsyncMock(return_value=page()),
        ),
        patch.object(AzureSync, "start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://name:pass@example.test",
        "https://example.test?token=x",
        "https://example.test/other",
        "https://example.test/#x",
    ],
)
def test_reject_unsafe_url(url):
    with pytest.raises(ValueError):
        normalize_api_url(url)


def test_normalize_base_url():
    assert normalize_api_url("https://example.azurewebsites.net/") == API
    assert normalize_api_url("https://example.azurewebsites.net/api") == API


async def test_http_request_and_page_validation(hass):
    client = AzureClient(async_get_clientsession(hass), API, TOKEN)
    with (
        mock_aiohttp_client() as mock,
        patch("aiohttp.ClientSession._request", new=mock.match_request),
    ):
        mock.get(MATCH, json=page([event()]))
        result = await client.read("saved-cursor")
        assert len(result["items"]) == 1
        request = mock.mock_calls[0]
        assert request[3]["Authorization"] == f"Bearer {TOKEN}"
        assert request[1].query["cursor"] == "saved-cursor"


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AzureAuthError),
        (403, AzureAuthError),
        (429, AzureRetryError),
        (500, AzureRetryError),
        (302, AzureError),
        (400, AzureError),
    ],
)
async def test_http_errors(hass, status, error):
    client = AzureClient(async_get_clientsession(hass), API, TOKEN)
    with (
        mock_aiohttp_client() as mock,
        patch("aiohttp.ClientSession._request", new=mock.match_request),
    ):
        mock.get(MATCH, status=status, headers={"Retry-After": "120"})
        with pytest.raises(error) as caught:
            await client.read()
        assert TOKEN not in str(caught.value)
        if status == 429:
            assert caught.value.retry_after == 120


@pytest.mark.parametrize(
    "error", [TimeoutError(), aiohttp.ClientConnectionError(TOKEN)]
)
async def test_network_errors_are_redacted(hass, error):
    with (
        mock_aiohttp_client() as mock,
        patch("aiohttp.ClientSession._request", new=mock.match_request),
    ):
        mock.get(MATCH, exc=error)
        with pytest.raises(AzureRetryError) as caught:
            await AzureClient(async_get_clientsession(hass), API, TOKEN).read()
        assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        page([{"id": "x"}]),
        page([], more=True),
        page([event()], cursor="same", more=True),
        page(cursor=""),
    ],
)
async def test_invalid_pages_never_reach_handler(hass, body):
    with (
        mock_aiohttp_client() as mock,
        patch("aiohttp.ClientSession._request", new=mock.match_request),
    ):
        mock.get(MATCH, json=body)
        with pytest.raises(AzureError):
            await AzureClient(async_get_clientsession(hass), API, TOKEN).read("same")


async def test_cloud_flow_checks_read_token(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_AZURE}
    )
    assert result["step_id"] == "azure"
    data = {k: v for k, v in SETTINGS.items() if k != CONF_MODE}
    with (
        mock_aiohttp_client() as mock,
        patch("aiohttp.ClientSession._request", new=mock.match_request),
    ):
        mock.get(MATCH, status=401)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
        assert result["errors"] == {"base": "invalid_auth"}
        mock.clear_requests()
        mock.get(MATCH, json=page())
        # Prevent automatic setup from starting a poll task after the flow completes.
        with patch("custom_components.drivepulse.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], data
            )
            await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODE] == MODE_AZURE


async def test_pages_use_existing_sensor_logic_and_resume_after_reload(
    hass, azure_entry, hass_storage
):
    hub = azure_entry.runtime_data
    sync = hub.azure_sync
    with patch.object(
        sync.client,
        "read",
        new=AsyncMock(
            side_effect=[
                page(
                    [event("start", message("trip_start", fuel_level_pct=64))],
                    "one",
                    True,
                ),
                page(
                    [
                        event(
                            "end",
                            message(
                                "trip_end", odometer_km=12345, trip={"distance_km": 5}
                            ),
                        )
                    ],
                    "two",
                ),
            ]
        ),
    ) as read:
        assert await sync.async_poll() is False
        assert read.call_args_list[1].args[0] == "one"
    await hass.async_block_till_done()
    assert (
        hass.states.get(find(hass, azure_entry, "sensor", "odometer")).state == "12345"
    )
    assert (
        hass.states.get(find(hass, azure_entry, "sensor", "fuel_level")).state == "62.0"
    )
    assert hub.azure_cursor == "two"
    stored = hass_storage[f"{DOMAIN}.{azure_entry.entry_id}"]["data"]
    assert stored["azure_cursor"] == "two"
    assert stored["azure_recent_ids"] == ["start", "end"]
    with (
        patch(
            "custom_components.drivepulse.azure.AzureClient.read",
            new=AsyncMock(return_value=page(cursor="two")),
        ) as read,
        patch.object(AzureSync, "start"),
    ):
        assert await hass.config_entries.async_reload(azure_entry.entry_id)
        await hass.async_block_till_done()
        assert read.call_args.args[0] == "two"
    assert azure_entry.runtime_data.azure_cursor == "two"
    assert (
        hass.states.get(find(hass, azure_entry, "sensor", "fuel_level")).state == "62.0"
    )


async def test_duplicate_events_do_not_repeat_automations(hass, azure_entry):
    hub = azure_entry.runtime_data
    fired = []
    hass.bus.async_listen("drivepulse_trip_start", lambda event: fired.append(event))
    response = page(
        [event("same", message("trip_start")), event("same", message("trip_start"))]
    )
    with patch.object(
        hub.azure_sync.client, "read", new=AsyncMock(return_value=response)
    ):
        await hub.azure_sync.async_poll()
        await hub.azure_sync.async_poll()
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_failure_on_second_page_does_not_advance_past_it(hass, azure_entry):
    hub = azure_entry.runtime_data
    with (
        patch.object(
            hub.azure_sync.client,
            "read",
            new=AsyncMock(
                side_effect=[page([event()], "committed", True), AzureRetryError()]
            ),
        ),
        pytest.raises(AzureRetryError),
    ):
        await hub.azure_sync.async_poll()
    assert hub.azure_cursor == "committed"
    with patch.object(
        hub.azure_sync.client,
        "read",
        new=AsyncMock(return_value=page([event("e2")], "next")),
    ) as read:
        await hub.azure_sync.async_poll()
        assert read.call_args.args[0] == "committed"


async def test_storage_failure_keeps_cursor_and_deduplicates_retry(hass, azure_entry):
    hub = azure_entry.runtime_data
    with patch.object(
        hub.azure_sync.client, "read", new=AsyncMock(return_value=page([event()]))
    ):
        with (
            patch.object(
                hub,
                "async_flush",
                new=AsyncMock(side_effect=OSError("disk unavailable")),
            ),
            pytest.raises(OSError),
        ):
            await hub.azure_sync.async_poll()
        assert hub.azure_cursor is None
        with patch.object(hub, "process_payload", wraps=hub.process_payload) as process:
            await hub.azure_sync.async_poll()
            process.assert_not_called()
        assert hub.azure_cursor == "cursor-1"


async def test_test_and_unknown_messages_create_no_vehicle(hass, azure_entry):
    hub = azure_entry.runtime_data
    with patch.object(
        hub.azure_sync.client,
        "read",
        new=AsyncMock(
            return_value=page(
                [event("t", message("test")), event("unknown", {"event": "future"})]
            )
        ),
    ):
        await hub.azure_sync.async_poll()
    assert hub.vehicles == {}
    assert hub.azure_cursor == "cursor-1"


async def test_background_backoff_and_auth_reauthentication(hass, azure_entry, caplog):
    sync = azure_entry.runtime_data.azure_sync
    with (
        patch.object(
            sync,
            "async_poll",
            new=AsyncMock(
                side_effect=[AzureRetryError(120), False, AzureAuthError("rejected")]
            ),
        ),
        patch(
            "custom_components.drivepulse.azure.asyncio.sleep", new=AsyncMock()
        ) as sleep,
        patch.object(azure_entry, "async_start_reauth") as reauth,
    ):
        await sync._run()
        assert [c.args[0] for c in sleep.call_args_list] == [120, 30]
        reauth.assert_called_once_with(hass)
    assert TOKEN not in caplog.text


async def test_diagnostics_exclude_secrets_and_payload(hass, azure_entry):
    data = await async_get_config_entry_diagnostics(hass, azure_entry)
    assert TOKEN not in repr(data)
    assert API not in repr(data)
    assert set(data) == {
        "mode",
        "scan_interval",
        "vehicle_count",
        "cursor_present",
        "sync_running",
    }


async def test_stop_cancels_poll_task(hass, azure_entry):
    sync = azure_entry.runtime_data.azure_sync
    sync.start()
    await sync.stop()
    assert sync.task.done()


async def test_existing_webhook_can_switch_to_azure_and_back(hass, entry, send):
    await send(message())
    original_id = find(hass, entry, "sensor", "battery_voltage")
    original_webhook = entry.data["webhook_id"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: MODE_AZURE, "local_only": True}
    )
    assert result["step_id"] == "azure"
    settings = {k: v for k, v in SETTINGS.items() if k != CONF_MODE}
    with (
        patch(
            "custom_components.drivepulse.azure.AzureClient.read",
            new=AsyncMock(return_value=page()),
        ),
        patch.object(AzureSync, "start"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], settings
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.runtime_data.azure_sync is not None
    assert original_webhook not in hass.data["webhook"]
    assert find(hass, entry, "sensor", "battery_voltage") == original_id
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "webhook", "local_only": True}
    )
    await hass.async_block_till_done()
    assert original_webhook in hass.data["webhook"]
    assert entry.runtime_data.azure_sync is None


async def test_options_do_not_echo_stored_token(hass, azure_entry):
    result = await hass.config_entries.options.async_init(azure_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: MODE_AZURE}
    )
    assert TOKEN not in repr(result)
    with (
        patch(
            "custom_components.drivepulse.azure.AzureClient.read",
            new=AsyncMock(return_value=page()),
        ),
        patch.object(AzureSync, "start"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_API_URL: API, CONF_VEHICLE: "default", CONF_SCAN_INTERVAL: 60},
        )
        await hass.async_block_till_done()
    assert azure_entry.options[CONF_READ_TOKEN] == TOKEN
    assert azure_entry.options[CONF_SCAN_INTERVAL] == 60


async def test_failed_processing_replays_page_without_duplicate_side_effects(
    hass, azure_entry
):
    hub = azure_entry.runtime_data
    payload = page(
        [event("first", message("trip_start")), event("second", message("trip_end"))]
    )
    original = hub.process_payload
    attempts = 0

    def fail_once(body):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("handler failed")
        original(body)

    with patch.object(
        hub.azure_sync.client, "read", new=AsyncMock(return_value=payload)
    ):
        with (
            patch.object(hub, "process_payload", side_effect=fail_once),
            pytest.raises(RuntimeError),
        ):
            await hub.azure_sync.async_poll()
        assert hub.azure_cursor is None
        assert list(hub.azure_recent_ids) == ["first"]
        with patch.object(hub, "process_payload", wraps=original) as process:
            await hub.azure_sync.async_poll()
            process.assert_called_once_with(payload["items"][1]["payload"])
    assert hub.azure_cursor == "cursor-1"


async def test_startup_auth_failure_starts_reauth(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=SETTINGS)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.drivepulse.azure.AzureClient.read",
        new=AsyncMock(side_effect=AzureAuthError("rejected")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(
        flow["context"]["source"] == config_entries.SOURCE_REAUTH for flow in flows
    )


async def test_idle_poll_does_not_rewrite_storage(hass, azure_entry):
    hub = azure_entry.runtime_data
    hub.azure_cursor = "same"
    with (
        patch.object(
            hub.azure_sync.client,
            "read",
            new=AsyncMock(return_value=page(cursor="same")),
        ),
        patch.object(hub, "async_flush", new=AsyncMock()) as flush,
    ):
        await hub.azure_sync.async_poll()
        flush.assert_not_called()


async def test_reauth_updates_token_even_if_options_overrode_it(hass, azure_entry):
    hass.config_entries.async_update_entry(
        azure_entry, options={CONF_READ_TOKEN: TOKEN, CONF_API_URL: API}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": azure_entry.entry_id,
        },
        data=azure_entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    renewed = "renewed-token-" + "x" * 40
    with (
        patch(
            "custom_components.drivepulse.azure.AzureClient.read",
            new=AsyncMock(return_value=page()),
        ),
        patch.object(AzureSync, "start"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_URL: API,
                CONF_READ_TOKEN: renewed,
                CONF_VEHICLE: "default",
                CONF_SCAN_INTERVAL: 30,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert azure_entry.data[CONF_READ_TOKEN] == renewed
    assert CONF_READ_TOKEN not in azure_entry.options


async def test_page_budget_limits_work_per_poll(hass, azure_entry):
    hub = azure_entry.runtime_data
    pages = [page([event(str(n))], str(n), True) for n in range(10)]
    with patch.object(
        hub.azure_sync.client, "read", new=AsyncMock(side_effect=pages)
    ) as read:
        assert await hub.azure_sync.async_poll() is True
        assert read.await_count == 10
    assert hub.azure_cursor == "9"
