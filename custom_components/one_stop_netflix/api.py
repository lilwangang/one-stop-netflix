"""Reads a Netflix profile's viewing activity using the account's own website sign-in cookies.

Netflix has no public API for this. These are the same requests netflix.com/viewingactivity makes,
so Netflix can change them at any time.
"""
from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from .const import BASE, UA


class NetflixAuthError(Exception):
    """The cookies are missing, expired or signed out."""


class NetflixError(Exception):
    """Netflix answered with something unexpected."""


def _clean(value: str) -> str:
    v = (value or "").strip().strip(";").strip()
    for name in ("SecureNetflixId=", "NetflixId="):
        if v.startswith(name):
            v = v[len(name):]
    return v


class NetflixHistory:
    def __init__(self, session: aiohttp.ClientSession, netflix_id: str, secure_id: str) -> None:
        self._s = session
        self.netflix_id = _clean(netflix_id)
        self.secure_id = _clean(secure_id)
        self._build: str | None = None
        self.profile: str | None = None
        self.cookies_changed = False

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": f"NetflixId={self.netflix_id}; SecureNetflixId={self.secure_id}",
        }

    def _take_cookies(self, resp: aiohttp.ClientResponse) -> None:
        # Netflix refreshes the sign-in cookies now and then; keep the newest so the link lasts.
        for name, morsel in resp.cookies.items():
            if name == "NetflixId" and morsel.value and morsel.value != self.netflix_id:
                self.netflix_id = morsel.value
                self.cookies_changed = True
            elif name == "SecureNetflixId" and morsel.value and morsel.value != self.secure_id:
                self.secure_id = morsel.value
                self.cookies_changed = True

    async def _get(self, path: str, accept: str, params: dict | None = None) -> aiohttp.ClientResponse:
        resp = await self._s.get(BASE + path, headers=self._headers(accept), params=params,
                                 allow_redirects=False, timeout=aiohttp.ClientTimeout(total=25))
        self._take_cookies(resp)
        return resp

    async def _build_id(self) -> str:
        if self._build:
            return self._build
        resp = await self._get("/browse", "text/html,application/xhtml+xml")
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "login" in loc.lower():
                raise NetflixAuthError("Netflix signed this connection out")
            resp = await self._get("/" + loc.split("netflix.com/", 1)[-1].lstrip("/"), "text/html")
        html = await resp.text()
        if "/login" in str(resp.url) or 'name="userLoginId"' in html:
            raise NetflixAuthError("Netflix signed this connection out")
        m = re.search(r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"', html)
        if not m:
            raise NetflixError("Couldn't read Netflix's page (it may have changed)")
        self._build = m.group(1)
        p = re.search(r'"name"\s*:\s*"([^"]+)"\s*,\s*"isAccountOwner"', html) or re.search(r'"profileName"\s*:\s*"([^"]+)"', html)
        if p:
            self.profile = p.group(1).encode().decode("unicode_escape", errors="ignore")
        return self._build

    async def viewing_activity(self, pages: int = 1, page_size: int = 40) -> list[dict[str, Any]]:
        build = await self._build_id()
        candidates = [f"/api/shakti/{build}/viewingactivity", "/api/shakti/mre/viewingactivity"]
        last_err: Exception | None = None
        for path in candidates:
            items: list[dict[str, Any]] = []
            try:
                for pg in range(pages):
                    resp = await self._get(path, "application/json, text/javascript, */*", {"pg": pg, "pgSize": page_size})
                    if resp.status in (401, 403):
                        raise NetflixAuthError("Netflix signed this connection out")
                    if resp.status != 200:
                        raise NetflixError(f"Netflix answered {resp.status}")
                    data = await resp.json(content_type=None)
                    got = data.get("viewedItems") or []
                    items.extend(got)
                    if len(got) < page_size:
                        break
                return [_item(x) for x in items]
            except NetflixAuthError:
                raise
            except Exception as err:  # try the next known address
                last_err = err
                self._build = None if path.startswith("/api/shakti/v") else self._build
        raise NetflixError(str(last_err or "No viewing activity found"))


def _item(x: dict[str, Any]) -> dict[str, Any]:
    series = x.get("seriesTitle") or ""
    title = series or x.get("title") or x.get("videoTitle") or ""
    ep = (x.get("episodeTitle") or x.get("title") or "") if series else ""
    if series and ep.startswith(series):
        ep = ep[len(series):].lstrip(": ")
    ts = x.get("date") or 0
    extra = {"nodate": True} if x.get("nodate") else {}
    if x.get("image"):
        extra["image"] = x["image"]
    return {**extra,
        "t": title,
        "ep": ep,
        "ts": int(ts) if ts else int(time.time() * 1000),
        "id": str(x.get("series") or x.get("movieID") or ""),
        "video_id": str(x.get("movieID") or ""),
        "o": "SHOW" if series else "MOVIE",
        "progress": round(100 * (x.get("bookmark") or 0) / x["duration"]) if x.get("duration") else None,
        "url": x.get("url") or (f"https://www.netflix.com/title/{x.get('series') or x.get('movieID')}" if (x.get("series") or x.get("movieID")) else None),
    }


class HelperHistory:
    """Reads history through the One Stop Sign-in Helper add-on, which keeps the signed-in browser."""

    def __init__(self, session: aiohttp.ClientSession, base: str, account: str, profile: str | None = None, service: str = "netflix") -> None:
        self._s, self.base, self.account, self.profile, self.service = session, base, account, profile, service
        self.cookies_changed = False

    profiles: list = []

    async def viewing_activity(self, pages: int = 1, page_size: int = 40, profile: str | None = None) -> list[dict[str, Any]]:
        try:
            async with self._s.post(self.base + f"/{self.service}/history", json={"account": self.account, "size": page_size * pages, "profile": profile},
                                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.json(content_type=None)
        except Exception as err:
            raise NetflixError(f"Sign-in helper not reachable: {err}") from err
        if data.get("error") in ("signed_out", "unknown_account"):
            raise NetflixAuthError(data["error"])
        if data.get("error"):
            raise NetflixError(data["error"])
        if data.get("profiles"):
            self.profiles = data["profiles"]
        self.active = data.get("profile_guid")
        return [_item(x) for x in data.get("items") or []]
