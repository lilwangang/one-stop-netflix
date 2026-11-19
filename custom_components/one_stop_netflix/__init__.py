"""One Stop streaming history: lets One Stop Streaming cards show real "Recently played" for Netflix and Disney+ profiles."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
import voluptuous as vol

from .api import HelperHistory, NetflixAuthError, NetflixError, NetflixHistory, _item
from .const import CACHE_SECONDS, CONF_NETFLIX_ID, CONF_SECURE_ID, DOMAIN
from homeassistant.helpers import config_validation as cv

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE = "get_history"
SCHEMA = vol.Schema({vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=200)),
                     vol.Optional("profile"): str,
                     vol.Optional("service", default="netflix"): vol.In(["netflix", "disney"]),
                     vol.Optional("kind", default="history"): vol.In(["history", "continue", "home"]),
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
        self._bg: dict = {}

    def _refresh_later(self, profile, kind) -> None:
        """Refresh one cached list in the background (once at a time per list)."""
        ck = (profile, kind)
        if ck in self._bg and not self._bg[ck].done():
            return
        async def run():
            try:
                await self.fetch(True, profile, kind)
            except Exception:  # noqa: BLE001 - keep the old list on errors
                pass
        self._bg[ck] = self.hass.async_create_background_task(run(), f"one_stop_netflix refresh {kind}")

    async def warm(self, *_):
        """Keep every profile's Continue watching ready so cards open instantly."""
        if self.entry.data.get("service", "netflix") != "netflix" or not isinstance(self.api, HelperHistory):
            return
        for p in list(getattr(self.api, "profiles", None) or self.entry.data.get("profiles") or []):
            g = p.get("guid") if isinstance(p, dict) else None
            if g:
                try:
                    await self.fetch(True, g, "home")
                except Exception:  # noqa: BLE001
                    pass

    async def fetch(self, refresh: bool = False, profile: str | None = None, kind: str = "history") -> list:
        ck = (profile, kind)
        hit = self.cache.get(ck)
        if not refresh and hit:
            if time.time() - hit[0] >= CACHE_SECONDS:
                self._refresh_later(profile, kind)       # answer now with the last list, update behind the scenes
            return hit[1]
        try:
            if isinstance(self.api, HelperHistory):
                items = await self.api.viewing_activity(pages=1, page_size=60, profile=profile, kind=kind)
            else:
                items = await self.api.viewing_activity(pages=2)
            if kind == "home":
                for vid, eps in (getattr(self.api, "last_episodes", None) or {}).items():
                    self.cache[("ep", str(vid), profile)] = (time.time(), eps)
                cont = next((r for r in items if "continue watching" in r.get("title", "").lower()), None)
                if cont:   # the home page already has Continue watching: keep that list fresh too
                    now = int(time.time() * 1000)
                    self.cache[(profile, "continue")] = (time.time(), [_item({**x, "movieID": x.get("id"), "date": now - i * 60000, "nodate": True}) for i, x in enumerate(cont["items"])])
            if kind == "continue" and not items and hit and hit[1]:
                items = await self.api.viewing_activity(pages=1, page_size=60, profile=profile, kind=kind)   # page was slow: try once more
            self.cache[ck] = (time.time(), items)
            if profile is None and getattr(self.api, "active", None):
                self.cache[(self.api.active, kind)] = self.cache[ck]
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
    entry.async_on_unload(async_track_time_interval(hass, link.warm, timedelta(minutes=10)))
    entry.async_create_background_task(hass, link.warm(), "one_stop_netflix warm")

    if not hass.services.has_service(DOMAIN, SERVICE):
        async def handle(call: ServiceCall) -> ServiceResponse:
            svc = call.data.get("service", "netflix")
            links = [l for l in hass.data.get(DOMAIN, {}).values() if l.entry.data.get("service", "netflix") == svc]
            if not links:
                return {"items": [], "profiles": [], "profile": None, "connected": False}
            out, profiles, active = [], [], None
            if call.data.get("kind") == "home":
                l = links[0]
                rows = await l.fetch(call.data.get("refresh", False), call.data.get("profile"), "home")
                return {"rows": rows, "profiles": getattr(l.api, "profiles", None) or [], "profile": call.data.get("profile") or getattr(l.api, "active", None), "connected": True}
            for l in links:
                items = await l.fetch(call.data.get("refresh", False), call.data.get("profile"), call.data.get("kind", "history"))
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

    if not hass.services.has_service(DOMAIN, "get_episodes"):
        async def episodes(call: ServiceCall) -> ServiceResponse:
            links = [l for l in hass.data.get(DOMAIN, {}).values() if l.entry.data.get("service", "netflix") == "netflix" and isinstance(l.api, HelperHistory)]
            if not links:
                return {"error": "not_connected", "seasons": []}
            l, vid, prof = links[0], str(call.data["id"]), call.data.get("profile")
            ck = ("ep", vid, prof)
            hit = l.cache.get(ck)
            if hit and time.time() - hit[0] < 600:
                return hit[1]
            try:
                res = await l.api.episodes(vid, prof)
            except NetflixError as err:
                raise HomeAssistantError(f"Couldn't read episodes: {err}") from err
            if not res.get("error"):
                l.cache[ck] = (time.time(), res)
            return res
        hass.services.async_register(DOMAIN, "get_episodes", episodes, schema=vol.Schema({vol.Required("id"): cv.string, vol.Optional("profile"): cv.string}), supports_response=SupportsResponse.ONLY)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE)
        hass.services.async_remove(DOMAIN, "get_episodes")
    return True


# ---- Plex library browsing that works even when no Plex app is open ----
# Home Assistant only creates Plex player entities while a Plex app is active, so cards that
# browse through those entities go blank. This websocket command browses the Plex server directly.
@websocket_api.websocket_command({vol.Required("type"): "one_stop/plex_browse",
                                  vol.Optional("media_content_id"): vol.Any(str, None),
                                  vol.Optional("media_content_type"): vol.Any(str, None)})
@websocket_api.async_response
async def ws_plex_browse(hass, connection, msg):
    try:
        from homeassistant.components.plex.media_browser import browse_media
    except ImportError:
        connection.send_error(msg["id"], "no_plex", "The Plex integration isn't set up")
        return
    try:
        res = await hass.async_add_executor_job(
            browse_media, hass, False, msg.get("media_content_type"), msg.get("media_content_id"))
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "browse_failed", str(err))
        return
    out = res.as_dict()
    # thumbnails go through HA's Plex image proxy; sign them so plain <img> tags can load them
    from datetime import timedelta
    from homeassistant.components.http.auth import async_sign_path

    def sign(node):
        t = node.get("thumbnail")
        if isinstance(t, str) and t.startswith("/api/"):
            node["thumbnail"] = async_sign_path(hass, t, timedelta(hours=24), refresh_token_id=connection.refresh_token_id)
        for c in node.get("children") or []:
            sign(c)
    sign(out)
    connection.send_result(msg["id"], out)


async def async_setup(hass: HomeAssistant, config) -> bool:
    websocket_api.async_register_command(hass, ws_plex_browse)
    return True
