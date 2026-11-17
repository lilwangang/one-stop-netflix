from __future__ import annotations

import hashlib
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NetflixAuthError, NetflixError, NetflixHistory
from .const import CONF_NETFLIX_ID, CONF_SECURE_ID, DOMAIN

COOKIE_FIELDS = vol.Schema({vol.Required(CONF_NETFLIX_ID): str, vol.Required(CONF_SECURE_ID): str})
ADDON_REPO = "https://github.com/lilwangang/one-stop-addons"
ADDON_SLUG = "one_stop_signin"


def _helper_hosts(hass) -> list[str]:
    hosts = []
    try:
        from homeassistant.components.hassio import get_addons_info
        for slug in (get_addons_info(hass) or {}):
            if slug.endswith(ADDON_SLUG):
                hosts.append(slug.replace("_", "-"))
    except Exception:
        pass
    h = hashlib.sha1(ADDON_REPO.lower().encode()).hexdigest()[:8]
    hosts += [f"{h}-one-stop-signin", "local-one-stop-signin"]
    return list(dict.fromkeys(hosts))


async def _check(hass, data) -> tuple[NetflixHistory | None, str | None]:
    api = NetflixHistory(async_get_clientsession(hass), data[CONF_NETFLIX_ID], data[CONF_SECURE_ID])
    try:
        await api.viewing_activity(pages=1, page_size=5)
    except NetflixAuthError:
        return None, "signed_out"
    except NetflixError:
        return None, "cannot_read"
    except Exception:
        return None, "cannot_connect"
    return api, None


class FlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._helper: str | None = None
        self._sid: str | None = None
        self._profiles: list[str] = []
        self._reauth = False

    # ---------- sign-in helper ----------
    async def _find_helper(self) -> str | None:
        s = async_get_clientsession(self.hass)
        for host in _helper_hosts(self.hass):
            try:
                async with s.get(f"http://{host}:8099/health", timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status == 200:
                        return f"http://{host}:8099"
            except Exception:
                continue
        return None

    async def _call(self, path: str, payload: dict) -> dict:
        s = async_get_clientsession(self.hass)
        try:
            async with s.post(self._helper + path, json=payload, timeout=aiohttp.ClientTimeout(total=90)) as r:
                return await r.json(content_type=None)
        except Exception as err:
            return {"step": "error", "message": f"The sign-in helper didn't answer ({err})."}

    async def _route(self, res: dict):
        step = res.get("step")
        self._sid = res.get("session", self._sid)
        if step in ("code", "password"):
            return await (self.async_step_code() if step == "code" else self.async_step_password())
        if step == "profiles":
            self._profiles = res.get("profiles") or []
            if len(self._profiles) <= 1:
                return await self._finish_profile(0)
            return await self.async_step_profile()
        if step == "done":
            return await self._create({CONF_NETFLIX_ID: res["netflix_id"], CONF_SECURE_ID: res["secure_netflix_id"]}, res.get("profile"))
        return self.async_show_form(step_id="email", data_schema=self._email_schema(), errors={"base": "helper"},
                                    description_placeholders={"message": res.get("message") or "Unknown problem."})

    def _email_schema(self):
        return vol.Schema({vol.Required("email"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL, autocomplete="username"))})

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        self._helper = await self._find_helper()
        if self._helper:
            return self.async_show_menu(step_id="user", menu_options=["email", "cookies"])
        return await self.async_step_cookies()

    async def async_step_email(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="email", data_schema=self._email_schema(), description_placeholders={"message": ""})
        return await self._route(await self._call("/netflix/start", {"email": user_input["email"]}))

    async def async_step_code(self, user_input=None, message: str = ""):
        if user_input is None:
            return self.async_show_form(step_id="code", data_schema=vol.Schema({vol.Required("code"): str}), description_placeholders={"message": message})
        res = await self._call("/netflix/answer", {"session": self._sid, "kind": "code", "value": user_input["code"]})
        if res.get("step") == "code":
            return self.async_show_form(step_id="code", data_schema=vol.Schema({vol.Required("code"): str}), errors={"base": "wrong_code"},
                                        description_placeholders={"message": res.get("message") or ""})
        return await self._route(res)

    async def async_step_password(self, user_input=None):
        schema = vol.Schema({vol.Required("password"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"))})
        if user_input is None:
            return self.async_show_form(step_id="password", data_schema=schema)
        res = await self._call("/netflix/answer", {"session": self._sid, "kind": "password", "value": user_input["password"]})
        if res.get("step") == "password":
            return self.async_show_form(step_id="password", data_schema=schema, errors={"base": "wrong_password"})
        return await self._route(res)

    async def async_step_profile(self, user_input=None):
        if user_input is None:
            opts = [selector.SelectOptionDict(value=str(i), label=n) for i, n in enumerate(self._profiles)]
            return self.async_show_form(step_id="profile", data_schema=vol.Schema({vol.Required("profile"): selector.SelectSelector(selector.SelectSelectorConfig(options=opts, mode=selector.SelectSelectorMode.LIST))}))
        return await self._finish_profile(int(user_input["profile"]))

    async def _finish_profile(self, idx: int):
        return await self._route(await self._call("/netflix/profile", {"session": self._sid, "index": idx}))

    async def _create(self, data: dict, profile: str | None):
        api, err = await _check(self.hass, data)
        if err:
            return self.async_abort(reason=err)
        data = {CONF_NETFLIX_ID: api.netflix_id, CONF_SECURE_ID: api.secure_id}
        name = profile or api.profile
        if self._reauth:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        await self.async_set_unique_id((name or "netflix").lower())
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=f"Netflix – {name}" if name else "Netflix", data=data)

    # ---------- manual (advanced) ----------
    async def async_step_cookies(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            api, err = await _check(self.hass, user_input)
            if err:
                errors["base"] = err
            else:
                return await self._create({CONF_NETFLIX_ID: api.netflix_id, CONF_SECURE_ID: api.secure_id}, api.profile)
        return self.async_show_form(step_id="cookies", data_schema=COOKIE_FIELDS, errors=errors)

    # ---------- reconnect ----------
    async def async_step_reauth(self, entry_data):
        self._reauth = True
        return await self.async_step_user()
