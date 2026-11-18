"""The page: what the machine and its LLM server are doing, and buttons to start and stop the services."""

import datetime
import socket
import time

from nicegui import app, ui

from . import access

__all__ = ["build"]

STATUS_COLORS = {"running": "positive", "starting": "warning", "stopping": "warning", "stopped": "grey"}


def _fmt(value, spec, unit=""):
    return "–" if value is None else f"{value:{spec}}{unit}"


def _spark(name, color, *, max_value=None):
    """ECharts options for a small line chart with no axes, labels or legend."""
    return {"animation": False,
            "grid": {"left": 2, "right": 2, "top": 4, "bottom": 2},
            "xAxis": {"type": "value", "show": False},
            "yAxis": {"type": "value", "show": False, "min": 0, **({"max": max_value} if max_value else {})},
            "tooltip": {"trigger": "axis", "valueFormatter": None},
            "series": [{"name": name, "type": "line", "showSymbol": False, "data": [],
                        "lineStyle": {"width": 1.5, "color": color},
                        "areaStyle": {"color": color, "opacity": 0.15}}]}


def build(hub):
    """Register the page, reading everything it shows from `hub`."""

    @ui.page("/")
    async def index(client):
        ui.page_title(f"m97 · {socket.gethostname()}")
        dark = ui.dark_mode(True)
        # Quasar marks a disabled button only by fading it to 70 % opacity, which beside an enabled one reads as a
        # slightly different shade of the same button. Grey it out instead.
        ui.add_css(".q-btn.disabled { opacity: 0.3 !important; filter: grayscale(1); }")

        # The viewer's choice of dark or light is kept in their browser, which may refuse storage (a private
        # window, blocked site data); then the page simply opens dark.
        def toggle_dark():
            dark.set_value(not dark.value)
            ui.run_javascript(f"try {{ localStorage.setItem('m97-dark', '{'1' if dark.value else '0'}') }} catch (e) {{}}")
        unlocked = {"by_pin": False}   # per page: a right PIN opens the buttons until the page is reloaded

        with ui.header().classes("items-center justify-between px-4 py-2"):
            ui.label(f"m97 · {socket.gethostname()}").classes("text-lg font-medium")
            with ui.row().classes("items-center gap-2"):
                clock_label = ui.label().classes("text-sm opacity-80")
                ui.button(icon="contrast", on_click=toggle_dark).props(
                    "flat round dense color=white").tooltip("Dark or light")

        with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):

            # ---- Services
            with ui.card().classes("w-full"):
                ui.label("Services").classes("text-base font-medium")
                rows = {}
                for key, service in hub.services.items():
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.label(service.title).classes("w-28 font-medium")
                        badge = ui.badge("…")
                        spinner = ui.spinner(size="1.2em")
                        detail = ui.label().classes("text-sm opacity-70")
                        address = ui.link(new_tab=True).classes("text-sm font-mono")
                        address_plain = ui.label().classes("text-sm font-mono opacity-80")
                        scope = ui.label().classes("text-sm opacity-70")
                        ui.space()
                        start_button = ui.button("Start", on_click=lambda k=key: act(k, "start")).props("unelevated")
                        stop_button = ui.button("Stop", on_click=lambda k=key: act(k, "stop"),
                                                color="negative").props("outline")
                    error = ui.label().classes("text-sm text-negative whitespace-pre-wrap")
                    rows[key] = (badge, spinner, detail, (address, address_plain, scope), start_button, stop_button,
                                 error)
                with ui.row().classes("w-full items-center gap-3"):
                    lock_note = ui.label().classes("text-sm opacity-70")
                    pin_input = ui.input("PIN", password=True).props("dense outlined").classes("w-32")

            # ---- Model, speed, power, load, memory
            with ui.grid(columns="repeat(auto-fit, minmax(17rem, 1fr))").classes("w-full gap-4"):
                with ui.card():
                    ui.label("Model").classes("text-base font-medium")
                    model_label = ui.label().classes("text-xl")
                    model_detail = ui.label().classes("text-sm opacity-70")
                    sessions_row = ui.row().classes("w-full gap-1 mt-2")
                    session_bars = []   # (bar, tooltip) per session, rebuilt when the count changes
                    sessions_label = ui.label().classes("text-sm opacity-70")

                with ui.card():
                    ui.label("Generation").classes("text-base font-medium")
                    speed_label = ui.label().classes("text-xl")
                    speed_chart = ui.echart(_spark("tok/s", "#7c6cf0")).classes("w-full h-16")
                    live_label = ui.label().classes("text-sm")
                    last_label = ui.label().classes("text-sm opacity-70")

                with ui.card():
                    ui.label("Power and temperatures").classes("text-base font-medium")
                    power_label = ui.label().classes("text-xl")
                    power_chart = ui.echart(_spark("W", "#e8833a")).classes("w-full h-16")
                    temp_label = ui.label().classes("text-sm opacity-70")

                with ui.card():
                    ui.label("GPU and CPU").classes("text-base font-medium")
                    load_label = ui.label().classes("text-xl")
                    load_chart = ui.echart(_spark("GPU %", "#2f9e72", max_value=100)).classes("w-full h-16")
                    cpu_label = ui.label().classes("text-sm opacity-70")

                with ui.card():
                    ui.label("Memory").classes("text-base font-medium")
                    ram_label = ui.label().classes("text-sm")
                    ram_bar = ui.linear_progress(show_value=False).props("rounded size=10px")
                    gtt_label = ui.label().classes("text-sm mt-2")
                    gtt_bar = ui.linear_progress(show_value=False, color="secondary").props("rounded size=10px")

            # ---- Recent requests
            with ui.card().classes("w-full"):
                ui.label("Recent requests").classes("text-base font-medium")
                columns = [{"name": "time", "label": "Time", "field": "time", "align": "left"},
                           {"name": "tokens", "label": "Prompt → generated", "field": "tokens", "align": "right"},
                           {"name": "decode", "label": "Generation", "field": "decode", "align": "right"},
                           {"name": "prefill", "label": "Prefill", "field": "prefill", "align": "right"},
                           {"name": "ttft", "label": "First token", "field": "ttft", "align": "right"},
                           {"name": "duration", "label": "Duration", "field": "duration", "align": "right"},
                           {"name": "finish", "label": "Finish", "field": "finish", "align": "left"}]
                table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense").classes("w-full")
                no_requests = ui.label("No finished requests yet.").classes("text-sm opacity-70")

        async def act(key, verb):
            decision = await hub.decision()
            if not decision.allowed and not unlocked["by_pin"]:
                if not decision.pin_required:
                    ui.notify(decision.reason, type="warning", multi_line=True)
                    return
                if not hub.pin_allowed(client.ip):
                    ui.notify("Too many wrong PINs. Try again in a few minutes.", type="negative")
                    return
                if not access.pin_matches(hub.settings["access"]["pin"], pin_input.value):
                    hub.pin_failed(client.ip)
                    ui.notify("Wrong PIN.", type="negative")
                    return
                unlocked["by_pin"] = True
                pin_input.value = ""
            hub.start_service(key) if verb == "start" else hub.stop_service(key)

        def refresh_access():
            decision = hub.access
            open_ = decision.allowed or unlocked["by_pin"]
            lock_note.text = "Unlocked with the PIN." if unlocked["by_pin"] and not decision.allowed else decision.reason
            pin_input.visible = decision.pin_required and not open_
            return open_ or decision.pin_required

        def refresh():
            clock_label.text = datetime.datetime.now().strftime("%H:%M:%S")
            usable = refresh_access()

            for key, (badge, spinner, detail, (address, address_plain, scope), start_button, stop_button,
                      error) in rows.items():
                status = hub.status(key)
                badge.text = status
                badge.props(f"color={STATUS_COLORS[status]}")
                spinner.visible = status in ("starting", "stopping")
                state = hub.container[key]
                detail.text = f"container: {state}" if state not in (None, "running") else ""
                service, where = hub.services[key], hub.scope[key]
                host = socket.gethostname() if where == "all" else "127.0.0.1"
                url = f"http://{host}:{service.port}{service.url_path}" if where else ""
                address.text = url
                address.props(f'href="{url}"')
                address.visible = bool(url) and service.browsable
                address_plain.text = url
                address_plain.visible = bool(url) and not service.browsable
                scope.text = {"all": "all interfaces", "local": "this machine only"}.get(where, "")
                # Stop stays available while starting: it cancels the start and removes the container.
                start_button.enabled = usable and status == "stopped"
                stop_button.enabled = usable and status in ("running", "starting")
                error.text = hub.error[key]
                error.visible = bool(hub.error[key])

            props = hub.props or {}
            if hub.ready["gufo"]:
                model_label.text = props.get("model_alias") or props.get("model") or "?"
                n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
                version = (props.get("build_info") or "").split(" ")[0]
                parts = [f"{n_ctx // 1024} Ki context" if n_ctx else "", f"gufo {version}" if version else ""]
                model_detail.text = " · ".join(part for part in parts if part)
            else:
                model_label.text = {"starting": "loading…", "stopping": "stopping…"}.get(hub.status("gufo"), "not running")
                model_detail.text = ""
            if len(session_bars) != len(hub.slots):
                sessions_row.clear()
                session_bars.clear()
                with sessions_row:
                    for _ in hub.slots:
                        with ui.element("div").classes("h-3 flex-1 rounded") as bar:
                            session_bars.append((bar, ui.tooltip()))
            for (bar, tip), slot in zip(session_bars, hub.slots):
                bar.style(f"background: {'#7c6cf0' if slot['busy'] else 'rgba(128,128,128,.25)'}")
                tip.text = (f"session {slot['id']}: generating at {_fmt(slot['tps'], '.1f', ' tok/s')}, "
                            f"{slot['decoded']} tokens so far" if slot["busy"] else f"session {slot['id']}: idle")
            busy_slots = sum(1 for s in hub.slots if s["busy"])
            deferred = hub.metrics.get("llamacpp:requests_deferred") or 0
            sessions_label.text = (f"{busy_slots} of {len(hub.slots)} sessions busy"
                                   + (f", {deferred:.0f} waiting" if deferred else "")) if hub.slots else ""

            speed_label.text = _fmt(hub.gen_tps, ".1f", " tok/s") if hub.ready["gufo"] else "–"
            live = [slot["tps"] for slot in hub.slots if slot["busy"] and slot["tps"] is not None]
            live_label.text = f"Now, per session: {' · '.join(f'{tps:.1f}' for tps in live)} tok/s" if live else ""
            live_label.visible = bool(live)
            if hub.requests:
                last = hub.requests[-1]
                last_label.text = (f"Last request: {_fmt(last.get('decode_tps'), '.1f')} tok/s generation, "
                                   f"{_fmt(last.get('prefill_tps'), '.0f')} tok/s prefill")
            else:
                last_label.text = ""

            s = hub.latest
            power_label.text = _fmt(s.get("ppt_w"), ".1f", " W")
            temp_label.text = f"CPU {_fmt(s.get('cpu_temp_c'), '.0f', ' °C')} · GPU {_fmt(s.get('gpu_temp_c'), '.0f', ' °C')}"
            load_label.text = f"GPU {_fmt(s.get('gpu_busy_pct'), '.0f', ' %')}"
            cpu_label.text = (f"CPU {_fmt(s.get('cpu_busy_pct'), '.0f', ' %')} · GPU clock "
                              f"{_fmt(s.get('gpu_mhz'), '.0f', ' MHz')} · load {_fmt(s.get('load1'), '.2f')}")
            ram_used, ram_total = s.get("ram_used_gib"), s.get("ram_total_gib")
            ram_label.text = f"RAM {_fmt(ram_used, '.1f')} of {_fmt(ram_total, '.0f', ' GiB')}"
            ram_bar.value = ram_used / ram_total if ram_used is not None and ram_total else 0
            gtt_used, gtt_total = s.get("gtt_used_gib"), s.get("gtt_total_gib")
            gtt_label.text = f"GTT (mapped by the GPU) {_fmt(gtt_used, '.1f')} of {_fmt(gtt_total, '.0f', ' GiB')}"
            gtt_bar.value = gtt_used / gtt_total if gtt_used is not None and gtt_total else 0

            now = time.monotonic()
            for chart, series in ((speed_chart, "gen_tps"), (power_chart, "ppt_w"), (load_chart, "gpu_busy_pct")):
                chart.options["xAxis"]["min"] = -hub.settings["history"]["seconds"]
                chart.options["xAxis"]["max"] = 0
                chart.options["series"][0]["data"] = [[round(t - now, 1), values[series]]
                                                      for t, values in hub.history if values[series] is not None]
                chart.update()

            table.rows = [{"id": r.get("request", i),
                           "time": r["time"].astimezone().strftime("%H:%M:%S"),
                           "tokens": f"{_fmt(r.get('prompt_tokens'), 'd')} → {_fmt(r.get('generated_tokens'), 'd')}",
                           "decode": _fmt(r.get("decode_tps"), ".1f", " tok/s"),
                           "prefill": _fmt(r.get("prefill_tps"), ".0f", " tok/s"),
                           "ttft": _fmt(_ms_to_s(r.get("ttft_ms")), ".2f", " s"),
                           "duration": _fmt(_ms_to_s(r.get("duration_ms")), ".1f", " s"),
                           "finish": str(r.get("finish", "–"))}
                          for i, r in enumerate(reversed(hub.requests))]
            table.visible = bool(hub.requests)
            no_requests.visible = not hub.requests

        refresh()
        ui.timer(hub.interval, refresh)

        await client.connected()
        try:
            stored = await ui.run_javascript("try { return localStorage.getItem('m97-dark') } catch (e) { return null }")
        except TimeoutError:
            stored = None
        if stored in ("0", "1"):
            dark.set_value(stored == "1")

    app.on_startup(hub.start)
    app.on_shutdown(hub.stop)


def _ms_to_s(value):
    return value / 1000 if isinstance(value, (int, float)) else None
