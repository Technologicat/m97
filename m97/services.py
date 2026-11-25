"""Starting, stopping and watching the services: gufo and Open WebUI, each in a podman container, and
optionally Raven-server and raven-mcp, each a host process run as a transient systemd user unit.

The containers are run detached and without `--rm`, so a container that dies while loading keeps its log
until the next start or stop removes it; that log is what the page shows as the error. A unit is likewise
started without `--collect`, so one that fails stays behind, `failed`, with its log in the journal.

Both kinds outlive m97: a container's monitor is moved to a scope of its own, and a unit belongs to the
user's systemd from the start. So restarting m97 leaves the services running.
"""

import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.request

__all__ = ["Service", "gufo_service", "openwebui_service",
           "gufo_run_args", "openwebui_run_args", "read_only_mounts",
           "unit_command", "raven_server_service", "raven_mcp_service",
           "configured_services",
           "run", "container_state", "unit_info", "unit_status", "state", "state_note", "log_tail", "log_hint",
           "answers", "is_ready", "start", "stop",
           "follow_events", "EVENT_STATES",
           "derive_status",
           "listen_scope", "parse_proc_net_tcp", "service_url"]

logger = logging.getLogger(__name__)

# The devices gufo's docs pass to the container: the GPU (`kfd`, `dri`) and the XDNA NPU (`accel0`).
GPU_DEVICES = ("/dev/kfd", "/dev/dri", "/dev/accel/accel0")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Service:
    """One service: how to run it, and how to tell it is ready.

    A service runs either in the podman container named `container`, or as the transient systemd user unit
    named `unit` (without `.service`); exactly one of the two is set. `run_args(lan)` returns the arguments
    to `podman run`, or for a unit its command line, with the service listening on all interfaces if `lan` is
    true. `ready_url` answers 200 once the service is up; if empty, the service counts as ready once something
    listens on `port`. `start_timeout` is in seconds. `port` and `url_path` make the address shown to people
    (`/v1` for an API base); `browsable` says whether that address is a page worth opening in a browser.
    `after` lists the keys of the services autostart brings up before this one.
    """
    key: str
    title: str
    run_args: object       # callable: lan -> list of str
    ready_url: str
    start_timeout: float
    port: int
    container: str = ""
    unit: str = ""
    url_path: str = ""
    browsable: bool = False
    after: tuple = ()


# --------------------------------------------------------------------------------
# The podman command lines

def gufo_run_args(gufo, *, lan, devices, uid, gid):
    """The `podman run` arguments for gufo, from the `gufo` settings table."""
    models_dir = pathlib.Path(gufo["models_dir"]).expanduser()
    args = ["-d", "--name", gufo["container"], "--network", "host",
            f"--userns=keep-id:uid={uid},gid={gid}", "--group-add", "keep-groups"]
    for device in devices:
        args += ["--device", device]
    args += ["--ulimit", "memlock=-1",
             "-v", f"{models_dir}:/models:ro",
             gufo["image"],
             "gufo", "serve", "--host", "0.0.0.0" if lan else "127.0.0.1", "--port", str(gufo["port"]),
             "llm", "--served-model-name", gufo["served_name"], "--model", f"/models/{gufo['model']}"]
    if gufo["mtp_model"]:
        args += ["--speculative", "mtp", "--mtp-model", f"/models/{gufo['mtp_model']}"]
    if gufo["sessions"]:
        args += ["--sessions", str(gufo["sessions"])]
    if gufo["context"]:
        args += ["--context", str(gufo["context"])]
    return args + list(gufo["extra_args"])


def openwebui_run_args(openwebui, gufo, *, lan):
    """The `podman run` arguments for Open WebUI, pointed at gufo, from the two settings tables."""
    data = "/app/backend/data"
    return ["-d", "--name", openwebui["container"], "--network", "host",
            "-v", f"{openwebui['volume']}:{data}",
            "-e", f"HOST={'0.0.0.0' if lan else '127.0.0.1'}", "-e", f"PORT={openwebui['port']}",
            # The session signing key, kept in the volume: its default location is outside it, so it would be
            # lost with the container and log everyone out at each restart.
            "-e", f"WEBUI_SECRET_KEY_FILE={data}/.webui_secret_key",
            "-e", f"OPENAI_API_BASE_URL=http://127.0.0.1:{gufo['port']}/v1", "-e", "OPENAI_API_KEY=none",
            "-e", "ENABLE_OLLAMA_API=false",
            *read_only_mounts(openwebui["mounts"]),
            openwebui["image"]]


def read_only_mounts(specs):
    """`-v` arguments mounting each of `specs` read-only: `"host:container"` strings, `~` expanded in the host path.

    A host path that does not exist is skipped with a warning, as is a spec without both halves.
    """
    args = []
    for spec in specs:
        # Split at the last colon: the path inside the container has none, and the host path might.
        host, _, inside = spec.rpartition(":")
        if not host or not inside:
            logger.warning(f"mount `{spec}`: not of the form `host:container`; not mounted")
            continue
        host = pathlib.Path(host).expanduser()
        # podman refuses to start a container whose mount source is missing, and a stale entry must not
        # keep Open WebUI down.
        if not host.exists():
            logger.warning(f"mount `{spec}`: {host} does not exist; not mounted")
            continue
        args += ["-v", f"{host}:{inside}:ro"]
    return args


def gufo_service(settings):
    """The gufo `Service`, from the whole settings dict."""
    gufo = settings["gufo"]
    devices = [d for d in GPU_DEVICES if os.path.exists(d)]
    return Service(key="gufo", title="gufo", container=gufo["container"],
                   run_args=lambda lan: gufo_run_args(gufo, lan=lan, devices=devices,
                                                      uid=os.getuid(), gid=os.getgid()),
                   ready_url=f"http://127.0.0.1:{gufo['port']}/ready",
                   start_timeout=gufo["start_timeout"], port=gufo["port"], url_path="/v1")


def openwebui_service(settings):
    """The Open WebUI `Service`, from the whole settings dict."""
    openwebui = settings["openwebui"]
    return Service(key="openwebui", title="Open WebUI", container=openwebui["container"],
                   run_args=lambda lan: openwebui_run_args(openwebui, settings["gufo"], lan=lan),
                   ready_url=f"http://127.0.0.1:{openwebui['port']}/health",
                   start_timeout=openwebui["start_timeout"], port=openwebui["port"], browsable=True,
                   after=("gufo",))


def unit_command(table, *args, lan):
    """A unit's command line: the settings table's `command` with `~` expanded, then `args`, then `--listen`
    if `lan`, then the table's `extra_args`."""
    return [str(pathlib.Path(table["command"]).expanduser()), *args, *(["--listen"] if lan else []),
            *table["extra_args"]]


def raven_server_service(settings):
    """The Raven-server `Service`, from the whole settings dict; `None` if its `command` is not set."""
    table = settings["raven-server"]
    if not table["command"]:
        return None
    return Service(key="raven-server", title="Raven-server", unit=table["unit"],
                   run_args=lambda lan: unit_command(table, "--port", str(table["port"]), lan=lan),
                   ready_url=f"http://127.0.0.1:{table['port']}/health",
                   start_timeout=table["start_timeout"], port=table["port"])


def raven_mcp_service(settings):
    """The raven-mcp `Service`, from the whole settings dict; `None` if its `command` is not set.

    When m97 runs Raven-server too, raven-mcp is pointed at it.
    """
    table, server = settings["raven-mcp"], settings["raven-server"]
    if not table["command"]:
        return None
    server_args = ["--server-url", f"http://127.0.0.1:{server['port']}"] if server["command"] else []
    # No `ready_url`: raven-mcp has no health endpoint, and an MCP `initialize` would open a session only to
    # close it again. Its HTTP server binds the port once the app is up, so listening is ready enough.
    return Service(key="raven-mcp", title="raven-mcp", unit=table["unit"],
                   run_args=lambda lan: unit_command(table, "--port", str(table["port"]), *server_args, lan=lan),
                   ready_url="", start_timeout=table["start_timeout"], port=table["port"], url_path="/mcp",
                   after=("raven-server",))


def configured_services(settings):
    """The services m97 manages, in the order the page lists them: the containers, then the units set up."""
    candidates = (gufo_service(settings), openwebui_service(settings),
                  raven_server_service(settings), raven_mcp_service(settings))
    return [service for service in candidates if service is not None]


# --------------------------------------------------------------------------------
# Running podman

async def run(*args, timeout=60):
    """Run a command, returning `(returncode, stdout, stderr)` as text. A timeout kills it and returns 124.

    Returns when the command exits, even if something it started keeps running with its output open.
    """
    # The output goes to files rather than pipes: `podman run -d` leaves conmon behind holding them, so reading
    # a pipe to its end would wait for the container to exit.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            return 124, "", f"{args[0]}: timed out after {timeout} s"
        except asyncio.CancelledError:
            _kill(proc)
            raise
        out.seek(0)
        err.seek(0)
        return proc.returncode, out.read().decode(errors="replace"), err.read().decode(errors="replace")


def _in_own_scope(*args):
    """`args`, run in a transient systemd scope of their own when systemd-run is available.

    podman leaves the container's monitor (conmon) in the cgroup of whoever ran `podman run`. Run from the
    m97 service, that is the service's cgroup, and stopping or restarting m97 would stop the containers too.
    """
    if shutil.which("systemd-run") is None:
        return list(args)
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *args]


def _kill(proc):
    try:
        proc.kill()
    except ProcessLookupError:  # it exited just now
        pass


async def container_state(name, *, run=run):
    """The container's state (`running`, `exited`, `created`, ...), or `None` if there is no such container."""
    code, out, _ = await run("podman", "container", "inspect", "--format", "{{.State.Status}}", name)
    return (out.strip() or None) if code == 0 else None


async def unit_info(unit, *, run=run):
    """The unit's `ActiveState`, `Result` and `ExecMainStatus`, as a dict of strings.

    A unit systemd does not know reads as `inactive`, like one that has stopped.
    """
    code, out, _ = await run("systemctl", "--user", "show", f"{unit}.service",
                             "--property=ActiveState,Result,ExecMainStatus")
    info = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line) if code == 0 else {}
    return {"ActiveState": info.get("ActiveState") or "inactive", "Result": info.get("Result", ""),
            "ExecMainStatus": info.get("ExecMainStatus", "")}


def unit_status(active_state, *, listening):
    """A unit service's state, in the vocabulary of `container_state`, from the unit's `ActiveState` and
    whether something listens on the service's port.

    `running` for a unit that is active (or on its way up or down), `failed` for one that failed, which stays
    so until it is started again, `outside` for the port taken with no unit running (the service run by hand),
    and `None` for none of these.
    """
    if active_state in ("active", "activating", "deactivating", "reloading"):
        return "running"
    if active_state == "failed":
        return "failed"
    return "outside" if listening else None


async def state(service, *, run=run):
    """The service's state: `container_state` for a container, `unit_status` for a unit."""
    if service.container:
        return await container_state(service.container, run=run)
    info = await unit_info(service.unit, run=run)
    return unit_status(info["ActiveState"], listening=listen_scope(service.port) is not None)


def state_note(service, state):
    """A few words on a state that is neither running nor absent, for beside the status; or `""`."""
    if state in (None, "running"):
        return ""
    if state == "outside":
        return "started outside m97"
    return f"{'container' if service.container else 'unit'}: {state}"


async def log_tail(service, lines, *, run=run):
    """The service's last `lines` log lines, as one string: from `podman logs`, or from the unit's journal."""
    if service.container:
        _, out, err = await run("podman", "logs", "--tail", str(lines), service.container)
    else:
        _, out, err = await run("journalctl", "--user", "--unit", f"{service.unit}.service",
                                "--lines", str(lines), "--no-pager", "--output", "cat")
    return (out + err).strip()


def log_hint(service):
    """The command that shows the service's whole log, for an error message."""
    if service.container:
        return f"podman logs {service.container}"
    return f"journalctl --user -u {service.unit}"


async def _remove(name, *, run):
    await run("podman", "stop", "-t", "15", name, timeout=30)
    await run("podman", "rm", "-f", name, timeout=30)


def answers(url):
    """Whether `url` answers 200 within a second."""
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def is_ready(service):
    """Whether `service` is up: its `ready_url` answers 200, or, if it has none, something listens on its port."""
    if service.ready_url:
        return answers(service.ready_url)
    return listen_scope(service.port) is not None


async def start(service, *, lan, run=run, ready=None, clock=time.monotonic, poll=2.0):
    """Start `service` and wait until it is ready. Returns `None` on success, or an error message.

    A leftover container in a non-running state is removed first, and a failed unit reset. If the service
    dies during the wait, the message carries its last log lines; if the wait times out, the service is left
    running, since it may only be slow (a cold start reads the whole model from disk).
    """
    is_ready_now = ready or (lambda: asyncio.to_thread(is_ready, service))
    current = await state(service, run=run)
    if current == "running":
        return f"{service.title} is already running."
    if current == "outside":
        return (f"Something already listens on port {service.port}, which {service.title} needs: probably "
                f"{service.title}, started outside m97. Stop it there first.")
    if service.container:
        if current is not None:
            logger.info(f"{service.container}: removing a leftover container (state: {current})")
            await _remove(service.container, run=run)
        code, _, err = await run(*_in_own_scope("podman", "run", *service.run_args(lan)))
        if code != 0:
            return f"podman could not start {service.title}: {err.strip()}"
    else:
        command = service.run_args(lan)
        if not os.access(command[0], os.X_OK):
            return f"{service.title}: {command[0]} is not an executable file; check `command` in m97's settings."
        if current == "failed":
            await run("systemctl", "--user", "reset-failed", f"{service.unit}.service")
        code, _, err = await run("systemd-run", "--user", f"--unit={service.unit}", "--quiet",
                                 f"--description=m97: {service.title}", "--setenv=PYTHONUNBUFFERED=1",
                                 "--", *command)
        if code != 0:
            return f"systemd-run could not start {service.title}: {err.strip()}"
    t0 = clock()
    while clock() - t0 < service.start_timeout:
        if await is_ready_now():
            logger.info(f"{service.key}: ready after {clock() - t0:.0f} s")
            return None
        now = await state(service, run=run)
        if now != "running":
            tail = await log_tail(service, 20, run=run)
            return f"{service.title} stopped while starting (state: {now}).\n\nIts last log lines:\n{tail}"
        await asyncio.sleep(poll)
    where = "The container is still there" if service.container else "It is still running"
    return f"{service.title} did not answer within {service.start_timeout:.0f} s. {where}; its log may say why."


async def stop(service, *, run=run):
    """Stop `service`, whatever state it is in: remove its container, or stop its unit.

    Returns `None`, or an error message. A service started outside m97 is left alone: m97 could only kill a
    process it knows nothing about.
    """
    current = await state(service, run=run)
    if current == "outside":
        return f"{service.title} was started outside m97, so m97 does not stop it. Stop it where it was started."
    if current is None:
        return None
    if service.container:
        await _remove(service.container, run=run)
        current = await container_state(service.container, run=run)
        if current is not None:
            return (f"The {service.title} container is still there (state: {current}); "
                    f"try `podman rm -f {service.container}`.")
        return None
    unit = f"{service.unit}.service"
    # systemd sends SIGTERM, then SIGKILL after its stop timeout (90 s by default). A process that exits
    # non-zero on SIGTERM leaves the unit failed, which after a deliberate stop would read as a crash.
    await run("systemctl", "--user", "stop", unit, timeout=120)
    await run("systemctl", "--user", "reset-failed", unit)
    info = await unit_info(service.unit, run=run)
    if info["ActiveState"] not in ("inactive", "failed"):
        return f"{service.title} is still {info['ActiveState']}; try `systemctl --user kill {unit}`."
    return None


# --------------------------------------------------------------------------------
# Watching

# podman event statuses, and the container state each leaves behind. Statuses not listed (`exec`, `attach`,
# health checks ...) change nothing.
EVENT_STATES = {"create": "created", "init": "created", "start": "running", "restart": "running",
                "pause": "paused", "unpause": "running",
                "stop": "exited", "died": "exited", "cleanup": "exited", "remove": None}


async def follow_events(containers, on_state, *, run=run, retry=5.0):
    """Keep `on_state(name, state, event)` informed of each container's state, until cancelled.

    Calls it once per container at the start, from `podman container inspect`, with `event` `None`; and
    again whenever `podman events` reports a change, with `event` the decoded event (a `died` event carries
    `ContainerExitCode`). If the event stream ends, it is restarted and the states are read afresh, since
    events in the gap are lost. `state` is `None` for a container that does not exist.
    """
    names = list(containers)
    while True:
        args = ["podman", "events", "--format", "json", "--filter", "type=container"]
        for name in names:
            args += ["--filter", f"container={name}"]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            logger.warning(f"could not run podman events, retrying in {retry:.0f} s: {exc}")
            await asyncio.sleep(retry)
            continue
        try:
            for name in names:  # after the stream is open, so a change from here on is not missed
                on_state(name, await container_state(name, run=run), None)
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                name, status = event.get("Name"), event.get("Status")
                if name in names and status in EVENT_STATES:
                    on_state(name, EVENT_STATES[status], event)
            await proc.wait()
            logger.warning(f"podman events ended (exit {proc.returncode}), restarting in {retry:.0f} s")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.sleep(retry)


def derive_status(*, state, ready, action, ready_known=True):
    """The status the page shows for a service, from what is known about it.

    `state` is the service's state (see `state`; `None` if it has no container or unit), `ready` whether the
    service answers, `ready_known` whether that has been checked since it started running, and `action` the
    start or stop m97 is running for it, if any (`"start"`, `"stop"`, or `None`).
    Returns one of `starting`, `stopping`, `running`, `stopped`, `unknown`. A service running but not ready
    yet reads as `starting` even if m97 did not start it, e.g. after `gufo-serve start` in a shell; one whose
    readiness has not been checked yet, as just after m97 itself starts, reads as `unknown`. A service
    started outside m97 reads like one m97 started.
    """
    if action == "stop":
        return "stopping"
    if action == "start":
        return "starting"
    if state in ("running", "outside"):
        if not ready_known:
            return "unknown"
        return "running" if ready else "starting"
    return "stopped"


# --------------------------------------------------------------------------------
# Where a service listens

_TCP_LISTEN = "0A"
_ANY = {"00000000", "0" * 32}                                   # 0.0.0.0, ::
_LOOPBACK = {"0100007F", "00000000000000000000000001000000"}   # 127.0.0.1, ::1 (as the kernel prints them)


def parse_proc_net_tcp(text, port):
    """The local addresses (kernel hex) listening on `port` in a /proc/net/tcp or tcp6 table."""
    addresses = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != _TCP_LISTEN:
            continue
        address, _, hex_port = fields[1].partition(":")
        try:
            if int(hex_port, 16) == port:
                addresses.append(address)
        except ValueError:
            continue
    return addresses


def listen_scope(port, *, root="/"):
    """Whether something listens on TCP `port`: `"all"` (all interfaces), `"local"` (loopback only), or `None`.

    Read from the kernel's socket tables, so it holds whoever started the service. The containers run with
    `--network host`, so their sockets are in the host's tables, like the units'.
    """
    addresses = []
    for table in ("tcp", "tcp6"):
        try:
            addresses += parse_proc_net_tcp(pathlib.Path(root, "proc/net", table).read_text(), port)
        except OSError:
            continue
    if any(a in _ANY for a in addresses):
        return "all"
    if addresses:
        return "local" if all(a in _LOOPBACK for a in addresses) else "all"
    return None


def service_url(service, scope):
    """The address people use to reach `service`, given `listen_scope`'s answer for its port, or `None`.

    On all interfaces it names this machine by hostname; on loopback only, `127.0.0.1`.
    """
    if not scope:
        return None
    host = socket.gethostname() if scope == "all" else "127.0.0.1"
    return f"http://{host}:{service.port}{service.url_path}"
