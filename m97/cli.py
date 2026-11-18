"""The `m97` command: run the dashboard."""

import argparse
import logging
import socket
import sys

from . import __version__, settings as settings_module

__all__ = ["main"]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="m97", description="A web dashboard and control panel for a gufo server.")
    parser.add_argument("--lan", action="store_true",
                        help="listen on all interfaces, so the page is reachable from the network "
                             "(default: this machine only)")
    parser.add_argument("--settings", default=str(settings_module.USER_SETTINGS),
                        help="the user settings file (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"m97 {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = settings_module.load(args.settings)
    except settings_module.SettingsError as exc:
        print(f"m97: {exc}", file=sys.stderr)
        return 2

    # Imported here so that `m97 --help` and a settings error stay fast.
    from nicegui import ui

    from .app import build
    from .hub import Hub

    port = settings["server"]["port"]
    if args.lan:
        print(f"m97: http://{socket.gethostname()}:{port}  (all interfaces)", flush=True)
    else:
        print(f"m97: http://127.0.0.1:{port}  (this machine only; --lan to serve the network)", flush=True)
    build(Hub(settings))
    ui.run(host="0.0.0.0" if args.lan else "127.0.0.1", port=port,
           title="m97", reload=False, show=False, dark=True,
           # The page refreshes itself from a timer, and uses no bindings, so the 10 Hz binding sweep would
           # only wake the CPU.
           binding_refresh_interval=None,
           show_welcome_message=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
