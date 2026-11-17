"""One Stop Netflix history: lets One Stop Streaming cards show a profile's real Netflix "Recently played"."""
from __future__ import annotations

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import NetflixAuthError, NetflixError, NetflixHistory
from .const import CACHE_SECONDS, CONF_NETFLIX_ID, CONF_SECURE_ID, DOMAIN

SERVICE = "get_history"
SCHEMA = vol.Schema({vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=200)),
                     vol.Optional("refresh", default=False): bool})


class Link:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.api = NetflixHistory(async_get_clientsession(hass), entry.data[CONF_NETFLIX_ID], entry.data[CONF_SECURE_ID])
        self.items: list = []
        self.t = 0.0
        self.error: str | None = None

    async def fetch(self, refresh: bool = False) -> list:
        if not refresh and self.items and time.time() - self.t < CACHE_SECONDS:
            return self.items
        try:
            self.items = await self.api.viewing_activity(pages=2)
            self.t, self.error = time.time(), None
        except NetflixAuthError as err:
            self.error = "signed_out"
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Netflix needs to be connected again: {err}") from err
        except NetflixError as err:
            self.error = str(err)
            if self.items:
                return self.items
            raise HomeAssistantError(f"Couldn't read Netflix history: {err}") from err
        if self.api.cookies_changed:
            self.api.cookies_changed = False
            self.hass.config_entries.async_update_entry(self.entry, data={
                **self.entry.data, CONF_NETFLIX_ID: self.api.netflix_id, CONF_SECURE_ID: self.api.secure_id})
        return self.items


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    link = Link(hass, entry)
    try:
        await link.fetch(refresh=True)
    except HomeAssistantError as err:
        if link.error == "signed_out":
            raise ConfigEntryAuthFailed(str(err)) from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = link

    if not hass.services.has_service(DOMAIN, SERVICE):
        async def handle(call: ServiceCall) -> ServiceResponse:
            links = list(hass.data.get(DOMAIN, {}).values())
            if not links:
                raise HomeAssistantError("Netflix isn't connected")
            out = []
            for l in links:
                items = await l.fetch(call.data.get("refresh", False))
                out.extend({**i, "profile": l.api.profile or l.entry.title} for i in items)
            out.sort(key=lambda i: i["ts"], reverse=True)
            seen, uniq = set(), []
            for i in out:
                k = (i["t"] or "").lower()
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(i)
            return {"items": uniq[: call.data.get("limit", 30)], "profiles": [l.api.profile or l.entry.title for l in links]}

        hass.services.async_register(DOMAIN, SERVICE, handle, schema=SCHEMA, supports_response=SupportsResponse.ONLY)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE)
    return True
