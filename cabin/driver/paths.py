"""Where the driver sheet reads and writes DriveOS data: the same biometrics home the payment ladder uses."""
from __future__ import annotations

from pathlib import Path

from ..wallet import data_dir


def biometrics_home(engine) -> Path:
    """The DriveOS home the payment bridge builds its verifiers from (``<cabin data>/driveos``)."""
    bridge = getattr(getattr(engine, "payment", None), "bridge", None)
    base = getattr(bridge, "home", None)
    return (Path(base) if base else data_dir()) / "driveos"


def biometrics_config(home: Path):
    from driveos.config import BiometricsConfig
    return BiometricsConfig(home=Path(home))


def model_config(home: Path):
    """A config whose ``models_dir`` holds the speaker model.

    Models are fetched once into the DriveOS default home; the cabin home only holds templates and profiles.
    """
    cfg = biometrics_config(home)
    from driveos.biometrics.sv import redimnet_path, wespeaker_path
    if redimnet_path(cfg.models_dir).is_file() or wespeaker_path(cfg.models_dir).is_file():
        return cfg
    from driveos.config import BiometricsConfig
    return BiometricsConfig()
