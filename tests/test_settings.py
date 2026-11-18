import logging
import pathlib

import pytest

from m97.settings import SettingsError, load, merge

DEFAULTS = {"server": {"port": 8097},
            "history": {"seconds": 600, "interval": 1.0},
            "access": {"pin": "", "trusted_networks": [{"name": "default"}]},
            "gufo": {"extra_args": ["--a", "--b"], "debug": False}}


def test_table_merges_key_by_key():
    merged = merge(DEFAULTS, {"history": {"seconds": 120}})
    assert merged["history"] == {"seconds": 120, "interval": 1.0}


def test_array_replaces_whole():
    merged = merge(DEFAULTS, {"gufo": {"extra_args": ["--c"]},
                              "access": {"trusted_networks": [{"name": "home"}, {"name": "phone"}]}})
    assert merged["gufo"]["extra_args"] == ["--c"]
    assert [n["name"] for n in merged["access"]["trusted_networks"]] == ["home", "phone"]


def test_arguments_untouched():
    overrides = {"history": {"seconds": 120}}
    merge(DEFAULTS, overrides)
    assert DEFAULTS["history"]["seconds"] == 600
    assert overrides == {"history": {"seconds": 120}}


def test_unknown_key_reported_and_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="m97.settings"):
        merged = merge(DEFAULTS, {"history": {"secnods": 120}, "nonsense": 1})
    assert merged == DEFAULTS
    assert "history.secnods" in caplog.text
    assert "nonsense" in caplog.text


def test_int_accepted_for_float():
    assert merge(DEFAULTS, {"history": {"interval": 2}})["history"]["interval"] == 2


@pytest.mark.parametrize("overrides", [
    {"history": {"seconds": "600"}},       # a string for an int
    {"history": {"seconds": 1.5}},         # a float for an int would truncate
    {"history": {"seconds": True}},        # a bool is an int to Python, but not a count
    {"gufo": {"debug": 1}},                # and an int is not a bool
    {"history": 600},                      # a scalar for a table
    {"access": {"pin": 1234}},             # a PIN written as a number would lose leading zeros
])
def test_wrong_type_raises(overrides):
    with pytest.raises(SettingsError):
        merge(DEFAULTS, overrides)


def test_load_without_user_file_gives_shipped_defaults(tmp_path):
    settings = load(tmp_path / "absent.toml")
    assert settings["server"]["port"] == 8097
    assert settings["access"]["pin"] == ""


@pytest.mark.parametrize("as_type", [str, pathlib.Path])
def test_load_merges_user_file(tmp_path, as_type):
    user = tmp_path / "settings.toml"
    user.write_text('[access]\npin = "0451"\n\n[gufo]\nsessions = 3\n', encoding="utf-8")
    settings = load(as_type(user))
    assert settings["access"]["pin"] == "0451"
    assert settings["gufo"]["sessions"] == 3
    assert settings["gufo"]["port"] == 8080          # untouched default beside an override


def test_load_rejects_invalid_toml(tmp_path):
    user = tmp_path / "settings.toml"
    user.write_text("[access\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="settings.toml"):
        load(user)
