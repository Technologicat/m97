"""The `m97` command: run the dashboard, or manage it as a systemd user service.

    m97 serve [--lan]          run in the foreground (what the service runs)
    m97 install [--local]      install and enable the service, so m97 starts at boot; serves the network
    m97 uninstall              disable and remove the service
    m97 start | stop           start or stop the service
    m97 status                 running or not, and the URL
    m97 logs [-f]              the service's log
    m97 report [--url URL]     what the dashboard shows, as text (--json for the data, --history for the graphs)
    m97 gufo start | stop | restart
    m97 openwebui start | stop | restart
                               start, stop or restart a service through the running m97, as its buttons do
"""

import argparse
import json
import logging
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__, settings as settings_module

__all__ = ["UNIT_NAME", "unit_path", "unit_file", "url_line",
           "SERVICES",
           "main"]

UNIT_NAME = "m97.service"


def unit_path():
    """Where the systemd user unit goes."""
    return pathlib.Path("~/.config/systemd/user", UNIT_NAME).expanduser()


def unit_file(*, python, lan, settings_path=None):
    """The text of the systemd user unit that runs `python -m m97 serve`.

    `python` is the interpreter to run (the one m97 is installed in), `lan` whether to serve the network,
    `settings_path` a settings file other than the default, or `None`.
    """
    args = [str(python), "-m", "m97", "serve"]
    if lan:
        args.append("--lan")
    if settings_path is not None:
        args += ["--settings", str(settings_path)]
    return f"""[Unit]
Description=m97, a dashboard and control panel for the gufo LLM server

[Service]
ExecStart={" ".join(_quote(a) for a in args)}
Restart=on-failure
RestartSec=5
SyslogIdentifier=m97
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def _quote(arg):
    # systemd splits ExecStart= like a shell would, double quotes included.
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def url_line(port, scope):
    """The line saying where m97 can be reached, given `services.listen_scope`'s answer for its port."""
    if scope == "all":
        return f"  http://{socket.gethostname()}:{port}  (all interfaces)"
    if scope == "local":
        return f"  http://127.0.0.1:{port}  (this machine only)"
    return f"  nothing is listening on port {port} yet"


def _systemctl(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], check=check)


def _load_settings(path):
    try:
        return settings_module.load(path)
    except settings_module.SettingsError as exc:
        sys.exit(f"m97: {exc}")


# --------------------------------------------------------------------------------
# The verbs

def serve(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = _load_settings(args.settings)

    # Imported here so that the other verbs, `--help` and a settings error stay fast.
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


def _wait_until_listening(args, timeout=10.0):
    """Give a just-started service a moment to open its port, so `status` can show the URL."""
    from .services import listen_scope

    port = _load_settings(args.settings)["server"]["port"]
    deadline = time.monotonic() + timeout
    while listen_scope(port) is None and time.monotonic() < deadline:
        time.sleep(0.5)


def install(args):
    _load_settings(args.settings)   # refuse to install a service that would fail on its settings at once
    settings_path = None if args.settings == str(settings_module.USER_SETTINGS) else pathlib.Path(args.settings).resolve()
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_file(python=sys.executable, lan=not args.local, settings_path=settings_path))
    _systemctl("daemon-reload")
    _systemctl("enable", UNIT_NAME)
    _systemctl("restart", UNIT_NAME)   # not `enable --now`, which leaves a running service on the old unit
    print(f"m97: installed {path}, enabled at boot and started")
    _wait_until_listening(args)
    return status(args)


def uninstall(args):
    path = unit_path()
    if not path.exists():
        print("m97: not installed")
        return 0
    _systemctl("disable", "--now", UNIT_NAME, check=False)
    path.unlink()
    _systemctl("daemon-reload")
    print(f"m97: stopped, disabled and removed {path}")
    return 0


def _require_installed():
    if not unit_path().exists():
        sys.exit("m97: the service is not installed; 'm97 install' first, or run 'm97 serve' in the foreground")


def start(args):
    _require_installed()
    _systemctl("start", UNIT_NAME)
    _wait_until_listening(args)
    return status(args)


def stop(args):
    _require_installed()
    _systemctl("stop", UNIT_NAME)
    print("m97: stopped")
    return 0


def status(args):
    from .services import listen_scope

    port = _load_settings(args.settings)["server"]["port"]
    if not unit_path().exists():
        scope = listen_scope(port)
        print("m97: the service is not installed" + ("; something is serving on its port:" if scope else ""))
        if scope:
            print(url_line(port, scope))
        return 0
    active = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME],
                            capture_output=True, text=True).stdout.strip()
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", UNIT_NAME],
                             capture_output=True, text=True).stdout.strip()
    at_boot = "starts at boot" if enabled == "enabled" else "does not start at boot"
    print(f"m97: {'running' if active == 'active' else active} ({at_boot})")
    if active in ("active", "activating"):
        print(url_line(port, listen_scope(port)))
    return 0


def report(args):
    from .report import format_report

    base = (args.url or f"http://127.0.0.1:{_load_settings(args.settings)['server']['port']}").rstrip("/")
    url = f"{base}/api/status" + ("?history=1" if args.history else "")
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            text = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError) as exc:
        print(f"m97: no answer from {url}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(text)
    else:
        try:
            print(format_report(json.loads(text)))
        except (ValueError, KeyError, TypeError) as exc:
            print(f"m97: {url} answered with something that is not m97's status data: {exc!r}", file=sys.stderr)
            return 1
    return 0


SERVICES = {"gufo": "gufo", "openwebui": "Open WebUI"}   # the service's key -> its title
_DOING = {"start": "starting", "stop": "stopping", "restart": "restarting"}


def control(args):
    """Start, stop or restart a service through the running m97, and wait until that is done."""
    settings = _load_settings(args.settings)
    title = SERVICES[args.service]
    url = f"http://127.0.0.1:{settings['server']['port']}/api/services/{args.service}/{args.action}"
    # The request returns when the action is done; a start may take the whole of its timeout.
    timeout = settings[args.service]["start_timeout"] + 120
    print(f"m97: {_DOING[args.action]} {title}...", flush=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8"))["error"]
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {exc.code}"
        print(f"m97: {message}", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"m97: no answer from m97 at {url}: {getattr(exc, 'reason', exc)}. Is it running? 'm97 status' says.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nm97: no longer waiting; m97 goes on {_DOING[args.action]} {title}, and 'm97 report' shows how it went.",
              file=sys.stderr)
        return 130
    print(f"m97: {title} {result['status']}")
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    return 0


def logs(args):
    return subprocess.run(["journalctl", "--user", "-u", UNIT_NAME, *(["-f"] if args.follow else [])]).returncode


# --------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="m97", description="A web dashboard and control panel for a gufo server.",
                                     epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"m97 {__version__}")
    parser.add_argument("--settings", default=str(settings_module.USER_SETTINGS),
                        help="the user settings file (default: %(default)s)")
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    p = verbs.add_parser("serve", help="run in the foreground")
    p.add_argument("--lan", action="store_true",
                   help="listen on all interfaces, so the page is reachable from the network (default: this machine only)")
    p.set_defaults(run=serve)

    p = verbs.add_parser("install", help="install and enable the systemd user service, serving the network")
    p.add_argument("--local", action="store_true", help="serve this machine only")
    p.set_defaults(run=install)

    for name, run, text in (("uninstall", uninstall, "disable and remove the service"),
                            ("start", start, "start the service"),
                            ("stop", stop, "stop the service"),
                            ("status", status, "running or not, and the URL")):
        verbs.add_parser(name, help=text).set_defaults(run=run)

    p = verbs.add_parser("report", help="what the dashboard shows, as text")
    p.add_argument("--url", help="the m97 to ask, e.g. http://ai-server:8097 (default: this machine's)")
    p.add_argument("--json", action="store_true", help="print the data as JSON, unformatted")
    p.add_argument("--history", action="store_true", help="include the graphs' series (with --json)")
    p.set_defaults(run=report)

    for key, title in SERVICES.items():
        p = verbs.add_parser(key, help=f"start, stop or restart {title} through the running m97")
        p.add_argument("action", choices=list(_DOING), help="what to do")
        p.set_defaults(run=control, service=key)

    p = verbs.add_parser("logs", help="the service's log")
    p.add_argument("-f", "--follow", action="store_true", help="keep following it")
    p.set_defaults(run=logs)

    args = parser.parse_args(argv)
    if args.verb is None:
        parser.print_help()
        return 2
    try:
        return args.run(args)
    except subprocess.CalledProcessError as exc:
        print(f"m97: {' '.join(exc.cmd)} failed (exit {exc.returncode})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
