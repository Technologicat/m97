import ipaddress

import pytest

from m97.access import (decide, make_rate_limiter, parse_active_connections, parse_addresses, pin_matches,
                        trusted_network)

HOME_UUID = "11111111-1111-1111-1111-111111111111"
TRAVEL_2G = "22222222-2222-2222-2222-222222222222"
TRAVEL_5G = "33333333-3333-3333-3333-333333333333"
CAFE_UUID = "44444444-4444-4444-4444-444444444444"
LOOPBACK = "55555555-5555-5555-5555-555555555555"

NETWORKS = [{"name": "home", "connections": [HOME_UUID], "subnet": "192.168.50.0/24"},
            {"name": "travel", "connections": [TRAVEL_2G, TRAVEL_5G]}]


def on(uuid, *addresses):
    return [(uuid, [ipaddress.ip_interface(a) for a in addresses]), (LOOPBACK, [])]


# --------------------------------------------------------------------------------
# nmcli output

def test_parse_active_connections():
    text = f"{HOME_UUID}:wlp99s0\n{LOOPBACK}:lo\n\nnonsense\n"
    assert parse_active_connections(text) == [(HOME_UUID, "wlp99s0"), (LOOPBACK, "lo")]


def test_parse_addresses():
    assert parse_addresses("192.168.50.58/24 | 10.0.0.2/8\n") == [ipaddress.ip_interface("192.168.50.58/24"),
                                                                  ipaddress.ip_interface("10.0.0.2/8")]
    assert parse_addresses("") == []
    assert parse_addresses("not-an-address") == []


# --------------------------------------------------------------------------------
# Trusted networks

def test_trusted_by_uuid_and_subnet():
    assert trusted_network(NETWORKS, on(HOME_UUID, "192.168.50.58/24")) == "home"


def test_trusted_uuid_outside_its_subnet():
    assert trusted_network(NETWORKS, on(HOME_UUID, "192.168.1.7/24")) is None


def test_trusted_any_of_several_profiles():
    assert trusted_network(NETWORKS, on(TRAVEL_2G, "192.168.1.7/24")) == "travel"
    assert trusted_network(NETWORKS, on(TRAVEL_5G, "192.168.1.8/24")) == "travel"


def test_untrusted_network_with_a_trusted_subnet():
    # A café on the same factory-default subnet as the travel router is still a café.
    assert trusted_network(NETWORKS, on(CAFE_UUID, "192.168.50.20/24")) is None


def test_no_connections_and_no_networks():
    assert trusted_network(NETWORKS, []) is None
    assert trusted_network([], on(HOME_UUID, "192.168.50.58/24")) is None


def test_bad_subnet_never_matches():
    networks = [{"name": "typo", "connections": [HOME_UUID], "subnet": "192.168.50/24x"}]
    assert trusted_network(networks, on(HOME_UUID, "192.168.50.58/24")) is None


# --------------------------------------------------------------------------------
# The PIN and its rate limit

def test_pin():
    assert pin_matches("0451", "0451")
    assert not pin_matches("0451", "0452")
    assert not pin_matches("0451", "")
    assert not pin_matches("0451", None)
    assert not pin_matches("", "")              # no PIN configured opens nothing


def test_rate_limiter():
    now = [1000.0]
    allowed, record_failure = make_rate_limiter(max_failures=3, window=60.0, clock=lambda: now[0])
    for _ in range(3):
        assert allowed("10.0.0.5")
        record_failure("10.0.0.5")
    assert not allowed("10.0.0.5")
    assert allowed("10.0.0.6"), "one client's failures must not lock out another"
    now[0] += 61.0
    assert allowed("10.0.0.5"), "failures older than the window no longer count"


# --------------------------------------------------------------------------------
# The decision

def test_decide_trusted():
    decision = decide({"pin": "", "trusted_networks": NETWORKS}, on(HOME_UUID, "192.168.50.58/24"))
    assert decision.allowed and not decision.pin_required
    assert "home" in decision.reason


def test_decide_untrusted_with_pin():
    decision = decide({"pin": "0451", "trusted_networks": NETWORKS}, on(CAFE_UUID, "10.1.2.3/16"))
    assert not decision.allowed and decision.pin_required


@pytest.mark.parametrize("connections", [[], on(CAFE_UUID, "10.1.2.3/16")])
def test_decide_untrusted_without_pin_says_what_to_do(connections):
    decision = decide({"pin": "", "trusted_networks": NETWORKS}, connections)
    assert not decision.allowed and not decision.pin_required
    assert "settings.toml" in decision.reason and "pin" in decision.reason
