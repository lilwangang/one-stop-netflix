"""One Stop streaming history: lets One Stop Streaming cards show real "Recently played" for Netflix and Disney+ profiles."""
from __future__ import annotations

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import HelperHistory, NetflixAuthError, NetflixError, NetflixHistory
from .const import CACHE_SECONDS, CONF_NETFLIX_ID, CONF_SECURE_ID, DOMAIN

SERVICE = "get_history"
SCHEMA = vol.Schema({vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=200)),
                     vol.Optional("profile"): str,
                     vol.Optional("service", default="netflix"): vol.In(["netflix", "disney"]),
                     vol.Optional("refresh", default=False): bool})


class Link:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        sess = async_get_clientsession(hass)
        if entry.data.get("account"):
            self.api = HelperHistory(sess, entry.data["helper"], entry.data["account"], entry.data.get("profile"), entry.data.get("service", "netflix"))
        else:
            self.api = NetflixHistory(sess, entry.data[CONF_NETFLIX_ID], entry.data[CONF_SECURE_ID])
        self.cache: dict = {}
        self.error: str | None = None

    async def fetch(self, refresh: bool = False, profile: str | None = None) -> list:
        hit = self.cache.get(profile)
        if not refresh and hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            if isinstance(self.api, HelperHistory):
                items = await self.api.viewing_activity(pages=1, page_size=60, profile=profile)
            else:
                items = await self.api.viewing_activity(pages=2)
            self.cache[profile] = (time.time(), items)
            self.error = None
        except NetflixAuthError as err:
            self.error = "signed_out"
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Netflix needs to be connected again: {err}") from err
        except NetflixError as err:
            self.error = str(err)
            if hit:
                return hit[1]
            raise HomeAssistantError(f"Couldn't read Netflix history: {err}") from err
        if self.api.cookies_changed:
            self.api.cookies_changed = False
            self.hass.config_entries.async_update_entry(self.entry, data={
                **self.entry.data, CONF_NETFLIX_ID: self.api.netflix_id, CONF_SECURE_ID: self.api.secure_id})
        return items


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
            svc = call.data.get("service", "netflix")
            links = [l for l in hass.data.get(DOMAIN, {}).values() if l.entry.data.get("service", "netflix") == svc]
            if not links:
                return {"items": [], "profiles": [], "profile": None, "connected": False}
            out, profiles, active = [], [], None
            for l in links:
                items = await l.fetch(call.data.get("refresh", False), call.data.get("profile"))
                plist = getattr(l.api, "profiles", None) or l.entry.data.get("profiles") or []
                profiles += plist
                active = getattr(l.api, "active", None) or active
                name = next((p["name"] for p in plist if p.get("guid") == active), None) or l.api.profile or l.entry.title
                out.extend({**i, "profile": name} for i in items)
            out.sort(key=lambda i: i["ts"], reverse=True)
            seen, uniq = set(), []
            for i in out:
                k = (i["t"] or "").lower()
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(i)
            return {"items": uniq[: call.data.get("limit", 30)], "profiles": profiles, "profile": active, "connected": True}

        hass.services.async_register(DOMAIN, SERVICE, handle, schema=SCHEMA, supports_response=SupportsResponse.ONLY)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE)
    return True
