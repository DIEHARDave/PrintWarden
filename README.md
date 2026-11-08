# TimelapseOffload

Render 3D printer timelapses on a real computer instead of on the printer.

Printers running [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse) (Zmod, Kiln OS, FLSUN_Timelapse, most Klipper setups) capture a frame per layer and then render the video on the printer's own slow CPU. TimelapseOffload runs in Docker on another machine. It watches each printer, downloads the frames as soon as a print finishes, renders them with ffmpeg and saves the video to a network share. The printer never renders and never stores videos.

## How it works

1. Connects to each printer's Moonraker websocket and watches `print_stats`.
2. When a print goes from printing to complete, downloads the frames from Moonraker's `timelapse_frames` file root (read-only HTTP; no SSH or printer-side install).
3. Renders with ffmpeg using the printer's own timelapse settings from Mainsail/Fluidd: frame rate or variable fps/target length, rotation and flip, duplicate last frame, preview image.
4. Saves `timelapse_<file>_<date>.mp4` and a preview `.jpg` to `<output>/<printer>/`.
5. With `delete_frames_after_render` (on by default), removes the frames from the printer once the video is safely on the NAS, where the printer allows it (see below).

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

## Save Frames (printer-made zips)

If a printer's timelapse settings have **Save Frames** on, moonraker-timelapse writes `timelapse_<file>_<date>_frames.zip` to its `timelapse` folder when a print completes, even with autorender off. TimelapseOffload then uses that zip: it waits for the zip to finish writing, downloads it in one request, renders it and saves the video as `timelapse_<file>_<date>.mp4`. Once the video is on the NAS it deletes the zip from the printer (`delete_saved_frames`, on by default). Zips don't get wiped when the next print starts, unlike the loose frames, so if the container was down for several prints, every one of them still gets rendered when it reconnects.

Cancelled prints get no zip. With `render_cancelled` they, and printers with Save Frames off, use the frame download below. moonraker-timelapse registers its loose-frames folder read-only. Where that folder sits inside a writable root (Zmod puts it at `gcodes/timelapse/tmp`), TimelapseOffload deletes the frames through that root after rendering, never while a print is running. Elsewhere (e.g. `printer_data/timelapse_frames` on the T1 or Kiln OS) they stay until the plugin clears them at the next print start.

## Fetching frames as a zip

By default each frame is its own HTTP request, about 1000 for a long print. With `fetch: zip` on a printer, Moonraker's `/server/files/zip` packs the frames into one uncompressed zip (store only, so almost no printer CPU) in `zip_root` (default `timelapse`; the frames root itself is read-only). TimelapseOffload downloads it in one request and deletes it from the printer. This helps on printers whose file serving drops individual requests (the Creator 5). The printer needs free space in `zip_root` for one extra copy of the frames (checked first). If anything goes wrong, it falls back to frame-by-frame.

## Sync mode (printers that render on-device)

Some printers render timelapses themselves and never expose the raw frames — the Snapmaker U1, for example, renders on the printer and only publishes finished MP4s. There's nothing to offload, so a printer set to `mode: sync` is instead polled on an interval, and any new videos in its file root are copied to `<output>/<printer>/`. Set `delete_after_sync: true` to also remove them from the printer after a successful copy (off by default). See the U1 block in [config.example.yaml](config.example.yaml).

## Render on demand

Render whatever frames are on a printer right now, even if they were rendered before:

```sh
docker compose run --rm timelapse-offload render T1
```

## Configuration

See [config.example.yaml](config.example.yaml). Output quality is set there (`ffmpeg.crf`, `ffmpeg.preset`); moonraker-timelapse's `constant_rate_factor` and `extraoutputparams` are ignored, since those are tuned for the printer's CPU.

## Not supported

Printers without moonraker-timelapse and without a Moonraker file root of finished videos: Bambu, stock Creality/Anycubic. (The Snapmaker U1 renders on-device but exposes its videos over Moonraker, so it works in `sync` mode.)
