import pytest

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
