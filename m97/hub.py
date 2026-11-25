"""The server's one view of the machine, shared by every open page.

A single set of background tasks keeps it current: the sensor sampler, gufo's HTTP endpoints (polled only
while its container runs), gufo's log (for the recent requests), and podman's container events. The sampler
also keeps the per-day usage tally (see `usage`), and watches the ports of the services run as systemd
units, asking systemd about a unit when its port opens or closes. Pages only
read from it, so a second open tab costs a render, not a second round of sampling.
"""

import asyncio
import collections
import datetime
import json
import logging
import pathlib
import time
import urllib.error
import urllib.request

from . import access, gufo, intent, sensors, services, usage

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

    def __init__(self, settings, *, state_path=None, usage_path=None):
        self.settings = settings
        self.services = {s.key: s for s in services.configured_services(settings)}
        self._by_container = {s.container: s.key for s in self.services.values() if s.container}
        self._sample = sensors.make_sampler()
        self.cpu_cores = sensors.cpu_cores()   # CPU numbers grouped by physical core

        interval = settings["history"]["interval"]
        self.interval = interval
        length = max(2, int(settings["history"]["seconds"] / interval))
        self.history = collections.deque(maxlen=length)   # (monotonic time, {series: value})
        self.latest = {}                                  # the newest sensor sample

        self.state = {key: None for key in self.services}       # per service, see `services.state`
        # For a unit service, the unit's `ActiveState` as last asked, and whether its port was open at the last
        # sample (`None` before the first), so that systemd is asked only when that changes.
        self._unit_active = {key: None for key, s in self.services.items() if s.unit}
        self._listening = {key: None for key in self._unit_active}
        self.ready = {key: False for key in self.services}
        self.ready_known = {key: False for key in self.services}   # `ready` checked since the container started
        self.scope = {key: None for key in self.services}       # "all", "local" or None: where it listens
        self.action = {key: None for key in self.services}      # "start" / "stop" while m97 runs one
        self.error = {key: "" for key in self.services}         # why the last start or stop failed
        self._action_tasks = {}                                  # key -> the asyncio task of its action
        self._restarting = set()                                 # keys being restarted: wanted running throughout
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

        # How the machine's time is spent, per day; beside the state file unless told otherwise.
        self._usage_path = pathlib.Path(usage_path or pathlib.Path(self._state_path).with_name("usage.json"))
        self.control_token = None   # written by `start`, for `POST /api/services` (see `access.write_token`)
        self.usage = usage.load_usage(self._usage_path)
        self._usage_t = None          # monotonic time of the last sample counted
        self._usage_saved_t = time.monotonic()

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
        """Write the control token, and start the background tasks. Call from inside the running event loop."""
        path = access.token_path(self._state_path)
        try:
            self.control_token = access.write_token(path)
        except OSError as exc:
            logger.warning(f"{path}: could not write the token, so `m97 gufo`, `m97 openwebui` and the like will be "
                           f"refused: {exc}")
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
        self._save_usage()

    # ----------------------------------------------------------------------------
    # Status

    def status(self, key):
        """`starting`, `stopping`, `running`, `stopped` or `unknown`; see `services.derive_status`."""
        return services.derive_status(state=self.state[key], ready=self.ready[key], action=self.action[key],
                                      ready_known=self.ready_known[key])

    async def decision(self):
        """Whether the buttons are open, asking NetworkManager afresh; also updates `self.access`.

        For a button press. The pages show `self.access`, which `nmcli monitor` keeps current.
        """
        self.access = access.decide(self.settings["access"], await asyncio.to_thread(access.active_connections))
        return self.access

    # ----------------------------------------------------------------------------
    # Actions

    def start_service(self, key):
        """Start a service in the background, returning the task; or `None`, doing nothing, while a start,
        stop or restart of it is under way."""
        if self.action[key] is not None or key in self._restarting:
            return None
        task = self._action_tasks[key] = asyncio.create_task(self._act(key, "start"))
        return task

    def stop_service(self, key):
        """Stop a service in the background, returning the task. A start or restart under way is cancelled
        first, its container removed; a stop under way is returned as it is."""
        if self.action[key] == "stop" and key not in self._restarting:
            return self._action_tasks[key]
        task = self._action_tasks[key] = asyncio.create_task(self._stop_after_cancelling(key, self._action_tasks.get(key)))
        return task

    def restart_service(self, key):
        """Stop a service and start it again, in the background, returning the task. A start or stop under way
        is cancelled first; a restart under way is returned as it is. The service counts as wanted running
        throughout, so if m97 stops in between, autostart still brings it back."""
        if key in self._restarting:
            return self._action_tasks[key]
        self._restarting.add(key)
        task = self._action_tasks[key] = asyncio.create_task(self._restart(key, self._action_tasks.get(key)))
        return task

    async def _stop_after_cancelling(self, key, maybe_task):
        await _cancel(maybe_task)
        await self._act(key, "stop")

    async def _restart(self, key, maybe_task):
        try:
            await _cancel(maybe_task)
            await self._act(key, "stop")
            if not self.error[key]:
                await self._act(key, "start")
        finally:
            self._restarting.discard(key)

    async def run_action(self, key, verb):
        """Start, stop or restart a service (`verb`), and wait until that is done; for `POST /api/services`.

        Returns `(status, error)`, `error` being `""` on success; or `None` for a start refused because
        another action is under way. A start of a service already running succeeds at once, doing nothing.
        If the caller is cancelled, the action goes on.
        """
        # The page disables Start while a service runs. Without this, `services.start` would refuse with an error,
        # and `_act` would keep that like a crash report, shown if the service is stopped later.
        if verb == "start" and self.status(key) == "running":
            return "running", ""
        task = {"start": self.start_service, "stop": self.stop_service, "restart": self.restart_service}[verb](key)
        if task is None:
            return None
        # A later stop may cancel this action; then its own outcome is what the status and error show.
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        return self.status(key), self.error[key]

    async def _act(self, key, verb):
        service = self.services[key]
        if verb == "stop" and self.state[key] == "outside":
            # `services.stop` refuses this one, and with no stop under way, nothing records it as wanted stopped.
            self.error[key] = await services.stop(service) or ""
            return
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
            if service.unit:
                # Before `action` is cleared: no event says a unit changed, and until this is read, the state
                # would be computed from the unit's state before the action.
                await self._refresh_unit(key)
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
        self.state[key] = state
        if state != "running":
            self.ready[key] = self.ready_known[key] = False
        if key == "gufo":
            self._follow_gufo_log(state == "running")
        if event is not None and event.get("Status") == "died" and self.action[key] is None:
            asyncio.create_task(self._check_crash(key, event.get("ContainerExitCode")))

    async def _check_crash(self, key, maybe_exit_code, *, settle=10.0):
        """Report a container that died by itself, as opposed to being stopped.

        A deliberate stop, by m97 or by a shell script, removes the container within a second or so of its
        death; a crash leaves it behind, exited. So wait a little, then look.
        """
        await asyncio.sleep(settle)
        state = await services.container_state(self.services[key].container)
        if state is None or state == "running" or self.action[key] is not None:
            return
        await self._report_crash(key, None if maybe_exit_code is None else f"exit code {maybe_exit_code}")

    async def _refresh_unit(self, key, *, settle=2.0):
        """Ask systemd for a unit service's state, and report a crash if the unit has just failed by itself.

        Called when the service's port opens or closes, at m97's start, and after m97 starts or stops it.
        A deliberate stop leaves the unit inactive (m97 resets one that failed while stopping); a crash leaves
        it failed.
        """
        service = self.services[key]
        info = await services.unit_info(service.unit)
        if info["ActiveState"] in ("active", "deactivating") and not self._listening[key]:
            # The port closes a moment before systemd has seen the process exit.
            await asyncio.sleep(settle)
            info = await services.unit_info(service.unit)
        was, self._unit_active[key] = self._unit_active[key], info["ActiveState"]
        self.state[key] = services.unit_status(info["ActiveState"], listening=bool(self._listening[key]))
        # `was` is `None` at the first look, and a unit that failed while m97 was not running is not news.
        if info["ActiveState"] == "failed" and was not in (None, "failed") and self.action[key] is None:
            # `ExecMainStatus` is the exit code only when the process exited; killed, it is the signal.
            how = (f"exit code {info['ExecMainStatus']}" if info["Result"] == "exit-code"
                   else info["Result"] or None)
            await self._report_crash(key, how)

    async def _report_crash(self, key, maybe_how):
        """Keep and show a crash report: `maybe_how` is a few words on how it ended, such as an exit code."""
        service = self.services[key]
        tail = await services.log_tail(service, 15)
        when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        code = f" ({maybe_how})" if maybe_how else ""
        # A blank line separates what to read from the details; the page folds the details away.
        message = (f"{service.title} stopped unexpectedly at {when}{code}. m97 does not restart it by itself, "
                   f"and will not start it at boot until it has run again. Press Start to try again; if it keeps "
                   f"stopping, `{services.log_hint(service)}` on the server has the whole log.\n\n"
                   f"Its last log lines:\n{tail}")
        logger.warning(f"{service.key}: stopped unexpectedly{code}")
        self._crash[key] = message
        self.error[key] = message

    def _remember(self, now):
        """Update what should be running from what is seen, and save it when it changes."""
        changed = False
        for key in self.services:
            action = ("start" if key in self._restarting or key in self._autostart_pending
                      else self.action[key])
            want, self._down_since[key] = intent.track(self.want[key], self._down_since[key],
                                                       running=self.state[key] in ("running", "outside"),
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
        # A service whose `after` names another being started waits until that start has finished: Open WebUI
        # is a front end to gufo, and raven-mcp a front end to Raven-server.
        started = {}

        async def bring_up(key):
            before = [other for other in self.services[key].after if other in started]
            if before:
                self.note[key] = f"autostart: waiting for {', '.join(self.services[k].title for k in before)}"
                await asyncio.gather(*(started[k] for k in before), return_exceptions=True)
            if await services.state(self.services[key]) in ("running", "outside"):
                self._autostart_pending.discard(key)
                self.note[key] = ""
                return
            logger.info(f"{key}: was running when m97 last ran; starting it")
            self.note[key] = "autostart"
            task = self.start_service(key)
            self._autostart_pending.discard(key)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

        for key in targets:
            started[key] = asyncio.create_task(bring_up(key))
        await asyncio.gather(*started.values(), return_exceptions=True)

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
            if service.unit:
                # A unit's port is read every sample, since its opening and closing are the only events there
                # are: systemd signals only clients that have subscribed over D-Bus.
                self.scope[key] = await asyncio.to_thread(services.listen_scope, service.port)
                listening = self.scope[key] is not None
                if listening != self._listening[key]:
                    self._listening[key] = listening
                    await self._refresh_unit(key)
                self.state[key] = services.unit_status(self._unit_active[key], listening=listening)
            else:
                self.scope[key] = (await asyncio.to_thread(services.listen_scope, service.port)
                                   if self.state[key] == "running" else None)
            if self.state[key] in ("running", "outside"):
                if key == "gufo":
                    await self._poll_gufo(now)
                else:
                    self.ready[key] = await asyncio.to_thread(services.is_ready, service)
                self.ready_known[key] = True
            else:
                self.ready[key] = self.ready_known[key] = False
        if self.state["gufo"] != "running" or not self.ready["gufo"]:
            self.slots, self.metrics, self.gen_tps, self._previous_tokens = [], {}, None, None
            self._previous_slots = {}
        self._remember(now)
        self._count_usage(now)
        self.history.append((now, {"ppt_w": self.latest.get("ppt_w"),
                                   "gpu_busy_pct": self.latest.get("gpu_busy_pct"),
                                   "cpu_busy_pct": self.latest.get("cpu_busy_pct"),
                                   "gen_tps": self.gen_tps}))

    def _count_usage(self, now, *, save_every=60.0):
        """Add the time since the previous sample to today's tally, and save the tally once a minute."""
        if self._usage_t is not None:
            what = usage.category(container=self.state["gufo"], ready=self.ready["gufo"],
                                  ready_known=self.ready_known["gufo"], slots=self.slots)
            date = datetime.date.today().isoformat()
            self.usage[date] = usage.account(self.usage.get(date), category=what, interval=now - self._usage_t,
                                             maybe_ppt_w=self.latest.get("ppt_w"),
                                             maybe_gpu_busy_pct=self.latest.get("gpu_busy_pct"))
        self._usage_t = now
        if now - self._usage_saved_t >= save_every:
            self._save_usage()
            self._usage_saved_t = now

    def _save_usage(self):
        if not self.usage:
            return
        try:
            usage.save_usage(self._usage_path, self.usage)
        except OSError as exc:
            logger.warning(f"{self._usage_path}: could not save the usage tally: {exc}")

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


async def _cancel(maybe_task):
    """Cancel an action task, if there is one still running, and wait until it has wound down."""
    if maybe_task is not None and not maybe_task.done():
        maybe_task.cancel()
        await asyncio.gather(maybe_task, return_exceptions=True)


def _json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
