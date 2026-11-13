"""Web search, weather and EV charging lookups."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import Settings

log = logging.getLogger("info")

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
       66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers",
       86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm"}


def _domain(u: str) -> str:
    d = urlparse(u).netloc
    return d.removeprefix("www.")


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


class Search:
    def __init__(self, cfg: Settings, http: httpx.AsyncClient, cache: Path) -> None:
        self.cfg, self.http, self.cache = cfg, http, cache
        pref = cfg.get("SEARCH_PROVIDER", "auto").lower()
        order = [pref] if pref != "auto" else []
        order += [p for p in ("exa", "tavily", "brave", "serper", "wikipedia") if p not in order]
        keys = {"exa": "EXA_API_KEY", "tavily": "TAVILY_API_KEY", "brave": "BRAVE_SEARCH_API_KEY", "serper": "SERPER_API_KEY"}
        self.order = [p for p in order if p == "wikipedia" or cfg.get(keys.get(p, ""))]

    @property
    def label(self) -> str:
        return {"exa": "Exa", "tavily": "Tavily", "brave": "Brave", "serper": "Google", "wikipedia": "Wikipedia"}[self.order[0]]

    async def run(self, query: str, n: int = 5) -> dict:
        last = None
        for p in self.order:
            try:
                res = await getattr(self, "_" + p)(query, n)
                if res["results"]:
                    res["provider"] = p
                    if res.get("image"):
                        res["image"] = await self._cache_image(res["image"])
                    return res
            except Exception as e:                                   # noqa: BLE001
                last = e
                log.warning("search %s failed: %s", p, e)
        if last:
            raise last
        return {"answer": "", "results": [], "image": ""}

    async def _exa(self, q, n):
        r = await self.http.post(
            "https://api.exa.ai/search", headers={"x-api-key": self.cfg.get("EXA_API_KEY")},
            json={"query": q, "numResults": n, "type": "auto",
                  "contents": {"text": {"maxCharacters": 700}}}, timeout=12)
        r.raise_for_status()
        res = r.json().get("results", [])
        img = next((x["image"] for x in res if x.get("image")), "")
        return {"answer": "", "image": img,
                "results": [{"title": x.get("title") or _domain(x["url"]), "url": x["url"],
                             "snippet": _strip(x.get("text", ""))[:500],
                             "source": _domain(x["url"]), "published": x.get("publishedDate", "")}
                            for x in res]}

    async def _tavily(self, q, n):
        k = self.cfg.get("TAVILY_API_KEY")
        r = await self.http.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {k}"},
                                 json={"api_key": k, "query": q, "max_results": n, "include_answer": True,
                                       "include_images": True, "search_depth": "basic"})
        r.raise_for_status()
        d = r.json()
        imgs = d.get("images") or []
        img = imgs[0] if imgs and isinstance(imgs[0], str) else (imgs[0].get("url") if imgs else "")
        return {"answer": d.get("answer") or "", "image": img,
                "results": [{"title": x["title"], "url": x["url"], "snippet": x.get("content", "")[:400],
                             "source": _domain(x["url"])} for x in d.get("results", [])]}

    async def _brave(self, q, n):
        r = await self.http.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": n},
                                headers={"X-Subscription-Token": self.cfg.get("BRAVE_SEARCH_API_KEY"),
                                         "Accept": "application/json"})
        r.raise_for_status()
        res = (r.json().get("web") or {}).get("results", [])
        img = next((x["thumbnail"]["src"] for x in res if x.get("thumbnail")), "")
        return {"answer": "", "image": img,
                "results": [{"title": _strip(x["title"]), "url": x["url"], "snippet": _strip(x.get("description", "")),
                             "source": _domain(x["url"])} for x in res]}

    async def _serper(self, q, n):
        r = await self.http.post("https://google.serper.dev/search", json={"q": q, "num": n},
                                 headers={"X-API-KEY": self.cfg.get("SERPER_API_KEY")})
        r.raise_for_status()
        d = r.json()
        kg = d.get("knowledgeGraph") or {}
        ab = d.get("answerBox") or {}
        return {"answer": ab.get("answer") or ab.get("snippet") or kg.get("description", ""),
                "image": kg.get("imageUrl", ""),
                "results": [{"title": x["title"], "url": x["link"], "snippet": x.get("snippet", ""),
                             "source": _domain(x["link"])} for x in d.get("organic", [])[:n]]}

    async def _wikipedia(self, q, n):
        h = {"User-Agent": "CabinAssistant/1.0 (dev@elevatics.ai)"}
        r = await self.http.get("https://en.wikipedia.org/w/rest.php/v1/search/page",
                                params={"q": q, "limit": n}, headers=h)
        r.raise_for_status()
        pages = r.json().get("pages", [])
        answer, image = "", ""
        if pages:
            s = await self.http.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + pages[0]["key"], headers=h)
            if s.status_code == 200:
                sd = s.json()
                answer = sd.get("extract", "")
                image = (sd.get("originalimage") or sd.get("thumbnail") or {}).get("source", "")
        return {"answer": answer, "image": image,
                "results": [{"title": p["title"], "url": f"https://en.wikipedia.org/wiki/{p['key']}",
                             "snippet": _strip(p.get("excerpt", "")) or p.get("description") or "",
                             "source": "wikipedia.org"} for p in pages]}

    async def _cache_image(self, url: str) -> str:
        if not url:
            return ""
        try:
            ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
            path = self.cache / ("img_" + hashlib.sha1(url.encode()).hexdigest()[:16] + ext)
            if not path.exists():
                r = await self.http.get(url, headers={"User-Agent": "CabinAssistant/1.0"}, follow_redirects=True)
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/"):
                    return ""
                path.write_bytes(r.content)
            return path.as_uri()
        except Exception as e:                                       # noqa: BLE001
            log.info("image cache skipped: %s", e)
            return ""


class Weather:
    def __init__(self, cfg: Settings, http: httpx.AsyncClient) -> None:
        self.cfg, self.http = cfg, http
        self.metric = cfg.metric

    async def forecast(self, lat: float, lon: float, days: int = 3) -> dict:
        r = await self.http.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": max(1, min(days, 7)),
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "temperature_unit": "celsius" if self.metric else "fahrenheit",
            "wind_speed_unit": "kmh" if self.metric else "mph"})
        r.raise_for_status()
        d = r.json()
        cur, daily = d["current"], d["daily"]
        out = {"unit": "°C" if self.metric else "°F",
               "now": {"temp": round(cur["temperature_2m"]), "cond": WMO.get(cur["weather_code"], "—"),
                       "wind": round(cur["wind_speed_10m"]), "humidity": cur.get("relative_humidity_2m")},
               "days": []}
        for i, day in enumerate(daily["time"]):
            name = "Today" if i == 0 else dt.date.fromisoformat(day).strftime("%A")
            out["days"].append({"day": name, "lo": round(daily["temperature_2m_min"][i]),
                                "hi": round(daily["temperature_2m_max"][i]),
                                "cond": WMO.get(daily["weather_code"][i], "—"),
                                "rain": daily.get("precipitation_probability_max", [None] * 99)[i]})
        return out


class Charging:
    def __init__(self, cfg: Settings, http: httpx.AsyncClient, maps) -> None:
        self.cfg, self.http, self.maps = cfg, http, maps
        self.key = cfg.get("OPENCHARGEMAP_API_KEY")

    async def near(self, lat: float, lon: float, n: int = 5) -> list[dict]:
        if self.key:
            try:
                r = await self.http.get("https://api.openchargemap.io/v3/poi/", params={
                    "output": "json", "latitude": lat, "longitude": lon, "distance": 15, "distanceunit": "Miles",
                    "maxresults": n, "compact": "true", "verbose": "false", "minpowerkw": 50, "key": self.key})
                r.raise_for_status()
                out = []
                for p in r.json():
                    a = p.get("AddressInfo", {})
                    kw = max((c.get("PowerKW") or 0 for c in p.get("Connections", [])), default=0)
                    out.append({"name": a.get("Title", "Charger"), "town": a.get("Town") or "",
                                "lat": a["Latitude"], "lon": a["Longitude"], "kw": kw,
                                "stalls": p.get("NumberOfPoints") or len(p.get("Connections", []))})
                if out:
                    return out
            except Exception as e:                                   # noqa: BLE001
                log.warning("openchargemap failed: %s", e)
        places = await self.maps.search("EV fast charging station", (lat, lon), n)
        return [{"name": p.name, "town": (p.address.split(",")[0] if p.address else ""), "lat": p.lat,
                 "lon": p.lon, "kw": 0, "stalls": 0} for p in places]
