"""Bounded asynchronous Azure reads and restart-safe delivery to the existing hub."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .const import (
    CONF_API_URL,
    CONF_READ_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_VEHICLE,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10


class AzureError(Exception):
    """A deliberately redacted API error."""


class AzureAuthError(AzureError):
    """The read token was rejected."""


class AzureRetryError(AzureError):
    """A temporary failure, optionally with a server-requested delay."""

    def __init__(self, retry_after: float = 0) -> None:
        super().__init__("Azure temporarily unavailable")
        self.retry_after = retry_after


def normalize_api_url(value: str) -> str:
    """Accept an HTTPS app base or telemetry URL; never credentials or query secrets."""
    try:
        url = URL(value.strip())
        if (
            url.scheme != "https"
            or not url.host
            or url.user
            or url.password
            or url.query_string
            or url.fragment
        ):
            raise ValueError
        path = url.path.rstrip("/")
        if path not in ("", "/api", "/api/telemetry"):
            raise ValueError
        return str(url.with_path("/api/telemetry"))
    except (ValueError, TypeError):
        raise ValueError("Invalid HTTPS API address") from None


def azure_settings(entry: ConfigEntry) -> dict[str, Any]:
    """Options can update Azure settings without exposing stored tokens in forms."""
    return {**entry.data, **entry.options}


class AzureClient:
    """Uses Home Assistant's shared client; no redirects and no credential logging."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_url: str,
        token: str,
        vehicle: str = "default",
    ) -> None:
        self.session = session
        self.url = normalize_api_url(api_url)
        self._token = token
        self.vehicle = vehicle

    async def read(self, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        params = {"vehicle": self.vehicle, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        try:
            async with self.session.get(
                self.url,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AzureAuthError("Azure read token rejected")
                if response.status == 429 or response.status >= 500:
                    try:
                        retry_after = min(
                            3600,
                            max(0, float(response.headers.get("Retry-After", "0"))),
                        )
                    except ValueError:
                        retry_after = 0
                    raise AzureRetryError(retry_after)
                if response.status != 200:
                    raise AzureError(f"Azure API returned HTTP {response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise AzureError("Azure response too large")
                try:
                    page = json.loads(data)
                except (ValueError, UnicodeError):
                    raise AzureError("Invalid Azure JSON response") from None
        except (aiohttp.ClientError, TimeoutError):
            # aiohttp exceptions can contain full URLs/headers: never propagate those.
            raise AzureRetryError() from None
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise AzureError("Invalid Azure page")
        if (
            type(page.get("hasMore")) is not bool
            or not isinstance(page.get("nextCursor"), str)
            or not page["nextCursor"]
        ):
            raise AzureError("Invalid Azure cursor")
        if len(page["items"]) > limit or len(page["nextCursor"]) > 2048:
            raise AzureError("Invalid Azure page size")
        for item in page["items"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                or len(item["id"]) > 128
                or not isinstance(item.get("payload"), dict)
            ):
                raise AzureError("Invalid Azure event")
        if page["hasMore"] and (not page["items"] or page["nextCursor"] == cursor):
            raise AzureError("Azure pagination made no progress")
        return page


def client_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> AzureClient:
    settings = azure_settings(entry)
    return AzureClient(
        async_get_clientsession(hass),
        settings[CONF_API_URL],
        settings[CONF_READ_TOKEN],
        settings.get(CONF_VEHICLE, "default"),
    )


class AzureSync:
    """One cancellable poll loop per entry; a failed page never advances its cursor."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, hub: Any, client: AzureClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.client = client
        self.interval = int(
            azure_settings(entry).get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        self.task: asyncio.Task | None = None

    async def async_poll(self) -> bool:
        """Drain a bounded number of pages; return whether more pages remain."""
        for _ in range(MAX_PAGES):
            page = await self.client.read(self.hub.azure_cursor)
            if not page["items"] and page["nextCursor"] == self.hub.azure_cursor:
                return False  # An idle poll must not rewrite Home Assistant storage.
            # All events have already passed structural validation before any side effects.
            for item in page["items"]:
                if item["id"] in self.hub.azure_recent_ids:
                    continue
                self.hub.process_payload(item["payload"])
                self.hub.azure_recent_ids.append(item["id"])
            old_cursor = self.hub.azure_cursor
            self.hub.azure_cursor = page["nextCursor"]
            try:
                await self.hub.async_flush()
            except Exception:
                self.hub.azure_cursor = old_cursor
                raise
            if not page["hasMore"]:
                return False
        return True

    def start(self) -> None:
        self.task = self.entry.async_create_background_task(
            self.hass, self._run(), "DrivePulse Azure sync"
        )

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        failures = 0
        while True:
            delay = self.interval
            try:
                more = await self.async_poll()
                if failures:
                    _LOGGER.info("DrivePulse Azure synchronization recovered")
                failures = 0
                if more:
                    delay = 1
            except AzureAuthError:
                _LOGGER.warning(
                    "DrivePulse Azure read token rejected; reauthentication required"
                )
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as error:  # noqa: BLE001 - redact all transport/processing failures
                # Never attach exception info: it could include a token, payload or cursor.
                failures += 1
                delay = min(900, self.interval * 2 ** min(failures, 6))
                if isinstance(error, AzureRetryError):
                    delay = max(delay, error.retry_after)
                if failures == 1:
                    _LOGGER.warning(
                        "DrivePulse Azure synchronization failed; retrying automatically"
                    )
            await asyncio.sleep(delay)
