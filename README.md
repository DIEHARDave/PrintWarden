# TimelapseOffload

Render 3D printer timelapses on a real computer instead of on the printer.

Printers running [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse) (Zmod, Kiln OS, FLSUN_Timelapse, most Klipper setups) capture a frame per layer and then render the video on the printer's own slow CPU. TimelapseOffload runs in Docker on another machine. It watches each printer, downloads the frames as soon as a print finishes, renders them with ffmpeg and saves the video to a network share. The printer never renders and never stores videos.

## How it works

1. Connects to each printer's Moonraker websocket and watches `print_stats`.
2. When a print goes from printing to complete, downloads the frames from Moonraker's `timelapse_frames` file root (read-only HTTP; no SSH or printer-side install).
3. Renders with ffmpeg using the printer's own timelapse settings from Mainsail/Fluidd: frame rate or variable fps/target length, rotation and flip, duplicate last frame, preview image.
4. Saves `timelapse_<file>_<date>.mp4` and a preview `.jpg` to `<output>/<printer>/`.

On connect it turns off moonraker-timelapse's `autorender` on the printer (`disable_printer_render`). Turn it back on in Mainsail/Fluidd (Settings → Timelapse) if you remove TimelapseOffload.

If the container was down when a print finished, it renders the leftover frames when it reconnects (`catch_up`). The frames stay on the printer until the next print starts, when moonraker-timelapse deletes them.

## Requirements

- Printers with moonraker-timelapse installed and timelapse **enabled**, and the slicer emitting `TIMELAPSE_TAKE_FRAME` per layer.
- The Docker host can reach each printer's Moonraker port (7125). If Moonraker doesn't trust the host's address, set `api_key` for that printer.
- A Docker host with `cifs-utils` (needed for the SMB volume).

## Install

```sh
git clone https://github.com/DIEHARDave/TimelapseOffload.git
cd TimelapseOffload
cp .env.example .env                       # NAS share and credentials
mkdir -p config && cp config.example.yaml config/config.yaml   # printers
docker compose up -d --build
docker compose logs -f
```

## Render on demand

Render whatever frames are on a printer right now, even if they were rendered before:

```sh
docker compose run --rm timelapse-offload render T1
```

## Configuration

See [config.example.yaml](config.example.yaml). Output quality is set there (`ffmpeg.crf`, `ffmpeg.preset`); moonraker-timelapse's `constant_rate_factor` and `extraoutputparams` are ignored, since those are tuned for the printer's CPU.

## Not supported

Printers without moonraker-timelapse: Bambu, stock Creality/Anycubic, Snapmaker U1 stock firmware.
