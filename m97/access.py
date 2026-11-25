"""Who may press the buttons: anyone while the machine is on a trusted network, and elsewhere whoever knows the PIN.

Viewing is always open; only starting and stopping services is gated. The `m97 gufo` and `m97 openwebui`
commands, run on the machine itself, are allowed by `is_local` instead. A trusted network is recognized by the
UUID of the NetworkManager connection the machine is using, optionally narrowed by a subnet. NetworkManager
offers a saved Wi-Fi profile only to an access point with the same SSID *and* compatible security, so a WPA
profile is not matched by an open network that borrows its name.
"""

import dataclasses
import hmac
import ipaddress
import logging
import subprocess
import time

__all__ = ["active_connections", "parse_active_connections", "parse_addresses",
           "trusted_network",
           "pin_matches", "make_rate_limiter",
           "Decision", "decide",
           "is_local"]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------
# What network the machine is on

def parse_active_connections(text):
    """Parse `nmcli -t -f UUID,DEVICE connection show --active` into a list of `(uuid, device)`."""
    connections = []
    for line in text.splitlines():
        uuid, sep, device = line.partition(":")
        if sep and uuid:
            connections.append((uuid, device))
    return connections


def parse_addresses(text):
    """Parse `nmcli -g IP4.ADDRESS device show <dev>` into a list of `ipaddress.IPv4Interface`.

    nmcli separates several addresses with ` | `; anything that is not an address is skipped.
    """
    addresses = []
    for item in text.replace("|", "\n").split():
        try:
            addresses.append(ipaddress.ip_interface(item))
        except ValueError:
            continue
    return addresses


def _nmcli(*args):
    result = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=5)
    result.check_returncode()
    return result.stdout


def active_connections():
    """Return the machine's active NetworkManager connections as a list of `(uuid, [addresses])`.

    Empty if NetworkManager is not there or does not answer, which makes every network untrusted.
    """
    try:
        connections = parse_active_connections(_nmcli("-f", "UUID,DEVICE", "connection", "show", "--active"))
        return [(uuid, parse_addresses(_nmcli("-g", "IP4.ADDRESS", "device", "show", device)) if device else [])
                for uuid, device in connections]
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"could not ask NetworkManager for the active connections, so no network is trusted: {exc}")
        return []


def trusted_network(networks, connections):
    """Return the name of the trusted network the machine is on, or `None`.

    `networks` is the `access.trusted_networks` setting: a list of tables with `name`, `connections` (a list
    of connection UUIDs) and optionally `subnet`. `connections` is what `active_connections` returns. A
    network matches when one of its UUIDs is active and, if it names a subnet, that connection has an
    address inside it.
    """
    for network in networks:
        uuids = set(network.get("connections", []))
        maybe_subnet = network.get("subnet")
        try:
            subnet = ipaddress.ip_network(maybe_subnet, strict=False) if maybe_subnet else None
        except ValueError:
            logger.warning(f"trusted network {network.get('name')!r}: `subnet` {maybe_subnet!r} is not a subnet, "
                           f"so the network never matches")
            continue
        for uuid, addresses in connections:
            if uuid not in uuids:
                continue
            if subnet is None or any(address.ip in subnet for address in addresses):
                return network.get("name") or uuid
    return None


# --------------------------------------------------------------------------------
# The PIN

def pin_matches(configured, given):
    """Whether `given` is the configured PIN. An empty configured PIN matches nothing."""
    if not configured:
        return False
    return hmac.compare_digest(configured.encode("utf-8"), (given or "").encode("utf-8"))


def make_rate_limiter(*, max_failures=5, window=300.0, clock=time.monotonic):
    """Make a limiter for wrong PINs: at most `max_failures` per client within `window` seconds.

    Returns `(allowed, record_failure)`. `allowed(client)` says whether the client may try now;
    `record_failure(client)` counts a wrong PIN. `client` is any hashable key, e.g. the client's address.
    """
    # Against a four-digit PIN: 10^4 values, so 5,000 guesses on average; at the defaults, one guess a minute,
    # that is 5,000 min ≈ 3.5 days per address, and a week to exhaust the space.
    failures = {}  # client -> monotonic times of recent failures

    def recent(client):
        now = clock()
        times = [t for t in failures.get(client, []) if now - t < window]
        if times:
            failures[client] = times
        else:
            failures.pop(client, None)
        return times

    def allowed(client):
        return len(recent(client)) < max_failures

    def record_failure(client):
        failures.setdefault(client, []).append(clock())

    return allowed, record_failure


# --------------------------------------------------------------------------------
# The decision the page shows

@dataclasses.dataclass(frozen=True, kw_only=True)
class Decision:
    """Whether the buttons are open, and a sentence for the page saying why.

    `allowed`: open without a PIN. `pin_required`: a PIN would open them. Both false: locked, and `reason`
    says what to configure. `network`: the name of the trusted network the machine is on, or `None`.
    """
    allowed: bool
    pin_required: bool
    reason: str
    network: str | None = None


def decide(access_settings, connections):
    """Decide whether the buttons are open, given the `access` settings and the active connections."""
    name = trusted_network(access_settings.get("trusted_networks", []), connections)
    if name is not None:
        return Decision(allowed=True, pin_required=False, reason=f"On the trusted network “{name}”.", network=name)
    if access_settings.get("pin"):
        return Decision(allowed=False, pin_required=True,
                        reason="Not on a trusted network: enter the PIN to use the buttons.")
    return Decision(allowed=False, pin_required=False,
                    reason="Locked: this machine is not on a trusted network, and no PIN is set. To use the buttons "
                           "here, set `pin` under `[access]` in ~/.config/m97/settings.toml, or add this network "
                           "to `trusted_networks` there.")


# --------------------------------------------------------------------------------
# The command line

def is_local(host):
    """Whether a request from `host`, a client address as text, comes from this machine: a loopback address.
    Anything that is not an IP address counts as remote."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False
