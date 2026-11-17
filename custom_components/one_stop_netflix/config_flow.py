from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NetflixAuthError, NetflixError, NetflixHistory
from .const import CONF_NETFLIX_ID, CONF_SECURE_ID, DOMAIN

FIELDS = vol.Schema({vol.Required(CONF_NETFLIX_ID): str, vol.Required(CONF_SECURE_ID): str})


async def _check(hass, data) -> tuple[NetflixHistory | None, str | None]:
    api = NetflixHistory(async_get_clientsession(hass), data[CONF_NETFLIX_ID], data[CONF_SECURE_ID])
    try:
        await api.viewing_activity(pages=1, page_size=5)
    except NetflixAuthError:
        return None, "signed_out"
    except NetflixError:
        return None, "cannot_read"
    except Exception:  # network
        return None, "cannot_connect"
    return api, None


class FlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            api, err = await _check(self.hass, user_input)
            if err:
                errors["base"] = err
            else:
                data = {CONF_NETFLIX_ID: api.netflix_id, CONF_SECURE_ID: api.secure_id}
                title = f"Netflix – {api.profile}" if api.profile else "Netflix"
                await self.async_set_unique_id((api.profile or "netflix").lower())
                self._abort_if_unique_id_configured(updates=data)
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(step_id="user", data_schema=FIELDS, errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api, err = await _check(self.hass, user_input)
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data={CONF_NETFLIX_ID: api.netflix_id, CONF_SECURE_ID: api.secure_id})
        return self.async_show_form(step_id="reauth_confirm", data_schema=FIELDS, errors=errors)
