import io
import json
import urllib.error
import urllib.request

import pytest

from m97.access import token_path, write_token
from m97.cli import main, unit_file, url_line


def test_unit_file_serves_the_network_by_default():
    text = unit_file(python="/opt/m97/.venv/bin/python", lan=True)
    assert "ExecStart=/opt/m97/.venv/bin/python -m m97 serve --lan\n" in text
    assert "WantedBy=default.target" in text
    assert "Restart=on-failure" in text
    assert "SyslogIdentifier=m97" in text


def test_unit_file_local_and_settings_path_with_spaces():
    text = unit_file(python="/opt/m97/bin/python", lan=False, settings_path="/home/u/my settings.toml")
    assert 'ExecStart=/opt/m97/bin/python -m m97 serve --settings "/home/u/my settings.toml"\n' in text
    assert "--lan" not in text


def test_url_line():
    assert "(all interfaces)" in url_line(8097, "all")
    assert url_line(8097, "local") == "  http://127.0.0.1:8097  (this machine only)"
    assert "nothing is listening" in url_line(8097, None)


def test_no_verb_prints_help(capsys):
    assert main([]) == 2
    assert "install" in capsys.readouterr().out


def test_unknown_verb_is_an_error():
    with pytest.raises(SystemExit):
        main(["frobnicate"])


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def token(monkeypatch, tmp_path):
    """A token where the CLI looks for it, as a running m97 would have written it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return write_token(token_path(tmp_path / "state" / "m97" / "state.json"))


def test_service_verb_posts_to_the_running_m97(monkeypatch, tmp_path, capsys, token):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.get_method(), request.full_url, request.get_header("Authorization"), timeout))
        return _FakeResponse(json.dumps({"status": "running", "error": None}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert main(["--settings", str(tmp_path / "none.toml"), "gufo", "restart"]) == 0
    assert seen == [("POST", "http://127.0.0.1:8097/api/services/gufo/restart", f"Bearer {token}", 600 + 120)]
    assert capsys.readouterr().out == "m97: restarting gufo...\nm97: gufo running\n"


def test_service_verb_reports_an_error_and_a_refusal(monkeypatch, tmp_path, capsys, token):
    settings = ["--settings", str(tmp_path / "none.toml")]

    def failed(request, timeout):
        return _FakeResponse(json.dumps({"status": "stopped", "error": "Open WebUI stopped while starting"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", failed)
    assert main([*settings, "openwebui", "start"]) == 1
    assert "Open WebUI stopped while starting" in capsys.readouterr().err

    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"error": "gufo is stopping; try again when that is done"}'))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert main([*settings, "gufo", "start"]) == 1
    assert "m97: gufo is stopping" in capsys.readouterr().err

    def down(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert main([*settings, "gufo", "stop"]) == 1
    assert "Is it running?" in capsys.readouterr().err


def test_service_verb_without_a_token_asks_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def urlopen(request, timeout):
        raise AssertionError("no request without a token")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert main(["--settings", str(tmp_path / "none.toml"), "gufo", "restart"]) == 1
    assert "control-token" in capsys.readouterr().err


def test_service_verb_needs_a_known_action():
    with pytest.raises(SystemExit):
        main(["gufo", "reload"])
