# TimelapseOffload

Render 3D printer timelapses on a real computer instead of on the printer, and watch running prints for failures ("spaghetti") through the webcam.

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

## Failure detection

While a printer is printing, TimelapseOffload grabs a webcam snapshot every 10 s (the printer's camera from Moonraker's webcam list) and runs [Obico](https://github.com/TheSpaghettiDetective/obico-server)'s open-source failure-detection model on it, on the CPU, inside the container. Scoring follows Obico's own algorithm: each frame's detections are summed, smoothed, and compared against a long-running baseline for that printer, so a printer whose camera always shows some clutter doesn't keep tripping it. The first 30 checks of each print (5 minutes) only learn what the print looks like.

Set per printer (`detect:` in config, or on the web page):

- `off`: no checks (the default).
- `alert`: when the score crosses the warning level, then again at the failure level, send an alert.
- `pause`: alert at the warning level, and at the failure level pause the print through Moonraker (`/printer/print/pause`). Pausing is never cancelling: resume it from Mainsail/Fluidd/the screen if it was a false alarm. After a resume the printer gets the 5-minute grace period again.

Alerts go to a Discord webhook (`notify.discord_webhook`, with the snapshot attached) and, with `notify.console`, to the printer's console as a `RESPOND` message. The frame that triggered each alert or pause is kept in `/data/flagged/<printer>/` and shown on the page. On the page, **Not a failure** silences alerts for the rest of that print. `sensitivity` (default 1.0) scales the score: raise it to catch failures sooner, lower it if a printer gives false alarms.

### Moved or loosened parts

The model only knows what loose strands look like. A part that came off the bed, tipped over or was pushed aside still looks like a clean part, so it scores nothing until spaghetti piles up. A second check watches for that, on the same snapshots and with the same `detect:` action (alert, or alert + pause):

- It compares the median of the last 2 minutes of snapshots with the median of the 2 minutes before. The median removes the moving toolhead; spots the toolhead kept passing over are left out rather than guessed at.
- Small shifts are tolerated, so the bed lowering, a delta effector rising and new layers growing do not count. A change of light or a bumped camera (most of the picture at once) is skipped.
- A change must cover more than about 1.2% of the picture (scaled by `motion_sensitivity`) and is then only a candidate. It's confirmed once it is still there in frames where the toolhead, read from Klipper, is at least 40 mm from where it was when the change appeared, so a toolhead that sat in one place for a few minutes is not taken for a new object. A move is usually reported 2 to 4 minutes after it happened.
- The alert carries the current snapshot and one from a few minutes earlier (both kept in `/data/flagged/`), and the page outlines the changed area in red.

Per printer: `motion: true|false` (on by default when `detect` is not off) and `motion_sensitivity` (default 1.0; higher catches smaller changes). It cannot see what the toolhead keeps covering, and a slow drift over many minutes is not caught. The first 4 minutes of each print, and the first 4 minutes after a resume, only learn the scene.

The model (~200 MB) is downloaded from obico.io into `/data/models` on first use and checked against its SHA-256. One check takes about 0.2 s of CPU on a desktop and somewhat more on a small VM; checks run one at a time across all printers. The model is licensed AGPL-3.0 by Obico and is not part of this repository.

Snapshots are fetched from the printer's web port (the `snapshot_url` in its webcam config, relative URLs on port 80). Pick a different camera per printer with `webcam:`.

## Web page

There are two pages:

- `http://<docker host>:8095/` is [Printer Monitor](https://github.com/DIEHARDave/printer-monitor): every printer's live camera, remote screen, Mainsail-style card and spools. Here it also reads Print Watch's status and draws the failure boxes (yellow) and moved-part areas (red, dashed) from the last checked snapshot over the live video, with a badge for the score and the tile's border turning orange or red. Each tile's header shows the timelapse handling and the detection mode; clicking the detection chip steps it through off → alert → alert + pause (saved on the server at once, `POST /api/printers/<name>/detect`). Under the spools, a detection row has the failure score and its history, the part-position check and **Not a failure**; recent activity and flagged frames sit in an Activity bar on the bottom of the camera that opens upward over the picture. **Detection** in its toolbar hides the boxes and that row. All of Print Watch's settings are in **Printers…**: each printer's row has its server settings under it (**Watch**, timelapse, detection, webcam, sensitivities, and under ⋯ the server name, port, API key, frame download and copy-mode options), matched by host; printers the server watches that are not on the page get a row added. The "Detection, alerts and timelapses" section below holds Find printers and the settings for all printers (interval, threads, Discord webhook, console message, test alert). Save writes the server side first and only when something changed. The Discord webhook URL never leaves the server once saved: `GET /api/settings` only reports `discord_webhook_set`, a save without `discord_webhook` keeps it and an empty one removes it. **Print Watch** opens the page below, which is kept for now but no longer needed. Its printer list and layout are kept in the browser, per address: to carry them over from another copy of Printer Monitor, use **Printers… → Export…** there and **Import…** here. The page is a copy of the printer-monitor repo's `index.html` in `timelapse_offload/web/monitor.html`; copy it again to update.
- `http://<docker host>:8095/watch/` is Print Watch, with two tabs:

- **Monitor**: one card per printer with its camera (detection boxes drawn on it while printing), print progress, the failure score and its history, the part-position check, recent alerts and flagged frames. With **Live video** (top right, on by default, remembered per browser) the browser plays each printer's MJPEG stream straight from the printer (camera-streamer's WebRTC entry is swapped for its `stream.mjpg`), and the boxes from the latest checked snapshot are drawn over it, labelled with their age. Streams stop while the tab is hidden or on Settings; a camera without a stream falls back to snapshots. Each open page pulls one stream per printer, so keep it to a few viewers on weak printer boards.
- **Settings**: add, find (scans a subnet for Moonraker on port 7125) or remove printers, and per printer choose the timelapse handling (render frames / copy videos / off) and detection (off / alert / alert + pause), webcam, sensitivity and moved-part watching (its sensitivity is under ⋯); plus the check interval and alert settings. Saving applies immediately, no restart, and writes `/data/settings.json`, which from then on replaces the `printers:` list in `config.yaml`. Delete that file to go back to `config.yaml`.

The page has no login; keep the port on your LAN, like Moonraker itself.

## Training data

To build our own failure examples, the camera button at the bottom right of each watched printer's camera on Printer Monitor takes a fresh frame from that printer and opens it on the label page (`/label/`) in a new tab. Drag boxes round problems and pick their type (spaghetti, part came loose or moved, layer shift, warping, stringing, under-extrusion, over-extrusion, blob, cracks, other; keys 1–9 and 0), say whether the frame shows a problem, looks fine or you are not sure, add a note and save. The ✎ on a flagged frame in the Activity list copies that alert frame to the label page, which is how false alarms get recorded.

Frames are kept in `/data/training/` (`<id>.jpg` + `<id>.json` with printer, time, print file and progress, verdict, boxes in pixels and the note). `/label/` lists them; **Export** (`/api/training/export.zip`) downloads the labelled ones as a YOLO dataset (`images/`, `labels/`, `classes.txt`; a "fine" frame has an empty label file) plus the JSON records.

## Render on demand

Render whatever frames are on a printer right now, even if they were rendered before:

```sh
docker compose run --rm timelapse-offload render T1
```

## Configuration

See [config.example.yaml](config.example.yaml). Output quality is set there (`ffmpeg.crf`, `ffmpeg.preset`); moonraker-timelapse's `constant_rate_factor` and `extraoutputparams` are ignored, since those are tuned for the printer's CPU.

## Not supported

Printers without moonraker-timelapse and without a Moonraker file root of finished videos: Bambu, stock Creality/Anycubic. (The Snapmaker U1 renders on-device but exposes its videos over Moonraker, so it works in `sync` mode.)
