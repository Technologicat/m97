"""The page: what the machine and its LLM server are doing, and buttons to start and stop the services."""

import datetime
import socket
import time

from nicegui import app, ui

from . import access, gufo, report, services, usage

__all__ = ["build"]

# Badge (background, text) per status, chosen for contrast: white on green-700 is 5.0:1, black on amber
# 12:1, white on grey-700 6.2:1, white on slate-600 7.6:1 (WCAG AA asks 4.5:1 for small text).
STATUS_COLORS = {"running": ("#15803d", "#ffffff"),
                 "starting": ("#f2c037", "#000000"),
                 "stopping": ("#f2c037", "#000000"),
                 "stopped": ("#616161", "#ffffff"),
                 "unknown": ("#475569", "#ffffff")}


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


# The usage chart's stacks, bottom up: (key in the usage payload, legend, colour). Serving shares the generation
# chart's purple, loading the starting badge's amber, stopped its grey; m97 off is left nearly empty.
USAGE_SERIES = (("serving", "Serving", "#7c6cf0"),
                ("idle", "Loaded, idle", "#b4aaf7"),
                ("loading", "Loading", "#f2c037"),
                ("stopped", "Stopped", "#757575"),
                ("m97_off", "m97 off", "rgba(128,128,128,.18)"))

# Hover text for a day: each stack's hours and energy, the day's total energy, and the other GPU use.
_USAGE_TOOLTIP = """params => {
  const d = params[0].data;
  let s = d.date + '<br>', total = 0;
  for (const p of params) {
    s += p.marker + p.seriesName + ': ' + p.value.toFixed(1) + ' h';
    if (p.data.wh != null) { s += ', ' + p.data.wh + ' Wh'; total += p.data.wh; }
    s += '<br>';
  }
  return s + 'Energy: ' + total + ' Wh<br>Other GPU use: ' + d.gpu_other_h.toFixed(1) + ' h, ' + d.gpu_other_wh + ' Wh';
}"""


def _usage_chart():
    """ECharts options for the usage card: a stacked bar of hours per day."""
    axis = {"color": "#888"}
    return {"animation": False,
            "grid": {"left": 36, "right": 8, "top": 32, "bottom": 24},
            "legend": {"top": 0, "textStyle": axis, "itemWidth": 12, "itemHeight": 12},
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}, ":formatter": _USAGE_TOOLTIP},
            "xAxis": {"type": "category", "data": [], "axisLabel": axis},
            "yAxis": {"type": "value", "min": 0, "max": 24, "interval": 6,
                      "axisLabel": dict(axis, formatter="{value} h"),
                      "splitLine": {"lineStyle": {"color": "rgba(128,128,128,.2)"}}},
            "series": [{"name": title, "type": "bar", "stack": "day", "barMaxWidth": 28, "data": [],
                        "itemStyle": {"color": color}}
                       for _, title, color in USAGE_SERIES]}


def build(hub):
    """Register the page and `GET /api/status`, reading everything they show from `hub`."""

    @app.get("/api/status")
    def api_status(history: int = 0):
        return report.snapshot(hub, history=bool(history))

    @app.get("/api/usage")
    def api_usage(days: int = 14):
        return report.usage_report(hub.usage, days=max(1, min(days, 3660)))

    @ui.page("/")
    async def index(client):
        ui.page_title(f"m97 · {socket.gethostname()}")
        dark = ui.dark_mode(True)
        # Quasar marks a disabled button only by fading it to 70 % opacity, which beside an enabled one reads as a
        # slightly different shade of the same button. Grey it out instead.
        ui.add_css(".q-btn.disabled { opacity: 0.3 !important; filter: grayscale(1); }")
        ui.add_css(".q-card { border-radius: 10px; } .q-btn { border-radius: 8px; }")
        # Quasar's red (#c10015) is 2.6:1 on the dark background; red-400 is 6.1:1 there, though too light on
        # white. Its variable reaches the Stop buttons and the error text alike. `!important` because Quasar
        # sets the variable as an inline style on <body>, which an ordinary rule cannot override.
        ui.add_css(".body--dark { --q-negative: #f87171 !important; }")

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
            with ui.card().props("flat bordered").classes("w-full"):
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
                    with ui.column().classes("w-full gap-0") as error:
                        error_summary = ui.label().classes("text-sm text-negative")
                        with ui.expansion("Details").props("dense").classes("text-sm w-full") as error_more:
                            error_details = ui.label().classes("text-xs font-mono whitespace-pre-wrap opacity-80")
                    rows[key] = (badge, spinner, detail, (address, address_plain, scope), start_button, stop_button,
                                 (error, error_summary, error_more, error_details))
                with ui.row().classes("w-full items-center gap-3"):
                    lock_note = ui.label().classes("text-sm opacity-70")
                    pin_input = ui.input("PIN", password=True).props("dense outlined").classes("w-32")

            # ---- Model, speed, power, load, memory
            with ui.grid(columns="repeat(auto-fit, minmax(17rem, 1fr))").classes("w-full gap-4"):
                with ui.card().props("flat bordered"):
                    ui.label("Model").classes("text-base font-medium")
                    model_label = ui.label().classes("text-xl")
                    model_detail = ui.label().classes("text-sm opacity-70")
                    sessions_row = ui.row().classes("w-full gap-1 mt-2")
                    session_bars = []   # (bar, tooltip) per session, rebuilt when the count changes
                    sessions_label = ui.label().classes("text-sm opacity-70")

                with ui.card().props("flat bordered"):
                    ui.label("Generation").classes("text-base font-medium")
                    speed_label = ui.label().classes("text-xl")
                    speed_chart = ui.echart(_spark("tok/s", "#7c6cf0")).classes("w-full h-16")
                    live_label = ui.label().classes("text-sm")
                    last_label = ui.label().classes("text-sm opacity-70")

                with ui.card().props("flat bordered"):
                    ui.label("Power and temperatures").classes("text-base font-medium")
                    power_label = ui.label().classes("text-xl")
                    power_chart = ui.echart(_spark("W", "#e8833a")).classes("w-full h-16")
                    temp_label = ui.label().classes("text-sm opacity-70")

                # CPU and GPU start alike, a readout over a load sparkline of the same height and scale, so the
                # two sparklines sit level across the row and the same height means the same load in both.
                with ui.card().props("flat bordered"):
                    ui.label("CPU").classes("text-base font-medium")
                    cpu_load_label = ui.label().classes("text-xl")
                    cpu_chart = ui.echart(_spark("CPU %", "#3b9ede", max_value=100)).classes("w-full h-16")
                    # One column per physical core, holding a bar per hardware thread of that core; 16 columns
                    # to a row.
                    thread_bars = {}   # CPU number -> (fill, tooltip, core)
                    with ui.element("div").classes("w-full grid gap-x-[3px] gap-y-2").style(
                            f"grid-template-columns: repeat({min(16, len(hub.cpu_cores)) or 1}, minmax(0, 1fr))"):
                        for core, threads in enumerate(hub.cpu_cores):
                            with ui.element("div").classes("flex flex-col gap-[2px]"):
                                for cpu in threads:
                                    with ui.element("div").classes("relative h-5 rounded-sm overflow-hidden").style(
                                            "background: rgba(128,128,128,.2)"):
                                        fill = ui.element("div").classes("absolute bottom-0 left-0 right-0").style(
                                            "background: #3b9ede; height: 0%")
                                        thread_bars[cpu] = (fill, ui.tooltip(), core)
                    cpu_label = ui.label().classes("text-sm opacity-70 mt-auto")

                with ui.card().props("flat bordered"):
                    ui.label("GPU").classes("text-base font-medium")
                    gpu_load_label = ui.label().classes("text-xl")
                    gpu_chart = ui.echart(_spark("GPU %", "#2f9e72", max_value=100)).classes("w-full h-16")
                    gpu_label = ui.label().classes("text-sm opacity-70 mt-auto")

                with ui.card().props("flat bordered"):
                    ui.label("Memory").classes("text-base font-medium")
                    ram_label = ui.label().classes("text-sm")
                    ram_bar = ui.linear_progress(show_value=False).props("rounded size=10px")
                    gtt_label = ui.label().classes("text-sm mt-2")
                    gtt_bar = ui.linear_progress(show_value=False, color="secondary").props("rounded size=10px")

            # ---- Recent requests
            with ui.card().props("flat bordered").classes("w-full"):
                ui.label("Recent requests").classes("text-base font-medium")
                columns = [{"name": "time", "label": "Time", "field": "time", "align": "left"},
                           {"name": "tokens", "label": "Prompt → generated", "field": "tokens", "align": "right"},
                           {"name": "output", "label": "Generation", "field": "output", "align": "right"},
                           {"name": "decode", "label": "Decoding", "field": "decode", "align": "right"},
                           {"name": "prefill", "label": "Prefill", "field": "prefill", "align": "right"},
                           {"name": "ttft", "label": "First token", "field": "ttft", "align": "right"},
                           {"name": "duration", "label": "Duration", "field": "duration", "align": "right"},
                           {"name": "finish", "label": "Finish", "field": "finish", "align": "left"}]
                table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense").classes("w-full")
                no_requests = ui.label("No finished requests yet.").classes("text-sm opacity-70")

            # ---- Usage
            with ui.card().props("flat bordered").classes("w-full"):
                ui.label("Usage, last 14 days").classes("text-base font-medium")
                # The chart and, beside it, the other GPU use; on a phone, one under the other.
                with ui.element("div").classes("w-full grid gap-4 grid-cols-1 md:grid-cols-[minmax(0,1fr)_14rem]"):
                    usage_chart = ui.echart(_usage_chart()).classes("w-full h-64")
                    with ui.column().classes("gap-1 md:pt-8"):
                        ui.label("Other GPU use").classes("text-sm font-medium")
                        gpu_other_today = ui.label().classes("text-sm")
                        gpu_other_total = ui.label().classes("text-sm")
                        ui.label(f"GPU busier than {usage.GPU_OTHER_PCT} % while gufo is not serving, whatever "
                                 f"is using it.").classes("text-xs opacity-70")
                ui.label("m97 off: time m97 did not see, with the machine off or asleep, or m97 not running."
                         ).classes("text-xs opacity-70")

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
                      (error, error_summary, error_more, error_details)) in rows.items():
                status = hub.status(key)
                badge.text = status
                background, foreground = STATUS_COLORS[status]
                badge.style(f"background: {background} !important; color: {foreground} !important")
                spinner.visible = status in ("starting", "stopping")
                state = hub.container[key]
                detail.text = hub.note[key] or (f"container: {state}" if state not in (None, "running") else "")
                service, where = hub.services[key], hub.scope[key]
                url = services.service_url(service, where) or ""
                address.text = url
                address.props(f'href="{url}"')
                address.visible = bool(url) and service.browsable
                address_plain.text = url
                address_plain.visible = bool(url) and not service.browsable
                scope.text = {"all": "all interfaces", "local": "this machine only"}.get(where, "")
                # Stop stays available while starting: it cancels the start and removes the container.
                start_button.enabled = usable and status == "stopped"
                stop_button.enabled = usable and status in ("running", "starting", "unknown")
                # A live error, or one remembered from before m97 restarted. What to read comes before the
                # first blank line; the rest, usually log lines, is folded away.
                message = hub.error[key] or hub.problem[key]
                summary, _, details = message.partition("\n\n")
                error_summary.text, error_details.text = summary, details
                error_more.visible = bool(details)
                error.visible = bool(message)

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
                # A busy session's bar is filled as far as its context is: a request running away in a loop
                # shows as a bar creeping across.
                if not slot["busy"]:
                    background = "rgba(128,128,128,.25)"
                elif slot["fill"] is None:
                    background = "#7c6cf0"
                else:
                    edge = max(2.0, 100 * slot["fill"])   # a sliver even when nearly empty, to read as busy
                    background = f"linear-gradient(to right, #7c6cf0 {edge:.1f}%, rgba(124,108,240,.3) {edge:.1f}%)"
                bar.style(f"background: {background}")
                if slot["busy"]:
                    used = (slot["prompt_tokens"] or 0) + (slot["decoded"] or 0)
                    tip.text = (f"session {slot['id']}: generating at {_fmt(slot['tps'], '.1f', ' tok/s')}; "
                                f"context {used:,} of {_fmt(slot['n_ctx'], ',')} tokens "
                                f"({_fmt(None if slot['fill'] is None else 100 * slot['fill'], '.1f', ' %')}), "
                                f"{_fmt(slot['decoded'], ',')} generated")
                else:
                    tip.text = f"session {slot['id']}: idle"
            busy_slots = [s for s in hub.slots if s["busy"]]
            fills = [s["fill"] for s in busy_slots if s["fill"] is not None]
            deferred = hub.metrics.get("llamacpp:requests_deferred") or 0
            sessions_label.text = " · ".join(part for part in (
                f"{len(busy_slots)} of {len(hub.slots)} sessions busy" if hub.slots else "",
                f"{deferred:.0f} waiting" if deferred else "",
                # One decimal below 10 %, where whole percents would read "0 %" for a session that is busy.
                f"fullest context {100 * max(fills):.{1 if max(fills) < 0.1 else 0}f} %" if fills else "") if part)

            speed_label.text = _fmt(hub.gen_tps, ".1f", " tok/s") if hub.ready["gufo"] else "–"
            live = [slot["tps"] for slot in hub.slots if slot["busy"] and slot["tps"] is not None]
            live_label.text = f"Now, per session: {' · '.join(f'{tps:.1f}' for tps in live)} tok/s" if live else ""
            live_label.visible = bool(live)
            if hub.requests:
                last = hub.requests[-1]
                last_label.text = (f"Last request: {_fmt(gufo.output_tps(last), '.1f')} tok/s generation "
                                   f"({_fmt(last.get('decode_tps'), '.1f')} decoding), "
                                   f"{_fmt(last.get('prefill_tps'), '.0f')} tok/s prefill")
            else:
                last_label.text = ""

            s = hub.latest
            power_label.text = _fmt(s.get("ppt_w"), ".1f", " W")
            temp_label.text = f"CPU {_fmt(s.get('cpu_temp_c'), '.0f', ' °C')} · GPU {_fmt(s.get('gpu_temp_c'), '.0f', ' °C')}"
            cpu_load_label.text = _fmt(s.get("cpu_busy_pct"), ".0f", " %")
            gpu_load_label.text = _fmt(s.get("gpu_busy_pct"), ".0f", " %")
            gpu_label.text = f"clock {_fmt(s.get('gpu_mhz'), '.0f', ' MHz')}"
            ram_used, ram_total = s.get("ram_used_gib"), s.get("ram_total_gib")
            ram_label.text = f"RAM {_fmt(ram_used, '.1f')} of {_fmt(ram_total, '.0f', ' GiB')}"
            ram_bar.value = ram_used / ram_total if ram_used is not None and ram_total else 0
            gtt_used, gtt_total = s.get("gtt_used_gib"), s.get("gtt_total_gib")
            gtt_label.text = f"GTT (mapped by the GPU) {_fmt(gtt_used, '.1f')} of {_fmt(gtt_total, '.0f', ' GiB')}"
            gtt_bar.value = gtt_used / gtt_total if gtt_used is not None and gtt_total else 0

            per_thread = s.get("cpu_thread_pct") or {}
            for cpu, (fill, tip, core) in thread_bars.items():
                pct = per_thread.get(cpu)
                fill.style(f"background: #3b9ede; height: {0 if pct is None else min(100.0, pct):.0f}%")
                tip.text = f"core {core}, CPU {cpu}: {_fmt(pct, '.0f', ' %')}"
            busy = [pct for pct in per_thread.values() if pct is not None]
            cpu_label.text = " · ".join(part for part in (
                f"{len(hub.cpu_cores)} cores, {len(per_thread)} threads" if per_thread else "",
                f"busiest {max(busy):.0f} %" if busy else "",
                f"load {_fmt(s.get('load1'), '.2f')}") if part)

            now = time.monotonic()
            for chart, series in ((speed_chart, "gen_tps"), (power_chart, "ppt_w"), (cpu_chart, "cpu_busy_pct"),
                                  (gpu_chart, "gpu_busy_pct")):
                chart.options["xAxis"]["min"] = -hub.settings["history"]["seconds"]
                chart.options["xAxis"]["max"] = 0
                chart.options["series"][0]["data"] = [[round(t - now, 1), values[series]]
                                                      for t, values in hub.history if values[series] is not None]
                chart.update()

            table.rows = [{"id": r.get("request", i),
                           "time": r["time"].astimezone().strftime("%H:%M:%S"),
                           "tokens": f"{_fmt(r.get('prompt_tokens'), ',d')} → {_fmt(r.get('generated_tokens'), ',d')}",
                           "output": _fmt(gufo.output_tps(r), ".1f", " tok/s"),
                           "decode": _fmt(r.get("decode_tps"), ".1f", " tok/s"),
                           "prefill": _prefill_cell(r),
                           "ttft": _fmt(_ms_to_s(r.get("ttft_ms")), ".2f", " s"),
                           "duration": _fmt(_ms_to_s(r.get("duration_ms")), ".1f", " s"),
                           "finish": str(r.get("finish", "–"))}
                          for i, r in enumerate(reversed(hub.requests))]
            table.visible = bool(hub.requests)
            no_requests.visible = not hub.requests

        def refresh_usage():
            days = report.usage_report(hub.usage, days=14)["days"]
            usage_chart.options["xAxis"]["data"] = [
                datetime.date.fromisoformat(d["date"]).strftime("%a %-d") for d in days]
            for series, (key, _, _) in zip(usage_chart.options["series"], USAGE_SERIES):
                series["data"] = [{"value": d[f"{key}_h"], "wh": d.get(f"{key}_wh"), "date": d["date"],
                                   "gpu_other_h": d["gpu_other_h"], "gpu_other_wh": d["gpu_other_wh"]}
                                  for d in days]
            usage_chart.update()
            today = days[-1]
            gpu_other_today.text = f"Today {today['gpu_other_h']:.1f} h, {today['gpu_other_wh']} Wh"
            hours = sum(d["gpu_other_h"] for d in days)
            energy = sum(d["gpu_other_wh"] for d in days)
            gpu_other_total.text = f"14 days {hours:.1f} h, {energy} Wh"

        refresh()
        ui.timer(hub.interval, refresh)
        refresh_usage()
        ui.timer(60.0, refresh_usage)   # the bars move by minutes, so once a minute is enough

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


def _prefill_cell(record):
    """Prefill speed for the recent-requests table, or "cached" when the whole prompt came from the cache."""
    if gufo.prompt_fully_cached(record):
        return "cached"
    return _fmt(record.get("prefill_tps"), ".0f", " tok/s")
