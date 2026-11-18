"""The server's one view of the machine, shared by every open page.

A single set of background tasks keeps it current: the sensor sampler, gufo's HTTP endpoints (polled only
while its container runs), gufo's log (for the recent requests), and podman's container events. Pages only
read from it, so a second open tab costs a render, not a second round of sampling.
"""

import asyncio
import collections
import datetime
import json
import logging
import time
import urllib.error
import urllib.request

from . import access, gufo, intent, sensors, services

__all__ = ["Hub"]

logger = logging.getLogger(__name__)


def _fetch(url, *, timeout=1.0):
    """GET `url`, returning the body as text, or `None` if it does not answer with 200."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace") if response.status == 200 else None
    except (OSError, urllib.error.URLError, ValueError):
        return None


class Hub:
    """Shared state: the latest readings, a few minutes of history, and each service's status.

    Read by the pages; written only by the hub's own tasks and by `start_service` / `stop_service`.
    Everything runs on the one asyncio event loop, so no locking is needed: a page reads between two awaits
    of the hub's tasks, never in the middle of an update.
    """

    def __init__(self, settings, *, state_path=None):
        self.settings = settings
        self.services = {s.key: s for s in (services.gufo_service(settings), services.openwebui_service(settings))}
        self._by_container = {s.container: s.key for s in self.services.values()}
        self._sample = sensors.make_sampler()
        self.cpu_cores = sensors.cpu_cores()   # CPU numbers grouped by physical core

        interval = settings["history"]["interval"]
        self.interval = interval
        length = max(2, int(settings["history"]["seconds"] / interval))
        self.history = collections.deque(maxlen=length)   # (monotonic time, {series: value})
        self.latest = {}                                  # the newest sensor sample

        self.container = {key: None for key in self.services}   # container state per service
        self.ready = {key: False for key in self.services}
        self.scope = {key: None for key in self.services}       # "all", "local" or None: where it listens
        self.action = {key: None for key in self.services}      # "start" / "stop" while m97 runs one
        self.error = {key: "" for key in self.services}         # why the last start or stop failed
        self._action_tasks = {}                                  # key -> the asyncio task of its action
        self.note = {key: "" for key in self.services}          # e.g. "autostart", shown beside the status

        # What should be running, remembered across restarts (see `intent`), and why a service that should
        # have been running is not: a crash report, kept until the service runs again.
        self._state_path = state_path or intent.default_state_path()
        state = intent.load_state(self._state_path)
        self.want = {key: state["want"].get(key) for key in self.services}
        self.problem = {key: state["problem"].get(key, "") for key in self.services}
        self._down_since = {key: None for key in self.services}
        self._crash = {key: "" for key in self.services}          # a crash seen live, not yet remembered
        self._autostart_pending = set()   # keys waiting for the network, to be started; count as starting

        self.props = None           # gufo's /props, read once each time it becomes ready
        self.slots = []             # parsed /slots
        self.metrics = {}           # parsed /metrics
        self.gen_tps = None         # total generation throughput over the last interval, tokens/s
        self.requests = collections.deque(maxlen=20)   # recent finished chat requests, newest last

        self.access = access.decide(settings["access"], [])   # replaced as soon as the network is read
        self._previous_tokens = None   # (monotonic time, tokens_predicted_total)
        self._previous_slots = {}      # session id -> (monotonic time, tokens decoded so far)
        self._tasks = []
        self._log_task = None
        self.pin_allowed, self.pin_failed = access.make_rate_limiter()

    # ----------------------------------------------------------------------------
    # Lifecycle

    def start(self):
        """Start the background tasks. Call from inside the running event loop."""
        self._tasks = [asyncio.create_task(self._sampling_loop()),
                       asyncio.create_task(services.follow_events(list(self._by_container), self._on_container)),
                       asyncio.create_task(self._network_loop())]
        if self.settings["server"]["autostart"]:
            self._tasks.append(asyncio.create_task(self._autostart()))

    async def stop(self):
        tasks = self._tasks + ([self._log_task] if self._log_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ----------------------------------------------------------------------------
    # Status

    def status(self, key):
        """`starting`, `stopping`, `running` or `stopped`; see `services.derive_status`."""
        return services.derive_status(container=self.container[key], ready=self.ready[key], action=self.action[key])

    async def decision(self):
        """Whether the buttons are open, asking NetworkManager afresh; also updates `self.access`.

        For a button press. The pages show `self.access`, which `nmcli monitor` keeps current.
        """
        self.access = access.decide(self.settings["access"], await asyncio.to_thread(access.active_connections))
        return self.access

    # ----------------------------------------------------------------------------
    # Actions

    def start_service(self, key):
        """Start a service in the background. Does nothing while a start or stop of it is under way."""
        if self.action[key] is None:
            self._action_tasks[key] = asyncio.create_task(self._act(key, "start"))

    def stop_service(self, key):
        """Stop a service in the background. A start under way is cancelled first, its container removed."""
        if self.action[key] == "stop":
            return
        self._action_tasks[key] = asyncio.create_task(self._stop_after_cancelling(key, self._action_tasks.get(key)))

    async def _stop_after_cancelling(self, key, maybe_start):
        if maybe_start is not None and not maybe_start.done():
            maybe_start.cancel()
            await asyncio.gather(maybe_start, return_exceptions=True)
        await self._act(key, "stop")

    async def _act(self, key, verb):
        service = self.services[key]
        self.action[key], self.error[key] = verb, ""
        error = None
        try:
            if verb == "start":
                # On a trusted network the services serve the LAN; elsewhere they stay on this machine,
                # whoever pressed the button.
                trusted = await self.decision()
                error = await services.start(service, lan=trusted.allowed)
            else:
                error = await services.stop(service)
        except asyncio.CancelledError:
            raise  # a stop took over; it clears `action` itself
        except Exception as exc:  # the page must hear about it, whatever it was
            logger.exception(f"{verb} {key} failed")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if self.action[key] == verb:
                self.action[key] = None
                self.note[key] = ""
        self.error[key] = error or ""
        if verb == "start" and error:
            self._crash[key] = error   # remembered like a crash: it will not be autostarted until it has run

    # ----------------------------------------------------------------------------
    # Background tasks

    def _on_container(self, name, state, event):
        key = self._by_container.get(name)
        if key is None:
            return
        self.container[key] = state
        if state != "running":
            self.ready[key] = False
        if key == "gufo":
            self._follow_gufo_log(state == "running")
        if event is not None and event.get("Status") == "died" and self.action[key] is None:
            asyncio.create_task(self._check_crash(key, event.get("ContainerExitCode")))

    async def _check_crash(self, key, maybe_exit_code, *, settle=10.0):
        """Report a service that died by itself, as opposed to being stopped.

        A deliberate stop, by m97 or by a shell script, removes the container within a second or so of its
        death; a crash leaves it behind, exited. So wait a little, then look.
        """
        await asyncio.sleep(settle)
        service = self.services[key]
        state = await services.container_state(service.container)
        if state is None or state == "running" or self.action[key] is not None:
            return
        _, out, err = await services.run("podman", "logs", "--tail", "15", service.container)
        when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        code = f" (exit code {maybe_exit_code})" if maybe_exit_code is not None else ""
        # A blank line separates what to read from the details; the page folds the details away.
        message = (f"{service.title} stopped unexpectedly at {when}{code}. m97 does not restart it by itself, "
                   f"and will not start it at boot until it has run again. Press Start to try again; if it keeps "
                   f"stopping, `podman logs {service.container}` on the server has the whole log.\n\n"
                   f"Its last log lines:\n{(out + err).strip()}")
        logger.warning(f"{service.container}: stopped unexpectedly{code}")
        self._crash[key] = message
        self.error[key] = message

    def _remember(self, now):
        """Update what should be running from what is seen, and save it when it changes."""
        changed = False
        for key in self.services:
            action = self.action[key] or ("start" if key in self._autostart_pending else None)
            want, self._down_since[key] = intent.track(self.want[key], self._down_since[key],
                                                       running=self.container[key] == "running",
                                                       action=action, now=now)
            if want == self.want[key]:
                continue
            self.want[key], changed = want, True
            if want == "running":
                self.problem[key] = self._crash[key] = ""
            elif self._crash[key]:
                self.problem[key] = self._crash[key]
        if changed:
            try:
                intent.save_state(self._state_path, {"want": {k: v for k, v in self.want.items() if v},
                                                     "problem": {k: v for k, v in self.problem.items() if v}})
            except OSError as exc:
                logger.warning(f"{self._state_path}: could not save which services should run: {exc}")

    async def _autostart(self, *, network_timeout=120.0, poll=2.0):
        """Start the services that were running when m97 last ran, once the network is up.

        Waiting matters because the network decides where they listen: started before NetworkManager has
        connected, there is no trusted network yet, and gufo would serve this machine only.
        """
        targets = [key for key, want in self.want.items() if want == "running"]
        if not targets:
            return
        for key in targets:
            self.note[key] = "autostart: waiting for the network"
        self._autostart_pending.update(targets)
        deadline = time.monotonic() + network_timeout
        while time.monotonic() < deadline:
            connections = await asyncio.to_thread(access.active_connections)
            if any(not address.ip.is_loopback for _, addresses in connections for address in addresses):
                break
            await asyncio.sleep(poll)
        else:
            logger.warning(f"no network after {network_timeout:.0f} s; starting anyway")
        # gufo first, and the rest once its start has finished: Open WebUI is a front end to it.
        for key in sorted(targets, key=lambda k: k != "gufo"):
            if await services.container_state(self.services[key].container) == "running":
                self._autostart_pending.discard(key)
                self.note[key] = ""
                continue
            logger.info(f"{key}: was running when m97 last ran; starting it")
            self.note[key] = "autostart"
            self.start_service(key)
            self._autostart_pending.discard(key)
            if key == "gufo" and len(targets) > 1:
                for other in targets:
                    if other != key:
                        self.note[other] = "autostart: waiting for gufo"
                await asyncio.gather(self._action_tasks[key], return_exceptions=True)

    def _follow_gufo_log(self, running):
        if running and (self._log_task is None or self._log_task.done()):
            self._log_task = asyncio.create_task(self._gufo_log_loop())
        elif not running and self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None

    async def _gufo_log_loop(self):
        """Follow gufo's log while its container runs, keeping the finished chat requests."""
        name = self.services["gufo"].container
        proc = await asyncio.create_subprocess_exec("podman", "logs", "-f", "--tail", "200", name,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            async for raw in proc.stdout:
                record = gufo.completed_request(raw.decode("utf-8", errors="replace"))
                if record is not None:
                    self.requests.append(record)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def _network_loop(self, *, retry=30.0):
        """Keep `self.access` current: re-read the network whenever `nmcli monitor` reports a change."""
        while True:
            await self.decision()
            try:
                proc = await asyncio.create_subprocess_exec("nmcli", "monitor", stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.DEVNULL)
            except OSError as exc:
                logger.warning(f"could not run nmcli monitor, so network changes are noticed only at a button "
                               f"press: {exc}")
                return
            try:
                async for _ in proc.stdout:
                    # A change arrives as a burst of lines (device, connection, connectivity); let it settle.
                    await asyncio.sleep(1.0)
                    await self.decision()
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(retry)

    async def _sampling_loop(self):
        next_t = time.monotonic()
        while True:
            try:
                await self._tick()
            except Exception:  # one bad tick must not stop the page from updating
                logger.exception("sampling failed")
            next_t += self.interval
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def _tick(self):
        now = time.monotonic()
        self.latest = await asyncio.to_thread(self._sample)
        for key, service in self.services.items():
            self.scope[key] = (await asyncio.to_thread(services.listen_scope, service.port)
                               if self.container[key] == "running" else None)
            if self.container[key] == "running":
                if key == "gufo":
                    await self._poll_gufo(now)
                else:
                    self.ready[key] = await asyncio.to_thread(services.answers, service.ready_url)
            else:
                self.ready[key] = False
        if self.container["gufo"] != "running" or not self.ready["gufo"]:
            self.slots, self.metrics, self.gen_tps, self._previous_tokens = [], {}, None, None
            self._previous_slots = {}
        self._remember(now)
        self.history.append((now, {"ppt_w": self.latest.get("ppt_w"),
                                   "gpu_busy_pct": self.latest.get("gpu_busy_pct"),
                                   "cpu_busy_pct": self.latest.get("cpu_busy_pct"),
                                   "gen_tps": self.gen_tps}))

    async def _poll_gufo(self, now):
        port = self.settings["gufo"]["port"]
        base = f"http://127.0.0.1:{port}"
        was_ready = self.ready["gufo"]
        self.ready["gufo"] = await asyncio.to_thread(services.answers, f"{base}/ready")
        if not self.ready["gufo"]:
            self.props = None
            return
        if not was_ready or self.props is None:
            self.props = _json(await asyncio.to_thread(_fetch, f"{base}/props"))
        slots = gufo.parse_slots(_json(await asyncio.to_thread(_fetch, f"{base}/slots")))
        self.slots, self._previous_slots = gufo.with_slot_rates(slots, self._previous_slots, now)
        text = await asyncio.to_thread(_fetch, f"{base}/metrics")
        self.metrics = gufo.parse_metrics(text) if text else {}
        tokens = self.metrics.get("llamacpp:tokens_predicted_total")
        self.gen_tps = None
        if tokens is not None:
            if self._previous_tokens is not None:
                t0, tokens0 = self._previous_tokens
                if now > t0 and tokens >= tokens0:   # a restart resets the counter
                    self.gen_tps = (tokens - tokens0) / (now - t0)
            self._previous_tokens = (now, tokens)


def _json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
