import json
import pathlib

import pytest

from m97.intent import default_state_path, load_state, save_state, track


def test_track_explicit_actions_count_at_once():
    assert track("stopped", 5.0, running=False, action="start", now=6.0) == ("running", None)
    assert track("running", None, running=True, action="stop", now=6.0) == ("stopped", None)


def test_track_running_container_means_wanted_running():
    assert track(None, 3.0, running=True, action=None, now=4.0) == ("running", None)


def test_track_down_past_grace_means_wanted_stopped():
    want, down = track("running", None, running=False, action=None, now=10.0, grace=30.0)
    assert (want, down) == ("running", 10.0)
    want, down = track(want, down, running=False, action=None, now=39.0, grace=30.0)
    assert want == "running"
    want, down = track(want, down, running=False, action=None, now=40.0, grace=30.0)
    assert want == "stopped"


def test_track_nothing_known_stays_unknown():
    assert track(None, None, running=False, action=None, now=100.0)[0] is None


@pytest.mark.parametrize("as_type", [str, pathlib.Path])
def test_state_round_trip(tmp_path, as_type):
    path = tmp_path / "deeper" / "state.json"
    save_state(as_type(path), {"want": {"gufo": "running"}, "problem": {"openwebui": "it broke"}})
    assert load_state(as_type(path)) == {"want": {"gufo": "running"}, "problem": {"openwebui": "it broke"}}
    assert not path.with_name("state.json.tmp").exists()


def test_state_missing_or_garbage(tmp_path):
    assert load_state(tmp_path / "absent.json") == {"want": {}, "problem": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_state(bad) == {"want": {}, "problem": {}}
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"want": {"gufo": "maybe", "openwebui": "stopped"}, "problem": {"gufo": 3}}))
    assert load_state(odd) == {"want": {"openwebui": "stopped"}, "problem": {}}


def test_default_state_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_state_path() == tmp_path / "m97" / "state.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert default_state_path().parts[-3:] == ("state", "m97", "state.json")
