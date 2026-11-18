"""'Netflix now playing' sensors: Netflix's Apple TV app never reports titles, so work it out from the
account's viewing activity while an Apple TV is in Netflix."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import DOMAIN

NETFLIX_APP = "com.netflix.Netflix"
POLL = timedelta(seconds=150)


def _in_netflix(st) -> bool:
    return bool(st) and st.state in ("playing", "paused") and (
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


class Tracker:
    def __init__(self, hass, link, sensors):
        self.hass, self.link, self.sensors = hass, link, sensors
        self.tops: dict = {}          # profile guid -> (key, item)
        self.guess: str | None = None
        self.busy = False

    @callback
    def on_change(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if _in_netflix(new) != _in_netflix(old):
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
            changed, today = [], datetime.now().date()
            for p in profiles:
                try:
                    items = await self.link.fetch(refresh=True, profile=p["guid"])
                except Exception:
                    continue
                if not items:
                    continue
                top = items[0]
                key = f"{top.get('t')}|{top.get('ep')}"
                prev = self.tops.get(p["guid"])
                self.tops[p["guid"]] = (key, top, p["name"])
                if prev is not None and prev[0] != key:
                    changed.append(p["guid"])
            if changed:
                self.guess = changed[0]
            elif self.guess is None:
                recent = [g for g, (_, it, _) in self.tops.items() if datetime.fromtimestamp(it.get("ts", 0) / 1000).date() == today]
                self.guess = recent[0] if recent else None
            if self.guess and self.guess in self.tops:
                _, item, name = self.tops[self.guess]
                for p in active:
                    self.sensors[p].show(item, name, sure=bool(changed))
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

    def show(self, item, profile, sure):
        self._attr_native_value = (item.get("t") or "")[:250]
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": self._player, "service": "netflix",
                                              "episode": item.get("ep") or None, "profile": profile, "url": item.get("url"),
                                              "confidence": "confirmed" if sure else "likely"}
        self.async_write_ha_state()

    def clear(self):
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"one_stop_now_playing": True, "media_player": self._player}
        self.async_write_ha_state()
