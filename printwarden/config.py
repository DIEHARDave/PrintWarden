"""Load and validate config.yaml, plus the web page's settings overlay."""

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Where state lives. The container uses /data, /work and /output; a native
# install (install.sh) points these at /var/lib/printwarden and friends.
DATA_DIR = Path(os.environ.get("PRINTWARDEN_DATA", "/data"))
WORK_DIR = Path(os.environ.get("PRINTWARDEN_WORK", "/work"))
OUTPUT_DIR = Path(os.environ.get("PRINTWARDEN_OUTPUT", "/output"))

TIMELAPSE_MODES = ("frames", "sync", "off")
DETECT_MODES = ("off", "alert", "pause")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


@dataclass
class Printer:
    name: str
    host: str
    port: int = 7125
    api_key: str = ""
    # Timelapse handling:
    # "frames": offload moonraker-timelapse frame rendering (the default).
    # "sync":   copy already-rendered videos from a file root to the NAS, for
    #           printers that render on-device and only expose finished files
    #           (e.g. the Snapmaker U1's "timelapse" root).
    # "off":    no timelapse handling (detection only).
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
    # --- failure detection ---
    # Spaghetti (Obico model): "off", "alert" (notify only) or "pause" (notify and
    # pause the print).
    detect: str = "off"
    # Webcam name from Moonraker's webcam list; empty = first camera with snapshots.
    webcam: str = ""
    # Obico's "detective sensitivity": higher flags failures sooner (0.5..2).
    sensitivity: float = 1.0
    # Moved parts: a part that moved, tipped over or came off the bed (compares the
    # scene with a few minutes earlier). Its own "off" / "alert" / "pause"; the old
    # true/false meant "follow detect" and now reads as "alert" / "off".
    motion: str = "alert"
    # Higher flags smaller changes (0.1..5).
    motion_sensitivity: float = 1.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/websocket"

    def timelapse_key(self) -> tuple:
        """Fields whose change means the timelapse watcher must restart."""
        d = asdict(self)
        for k in ("detect", "webcam", "sensitivity", "motion", "motion_sensitivity"):
            d.pop(k)
        return tuple(sorted((k, str(v)) for k, v in d.items()))


@dataclass
class FfmpegOptions:
    binary: str = "ffmpeg"
    codec: str = "libx264"
    crf: int = 23
    preset: str = "medium"
    extra: list[str] = field(default_factory=list)


@dataclass
class DetectionOptions:
    # Seconds between snapshots while printing. Obico's scoring is tuned for 10.
    interval: int = 10
    # CPU threads per inference (one inference runs at a time).
    threads: int = 1
    model_dir: Path = DATA_DIR / "models"
    # Our problem-type model (downloaded on first use); false turns it off.
    problem_types: bool = True
    # Frames that triggered an alert/pause are kept here.
    flagged_dir: Path = DATA_DIR / "flagged"


@dataclass
class NotifyOptions:
    # Discord webhook URL; alerts post the flagged snapshot there.
    discord_webhook: str = ""
    # Also print the alert in the printer's console (Mainsail/Fluidd) via RESPOND.
    console: bool = True


@dataclass
class WebOptions:
    enabled: bool = True
    port: int = 8095


@dataclass
class Config:
    printers: list[Printer]
    output_dir: Path = OUTPUT_DIR
    work_dir: Path = WORK_DIR
    state_file: Path = DATA_DIR / "state.json"
    # Printers, detection and notify settings saved from the web page. When this
    # file exists its printer list replaces the one in config.yaml.
    settings_file: Path = DATA_DIR / "settings.json"
    # Turn moonraker-timelapse's own render off on every printer we connect to.
    disable_printer_render: bool = True
    render_cancelled: bool = False
    # On (re)connect, render frames left on an idle printer that we haven't rendered yet.
    catch_up: bool = True
    # After a successful render+save, delete the frames from the printer so they
    # don't sit on its storage until the next print. Only the video on the NAS remains.
    # Only works where the frames folder sits inside a writable root (Zmod);
    # elsewhere it's read-only and the plugin clears it at the next print start.
    delete_frames_after_render: bool = True
    # Printers with "Save Frames" on leave a <name>_frames.zip in their timelapse
    # folder after each completed print; delete it once its video is on the NAS.
    delete_saved_frames: bool = True
    download_concurrency: int = 4
    max_parallel_renders: int = 1
    # How often a sync-mode printer is polled for new finished videos.
    sync_interval: int = 300
    ffmpeg: FfmpegOptions = field(default_factory=FfmpegOptions)
    detection: DetectionOptions = field(default_factory=DetectionOptions)
    notify: NotifyOptions = field(default_factory=NotifyOptions)
    web: WebOptions = field(default_factory=WebOptions)


def _build(cls, raw: dict | None, where: str):
    raw = dict(raw or {})
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{where}: unknown option(s) {', '.join(sorted(unknown))}")
    for f in fields(cls):
        if f.name in raw and f.type in (Path, "Path"):
            raw[f.name] = Path(raw[f.name])
    return cls(**raw)


def parse_printer(raw: dict) -> Printer:
    """Build and validate one printer. Raises ValueError with a readable message."""
    raw = {k: v for k, v in raw.items() if v is not None}
    name = str(raw.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError(f"printer name {name!r}: use letters, digits, space . _ - "
                         "(max 40, starting with a letter or digit)")
    raw["name"] = name
    if not str(raw.get("host", "")).strip():
        raise ValueError(f"{name}: host is required")
    raw["host"] = str(raw["host"]).strip()
    p = _build(Printer, raw, name)
    p.port = int(p.port)
    p.sensitivity = float(p.sensitivity)
    p.motion_sensitivity = float(p.motion_sensitivity)
    m = p.motion
    if isinstance(m, bool) or str(m).strip().lower() in ("1", "0", "true", "false", "yes", "no", "on"):
        m = "alert" if (m is True or str(m).strip().lower() in ("1", "true", "yes", "on")) else "off"
    p.motion = str(m).strip().lower()
    if p.motion not in DETECT_MODES:
        raise ValueError(f"{name}: moved parts must be one of {', '.join(DETECT_MODES)}")
    if p.mode not in TIMELAPSE_MODES:
        raise ValueError(f"{name}: mode must be one of {', '.join(TIMELAPSE_MODES)}")
    if p.fetch not in ("files", "zip"):
        raise ValueError(f"{name}: fetch must be 'files' or 'zip'")
    if p.detect not in DETECT_MODES:
        raise ValueError(f"{name}: detect must be one of {', '.join(DETECT_MODES)}")
    if not 0.1 <= p.sensitivity <= 5:
        raise ValueError(f"{name}: sensitivity must be between 0.1 and 5")
    if not 0.1 <= p.motion_sensitivity <= 5:
        raise ValueError(f"{name}: motion sensitivity must be between 0.1 and 5")
    return p


def parse_printers(items: list[dict]) -> list[Printer]:
    printers = [parse_printer(p) for p in items]
    names = [p.name.lower() for p in printers]
    if len(set(names)) != len(names):
        raise ValueError("printer names must be unique")
    return printers


def load(path: Path) -> Config:
    # No config file: start empty and add printers from the page (saved to
    # settings_file), so the container runs with nothing but its volumes
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.warning("%s not found: starting without printers; add them on the page", path)
        raw = {}
    printers = parse_printers(raw.pop("printers", None) or [])
    sub = {k: _build(cls, raw.pop(k, None), f"{path}: {k}")
           for k, cls in (("ffmpeg", FfmpegOptions), ("detection", DetectionOptions),
                          ("notify", NotifyOptions), ("web", WebOptions))}
    for key in ("output_dir", "work_dir", "state_file", "settings_file"):
        if key in raw:
            raw[key] = Path(raw[key])
    cfg = Config(printers=printers, **sub, **raw)
    apply_settings(cfg, read_settings(cfg.settings_file))
    if not cfg.printers and not cfg.web.enabled:
        raise ValueError(f"{path}: no printers configured")
    return cfg


# --- web-editable settings (printers + detection/notify) ---

EDITABLE = {"detection": ("interval", "threads"), "notify": ("discord_webhook", "console")}


def read_settings(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def apply_settings(cfg: Config, data: dict | None) -> None:
    """Overlay saved web settings onto cfg (validated; raises ValueError)."""
    if not data:
        return
    printers = parse_printers(data.get("printers") or [])
    updates = {}
    for section, keys in EDITABLE.items():
        given = data.get(section) or {}
        target = getattr(cfg, section)
        for k in keys:
            if k in given:
                cur = getattr(target, k)
                val = given[k]
                val = bool(val) if isinstance(cur, bool) else type(cur)(val)
                updates[(section, k)] = val
    if updates.get(("detection", "interval"), 10) < 3:
        raise ValueError("detection interval must be at least 3 seconds")
    if not 1 <= updates.get(("detection", "threads"), 1) <= 8:
        raise ValueError("detection threads must be 1..8")
    hook = updates.get(("notify", "discord_webhook"), "")
    if hook and not re.match(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|"
                             r"canary\.discord\.com)/api/webhooks/", hook):
        raise ValueError("Discord webhook must be a https://discord.com/api/webhooks/... URL")
    cfg.printers = printers
    for (section, k), val in updates.items():
        setattr(getattr(cfg, section), k, val)


def settings_dict(cfg: Config) -> dict:
    printers = []
    for p in cfg.printers:
        d = asdict(p)
        printers.append(d)
    return {"printers": printers,
            **{section: {k: getattr(getattr(cfg, section), k) for k in keys}
               for section, keys in EDITABLE.items()}}


def save_settings(cfg: Config, data: dict) -> None:
    """Validate data, apply it to cfg and write it to the settings file."""
    apply_settings(cfg, data)
    cfg.settings_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.settings_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings_dict(cfg), indent=2), encoding="utf-8")
    tmp.replace(cfg.settings_file)
