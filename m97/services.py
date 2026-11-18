"""Starting, stopping and watching the two services, gufo and Open WebUI, each in a podman container.

The containers are run detached and without `--rm`, so a container that dies while loading keeps its log
until the next start or stop removes it; that log is what the page shows as the error.
"""

import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request

__all__ = ["Service", "gufo_service", "openwebui_service",
           "gufo_run_args", "openwebui_run_args",
           "run", "container_state", "start", "stop",
           "follow_events", "EVENT_STATES",
           "derive_status"]

logger = logging.getLogger(__name__)

# The devices gufo's docs pass to the container: the GPU (`kfd`, `dri`) and the XDNA NPU (`accel0`).
GPU_DEVICES = ("/dev/kfd", "/dev/dri", "/dev/accel/accel0")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Service:
    """One service: how to run its container, and how to tell it is ready.

    `run_args(lan)` returns the arguments to `podman run`, with the service listening on all interfaces if
    `lan` is true. `ready_url` answers 200 once the service is up. `start_timeout` is in seconds.
    """
    key: str
    title: str
    container: str
    run_args: object       # callable: lan -> list of str
    ready_url: str
    start_timeout: float


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
            openwebui["image"]]


def gufo_service(settings):
    """The gufo `Service`, from the whole settings dict."""
    gufo = settings["gufo"]
    devices = [d for d in GPU_DEVICES if os.path.exists(d)]
    return Service(key="gufo", title="gufo", container=gufo["container"],
                   run_args=lambda lan: gufo_run_args(gufo, lan=lan, devices=devices,
                                                      uid=os.getuid(), gid=os.getgid()),
                   ready_url=f"http://127.0.0.1:{gufo['port']}/ready",
                   start_timeout=gufo["start_timeout"])


def openwebui_service(settings):
    """The Open WebUI `Service`, from the whole settings dict."""
    openwebui = settings["openwebui"]
    return Service(key="openwebui", title="Open WebUI", container=openwebui["container"],
                   run_args=lambda lan: openwebui_run_args(openwebui, settings["gufo"], lan=lan),
                   ready_url=f"http://127.0.0.1:{openwebui['port']}/health",
                   start_timeout=openwebui["start_timeout"])


# --------------------------------------------------------------------------------
# Running podman

async def run(*args, timeout=60):
    """Run a command, returning `(returncode, stdout, stderr)` as text. A timeout kills it and returns 124."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"{args[0]}: timed out after {timeout} s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def container_state(name, *, run=run):
    """The container's state (`running`, `exited`, `created`, ...), or `None` if there is no such container."""
    code, out, _ = await run("podman", "container", "inspect", "--format", "{{.State.Status}}", name)
    return (out.strip() or None) if code == 0 else None


async def _remove(name, *, run):
    await run("podman", "stop", "-t", "15", name, timeout=30)
    await run("podman", "rm", "-f", name, timeout=30)


def _ready(url):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


async def start(service, *, lan, run=run, ready=None, clock=time.monotonic, poll=2.0):
    """Start `service` and wait until it is ready. Returns `None` on success, or an error message.

    A leftover container in a non-running state is removed first. If the container dies during the wait,
    the message carries its last log lines; if the wait times out, the container is left running, since
    it may only be slow (a cold start reads the whole model from disk).
    """
    is_ready = ready or (lambda: asyncio.to_thread(_ready, service.ready_url))
    state = await container_state(service.container, run=run)
    if state == "running":
        return f"{service.title} is already running."
    if state is not None:
        logger.info(f"{service.container}: removing a leftover container (state: {state})")
        await _remove(service.container, run=run)
    code, _, err = await run("podman", "run", *service.run_args(lan))
    if code != 0:
        return f"podman could not start {service.title}: {err.strip()}"
    t0 = clock()
    while clock() - t0 < service.start_timeout:
        if await is_ready():
            logger.info(f"{service.container}: ready after {clock() - t0:.0f} s")
            return None
        state = await container_state(service.container, run=run)
        if state != "running":
            _, out, err = await run("podman", "logs", "--tail", "20", service.container)
            tail = (out + err).strip()
            return f"{service.title} stopped while starting (state: {state}). Its last log lines:\n{tail}"
        await asyncio.sleep(poll)
    return (f"{service.title} did not answer within {service.start_timeout:.0f} s. "
            f"The container is still there; its log may say why.")


async def stop(service, *, run=run):
    """Stop and remove `service`'s container, whatever state it is in. Returns `None`, or an error message."""
    if await container_state(service.container, run=run) is None:
        return None
    await _remove(service.container, run=run)
    state = await container_state(service.container, run=run)
    if state is not None:
        return f"The {service.title} container is still there (state: {state}); try `podman rm -f {service.container}`."
    return None


# --------------------------------------------------------------------------------
# Watching

# podman event statuses, and the container state each leaves behind. Statuses not listed (`exec`, `attach`,
# health checks ...) change nothing.
EVENT_STATES = {"create": "created", "init": "created", "start": "running", "restart": "running",
                "pause": "paused", "unpause": "running",
                "stop": "exited", "died": "exited", "cleanup": "exited", "remove": None}


async def follow_events(containers, on_state, *, run=run, retry=5.0):
    """Keep `on_state(name, state)` informed of each container's state, until cancelled.

    Calls it once per container at the start, from `podman container inspect`, and again whenever
    `podman events` reports a change. If the event stream ends, it is restarted and the states are read
    afresh, since events in the gap are lost. `state` is `None` for a container that does not exist.
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
                on_state(name, await container_state(name, run=run))
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                name, status = event.get("Name"), event.get("Status")
                if name in names and status in EVENT_STATES:
                    on_state(name, EVENT_STATES[status])
            await proc.wait()
            logger.warning(f"podman events ended (exit {proc.returncode}), restarting in {retry:.0f} s")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.sleep(retry)


def derive_status(*, container, ready, action):
    """The status the page shows for a service, from what is known about it.

    `container` is the container state (`None` if there is none), `ready` whether the service answers,
    `action` the start or stop m97 is running for it, if any (`"start"`, `"stop"`, or `None`).
    Returns one of `starting`, `stopping`, `running`, `stopped`. A container running but not ready yet
    reads as `starting` even if m97 did not start it, e.g. after `gufo-serve start` in a shell.
    """
    if action == "stop":
        return "stopping"
    if action == "start":
        return "starting"
    if container == "running":
        return "running" if ready else "starting"
    return "stopped"
