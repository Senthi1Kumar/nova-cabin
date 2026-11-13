"""Simulation mode: every feature end to end with no API keys and no network.

Swaps the providers behind the real Engine — nothing else changes:

    LLM      SimBrain         intent parsing → real tool calls → composed spoken replies
    STT      SimSTT           real mic + VAD; transcript = scripted request (or none: autopilot)
    TTS      Phonon           on-device voice when cached; else macOS `say` / espeak-ng, synthetic murmur or silent lip-sync
    Maps     SimMaps          offline gazetteer, POIs, routes; procedural map tiles
    Search   SimSearch        canned topical results
    Weather  SimWeather       seasonal, deterministic forecast
    Charging SimCharging      chargers in the nearest town on the route
"""
from __future__ import annotations

from pathlib import Path

from ..tts import SystemTTS
from .autopilot import TOUR, Autopilot
from .brain import SimBrain
from .services import SimCharging, SimMaps, SimSearch, SimWeather
from .speech import MurmurTTS, SimSTT
from .world import World


def build(engine) -> dict:
    cfg = engine.cfg
    world = World(engine.vehicle.pos)
    online = cfg.get("SIM_MAP_TILES", "offline").lower() == "online"
    maps = SimMaps(cfg, engine.http, engine.cache, world, online)
    voice = cfg.get("SIM_VOICE", "auto").lower()
    tts = None
    if voice in ("auto", "phonon"):                 # the on-device voice when its model is cached
        from ..tts import _phonon
        tts = _phonon(cfg, require_cached=True)
    if tts is None and voice in ("auto", "system"):
        s = SystemTTS(cfg.get("SYSTEM_TTS_VOICE"))
        tts = s if s.available else None
    if tts is None and voice in ("auto", "murmur"):
        tts = MurmurTTS()
    return {
        "world": world, "maps": maps,
        "search": SimSearch(cfg, engine.http, engine.cache, world),
        "weather": SimWeather(cfg, engine.http, world),
        "charging": SimCharging(cfg, engine.http, maps, world),
        "llm": SimBrain(cfg.metric),
        "stt": SimSTT([s.say for s in TOUR]),
        "tts": tts,
    }


def demo_card() -> dict:
    """Static card used by --state screenshots."""
    img = (Path(__file__).resolve().parents[2] / "assets" / "img" / "tahoe.jpg").as_uri()
    return {
        "kind": "trip", "title": "Lake Tahoe", "subtitle": "South Lake Tahoe, CA",
        "hero": {"kind": "image", "source": img, "label": "Lake Tahoe"},
        "rows": [
            {"icon": "route", "label": "Route", "value": "198 mi · 3 h 42 min"},
            {"icon": "bolt", "label": "Charging", "value": "1 stop · Reno, 25 min", "tint": "#FFD27A"},
            {"icon": "battery", "label": "Arrival", "value": "61% battery", "tint": "#8EF0C4"},
            {"icon": "cloud", "label": "Weather", "value": "48–62°F, Mostly sunny"}],
        "actions": [
            {"key": "navigate", "text": "Start Navigation", "icon": "send", "primary": True, "payload": {}},
            {"key": "calendar", "text": "Add to Calendar", "icon": "calendar", "payload": {}},
            {"key": "share", "text": "Share Plan", "icon": "share", "payload": {"text": "Lake Tahoe trip"}}],
    }


__all__ = ["TOUR", "Autopilot", "build", "demo_card"]
