import importlib
import logging
import types

from cabin import plugins

EXPECTED = (
    "cabin.commerce", "cabin.payment", "cabin.vehicle_tools", "cabin.thermal",
    "cabin.frontend.plugin", "cabin.nav.plugin", "cabin.memory.plugin",
    "cabin.telemetry.plugin", "cabin.driver.plugin", "cabin.agents.plugin",
)


class _Cfg:
    def get(self, key, default=""):
        return ""


class _Engine:
    cfg = _Cfg()


def _fake_import(installed, missing=()):
    def fake(name):
        if name in missing:
            raise ImportError(name)
        mod = types.SimpleNamespace(install=lambda engine, n=name: installed.append(n))
        return mod
    return fake


def test_all_planned_modules_listed_in_order():
    assert tuple(plugins.MODULES) == EXPECTED


def test_missing_module_is_skipped(monkeypatch):
    monkeypatch.delenv("NOVA_PLUGINS_OFF", raising=False)
    installed: list[str] = []
    monkeypatch.setattr(importlib, "import_module", _fake_import(installed, missing={"cabin.nav.plugin"}))
    loaded = plugins.load(_Engine())
    assert "cabin.nav.plugin" not in loaded
    assert "cabin.commerce" in loaded
    assert loaded == installed


def test_plugins_off_by_short_name(monkeypatch, caplog):
    monkeypatch.setenv("NOVA_PLUGINS_OFF", "commerce")
    installed: list[str] = []
    monkeypatch.setattr(importlib, "import_module", _fake_import(installed))
    with caplog.at_level(logging.INFO, logger="plugins"):
        loaded = plugins.load(_Engine())
    assert "cabin.commerce" not in loaded
    assert "cabin.commerce" not in installed
    assert "cabin.payment" in loaded
    assert "plugin cabin.commerce disabled" in caplog.text
