import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from cabin.config import Settings
from cabin.nav.live_map import LiveMap
from cabin.services.info import Search


class FakeHttp:
    async def post(self, url, **kw):
        assert "exa.ai" in url and kw["headers"]["x-api-key"] == "k"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": [
            {"title": "Headline", "url": "https://www.news.com/a", "text": "Body <b>text</b>", "publishedDate": "d"}]})


def test_exa_is_first_when_keyed(tmp_path):
    cfg = Settings({"EXA_API_KEY": "k"})
    s = Search(cfg, FakeHttp(), Path(tmp_path))
    assert s.order[0] == "exa"
    res = asyncio.run(s.run("today news"))
    assert res["provider"] == "exa" and res["results"][0]["source"] == "news.com"


def test_no_exa_without_key(tmp_path):
    assert "exa" not in Search(Settings({}), FakeHttp(), Path(tmp_path)).order


def test_clear_forgets_old_route():
    eng = SimpleNamespace(emit=lambda *a: None, nav=SimpleNamespace(agent=None), vehicle=None)
    live = LiveMap(eng)
    live._nav_route, live._pins, live._preview_route = [(1, 2), (3, 4)], [(1, 2, "dest")], [(5, 6)]
    live.on_nav({"active": False})
    assert live._nav_route == [] and live._pins == [] and live._preview_route == []


def test_player_gain_soft_limits():
    from cabin.audio import Player
    p = Player(None)
    p.gain = 3.0
    p.enqueue((np.ones(2400) * 0.9 * 32768).clip(-32768, 32767).astype(np.int16).tobytes())
    out = p._pull(2400)
    assert float(np.abs(out).max()) <= 0.9 + 1e-4
