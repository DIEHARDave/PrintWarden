"""Load and validate config.yaml."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Printer:
    name: str
    host: str
    port: int = 7125
    api_key: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/websocket"


@dataclass
class FfmpegOptions:
    binary: str = "ffmpeg"
    codec: str = "libx264"
    crf: int = 23
    preset: str = "medium"
    extra: list[str] = field(default_factory=list)


@dataclass
class Config:
    printers: list[Printer]
    output_dir: Path = Path("/output")
    work_dir: Path = Path("/work")
    state_file: Path = Path("/data/state.json")
    # Turn moonraker-timelapse's own render off on every printer we connect to.
    disable_printer_render: bool = True
    render_cancelled: bool = False
    # On (re)connect, render frames left on an idle printer that we haven't rendered yet.
    catch_up: bool = True
    download_concurrency: int = 4
    max_parallel_renders: int = 1
    ffmpeg: FfmpegOptions = field(default_factory=FfmpegOptions)


def load(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    printers = [Printer(**p) for p in raw.pop("printers", [])]
    if not printers:
        raise ValueError(f"{path}: no printers configured")
    names = [p.name for p in printers]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: printer names must be unique")
    ffmpeg = FfmpegOptions(**(raw.pop("ffmpeg", None) or {}))
    for key in ("output_dir", "work_dir", "state_file"):
        if key in raw:
            raw[key] = Path(raw[key])
    return Config(printers=printers, ffmpeg=ffmpeg, **raw)
