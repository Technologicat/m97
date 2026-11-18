"""The server's one view of the machine, shared by every open page.

A single set of background tasks keeps it current: the sensor sampler, gufo's HTTP endpoints (polled only
while its container runs), gufo's log (for the recent requests), and podman's container events. Pages only
read from it, so a second open tab costs a render, not a second round of sampling.
"""

import asyncio
import collections
import json
import logging
import time
import urllib.error
import urllib.request

from . import access, gufo, sensors, services

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

    def __init__(self, settings):
        self.settings = settings
        self.services = {s.key: s for s in (services.gufo_service(settings), services.openwebui_service(settings))}
        self._by_container = {s.container: s.key for s in self.services.values()}
        self._sample = sensors.make_sampler()

        interval = settings["history"]["interval"]
        self.interval = interval
        length = max(2, int(settings["history"]["seconds"] / interval))
        self.history = collections.deque(maxlen=length)   # (monotonic time, {series: value})
        self.latest = {}                                  # the newest sensor sample

        self.container = {key: None for key in self.services}   # container state per service
        self.ready = {key: False for key in self.services}
        self.action = {key: None for key in self.services}      # "start" / "stop" while m97 runs one
        self.error = {key: "" for key in self.services}         # why the last start or stop failed
        self._action_tasks = {}                                  # key -> the asyncio task of its action

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
        self.error[key] = error or ""

    # ----------------------------------------------------------------------------
    # Background tasks

    def _on_container(self, name, state):
        key = self._by_container.get(name)
        if key is None:
            return
        self.container[key] = state
        if state != "running":
            self.ready[key] = False
        if key == "gufo":
            self._follow_gufo_log(state == "running")

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
