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
    # "frames": offload moonraker-timelapse frame rendering (the default).
    # "sync":   copy already-rendered videos from a file root to the NAS, for
    #           printers that render on-device and only expose finished files
    #           (e.g. the Snapmaker U1's "timelapse" root).
    mode: str = "frames"
    # --- frames mode only ---
    # "files": download each frame with its own request (the default).
    # "zip":   have Moonraker pack the frames into one uncompressed zip in
    #          zip_root, download that, then delete it. Far fewer requests, for
    #          printers whose file serving is flaky; needs room for one extra
    #          copy of the frames. Falls back to "files" if it fails.
    fetch: str = "files"
    zip_root: str = "timelapse"
    # --- sync mode only ---
    sync_root: str = "timelapse"
    sync_suffixes: list[str] = field(default_factory=lambda: [".mp4", "_cover.jpg"])
    delete_after_sync: bool = False

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
    # After a successful render+save, delete the frames from the printer so they
    # don't sit on its storage until the next print. Only the video on the NAS remains.
    delete_frames_after_render: bool = False
    # Printers with "Save Frames" on leave a <name>_frames.zip in their timelapse
    # folder after each completed print; delete it once its video is on the NAS.
    delete_saved_frames: bool = True
    download_concurrency: int = 4
    max_parallel_renders: int = 1
    # How often a sync-mode printer is polled for new finished videos.
    sync_interval: int = 300
    ffmpeg: FfmpegOptions = field(default_factory=FfmpegOptions)


def load(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    printers = [Printer(**p) for p in raw.pop("printers", [])]
    if not printers:
        raise ValueError(f"{path}: no printers configured")
    names = [p.name for p in printers]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: printer names must be unique")
    for p in printers:
        if p.fetch not in ("files", "zip"):
            raise ValueError(f"{path}: {p.name}: fetch must be 'files' or 'zip'")
    ffmpeg = FfmpegOptions(**(raw.pop("ffmpeg", None) or {}))
    for key in ("output_dir", "work_dir", "state_file"):
        if key in raw:
            raw[key] = Path(raw[key])
    return Config(printers=printers, ffmpeg=ffmpeg, **raw)
