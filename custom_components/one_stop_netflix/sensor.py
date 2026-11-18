"""'Netflix now playing' sensors: Netflix's Apple TV app never reports titles, so work it out from the
account's viewing activity while an Apple TV is in Netflix."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import DOMAIN

NETFLIX_APP = "com.netflix.Netflix"
POLL = timedelta(seconds=150)


def _in_netflix(st) -> bool:
    return bool(st) and st.state in ("playing", "paused", "idle", "on") and (
        st.attributes.get("app_id") == NETFLIX_APP or str(st.attributes.get("app_name", "")).lower() == "netflix")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    if entry.data.get("service", "netflix") != "netflix":
        return
    link = hass.data[DOMAIN][entry.entry_id]
    reg = er.async_get(hass)
    players = [e.entity_id for e in reg.entities.values() if e.domain == "media_player" and e.platform == "apple_tv" and not e.disabled_by]
    if not players:
        return
    sensors = {p: NowPlaying(hass, entry, p) for p in players}
    async_add_entities(sensors.values())
    tracker = Tracker(hass, link, sensors)
    entry.async_on_unload(async_track_state_change_event(hass, players, tracker.on_change))
    entry.async_on_unload(async_track_time_interval(hass, tracker.tick, POLL))
    hass.async_create_task(tracker.tick())


BEFORE, AFTER = 20, 150      # a title counts only if Netflix logged it 20 s before to 150 s after this TV pressed play


class Tracker:
    """Strict matching: a title is shown for a TV only when Netflix logged it right as that TV started playing.
    Anything else (a phone, another TV, browsing) leaves the TV showing just 'Netflix'."""

    def __init__(self, hass, link, sensors):
        self.hass, self.link, self.sensors = hass, link, sensors
        self.presses: dict = {p: [] for p in sensors}   # player -> [epoch seconds it started playing]
        self.current: dict = {}                          # player -> (item, profile name)
        self.claimed: set = set()                         # (video id, ts) already given to a TV
        self.boot = time.time()
        self.busy = False

    @callback
    def on_change(self, event):
        pl, old, new = event.data.get("entity_id"), event.data.get("old_state"), event.data.get("new_state")
        if new and new.state == "playing" and _in_netflix(new) and old and old.state not in ("playing", "unknown", "unavailable") \
                and time.time() - self.boot > 90:
            self.presses[pl] = [t for t in self.presses.get(pl, []) if time.time() - t < 6 * 3600] + [time.time()]
            for d in (40, 100, 170):                       # Netflix logs the title shortly after play starts
                self.hass.loop.call_later(d, lambda: self.hass.async_create_task(self.tick()))
        if _in_netflix(new) != _in_netflix(old):
            if not _in_netflix(new):
                self.current.pop(pl, None)
            self.hass.async_create_task(self.tick())

    async def tick(self, *_):
        active = [p for p in self.sensors if _in_netflix(self.hass.states.get(p))]
        for p, s in self.sensors.items():
            if p not in active and s.native_value is not None:
                s.clear()
        if not active or self.busy:
            return
        self.busy = True
        try:
            profiles = getattr(self.link.api, "profiles", None) or self.link.entry.data.get("profiles") or []
            if not profiles:
                await self.link.fetch(refresh=True)
                profiles = getattr(self.link.api, "profiles", None) or []
            recent = []                                       # (ts seconds, item, profile) from the last 6 hours
            for p in profiles:
                try:
                    items = await self.link.fetch(refresh=True, profile=p["guid"])
                except Exception:
                    continue
                recent += [(it.get("ts", 0) / 1000, it, p["name"]) for it in items[:10] if time.time() - it.get("ts", 0) / 1000 < 6 * 3600]
            recent.sort(key=lambda r: r[0])
            for pl in active:
                cur = self.current.get(pl)
                for ts, it, name in recent:
                    key = (it.get("video_id") or it.get("t"), int(ts))
                    if key in self.claimed:
                        continue
                    if any(-BEFORE <= ts - press <= AFTER for press in self.presses.get(pl, [])):
                        cur = (it, name, ts); self.claimed.add(key)
                    elif cur and name == cur[1] and it.get("id") and it.get("id") == cur[0].get("id") and ts > cur[2] \
                            and self.hass.states.get(pl).state == "playing":
                        cur = (it, name, ts); self.claimed.add(key)   # next episode of the same show, same profile, TV still playing
                if cur:
                    self.current[pl] = cur
                    self.sensors[pl].show(cur[0], cur[1], sure=True, started=cur[2])
                else:
                    self.sensors[pl].plain()
        finally:
            self.busy = False


class NowPlaying(SensorEntity):
    _attr_has_entity_name = False
    _attr_icon = "mdi:netflix"
    _attr_should_poll = False

    def __init__(self, hass, entry, player):
        st = hass.states.get(player)
        room = (st.attributes.get("friendly_name") if st else None) or player.split(".")[1]
        self._player = player
        self._attr_name = f"{room} Netflix now playing"
        self._attr_unique_id = f"{entry.entry_id}_{player}_netflix_now_playing"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": player}

    def show(self, item, profile, sure, started=None):
        self._attr_native_value = (item.get("t") or "")[:250]
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": self._player, "service": "netflix",
                                              "episode": item.get("ep") or None, "profile": profile, "url": item.get("url"),
                                              "confidence": "confirmed" if sure else "likely",
                                              "started": datetime.fromtimestamp(started, timezone.utc).isoformat() if started else None}
        self.async_write_ha_state()

    def plain(self):
        self._attr_native_value = "Netflix"
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": self._player, "service": "netflix", "matched": False}
        self.async_write_ha_state()

    def clear(self):
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": self._player}
        self.async_write_ha_state()
