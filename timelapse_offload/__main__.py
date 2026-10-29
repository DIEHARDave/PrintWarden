import argparse
import asyncio
import logging
import os
from pathlib import Path

from . import app, config


def main() -> None:
    parser = argparse.ArgumentParser(prog="timelapse_offload",
                                     description="Render Moonraker timelapse frames off the printer.")
    parser.add_argument("-c", "--config", type=Path,
                        default=Path(os.environ.get("TIMELAPSE_CONFIG", "/config/config.yaml")))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="watch all printers (default)")
    once = sub.add_parser("render", help="render one printer's current frames now")
    once.add_argument("printer")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    cfg = config.load(args.config)

    if args.command == "render":
        result = asyncio.run(app.render_now(cfg, args.printer))
        raise SystemExit(0 if result else 1)
    asyncio.run(app.serve(cfg))


if __name__ == "__main__":
    main()
