"""Turn a folder of frames into an MP4 with ffmpeg."""

import asyncio
import logging
from pathlib import Path

from .config import FfmpegOptions

log = logging.getLogger("render")


def framerate(settings: dict, frame_count: int) -> float:
    """Same rule as moonraker-timelapse: fixed fps, or fit the video to targetlength."""
    if not settings.get("variable_fps"):
        return float(settings.get("output_framerate", 30))
    fps = frame_count / max(float(settings.get("targetlength", 10)), 1)
    lo = float(settings.get("variable_fps_min", 5))
    hi = float(settings.get("variable_fps_max", 60))
    return round(min(max(fps, lo), hi), 2)


def orientation_filters(settings: dict) -> list[str]:
    filters = []
    rotation = int(settings.get("rotation", 0)) % 360
    if rotation == 90:
        filters.append("transpose=1")
    elif rotation == 180:
        filters.append("hflip,vflip")
    elif rotation == 270:
        filters.append("transpose=2")
    if settings.get("flip_x"):
        filters.append("hflip")
    if settings.get("flip_y"):
        filters.append("vflip")
    return filters


async def run_ffmpeg(binary: str, args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}")


async def render(frames_dir: Path, out_file: Path, settings: dict,
                 opts: FfmpegOptions) -> tuple[Path, Path | None]:
    """Render frames_dir/frame%06d.jpg to out_file. Returns (video, preview or None)."""
    count = len(list(frames_dir.glob("frame*.jpg")))
    fps = framerate(settings, count)
    filters = orientation_filters(settings)
    # yuv420p needs even dimensions; webcam snapshots aren't always. Webcam JPEGs are
    # full range, which some players show wrong, so convert to TV range too.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv")
    dup = int(settings.get("duplicatelastframe", 0))
    if dup > 0:
        filters.append(f"tpad=stop_mode=clone:stop_duration={dup / fps:.3f}")

    args = ["-framerate", str(fps), "-i", str(frames_dir / "frame%06d.jpg"),
            "-vf", ",".join(filters),
            "-c:v", opts.codec, "-crf", str(opts.crf), "-preset", opts.preset,
            "-pix_fmt", settings.get("pixelformat") or "yuv420p",
            "-movflags", "+faststart", *opts.extra, str(out_file)]
    log.info("rendering %d frames at %s fps -> %s", count, fps, out_file.name)
    await run_ffmpeg(opts.binary, args)

    preview = None
    if settings.get("previewimage", True):
        preview = out_file.with_suffix(".jpg")
        last = frames_dir / f"frame{count:06d}.jpg"
        vf = ",".join(orientation_filters(settings)) or "null"
        await run_ffmpeg(opts.binary, ["-i", str(last), "-vf", vf, "-frames:v", "1",
                                       "-q:v", "2", str(preview)])
    return out_file, preview
