"""Settings: the defaults m97 ships, merged with the user's own file.

The defaults live in `settings.toml` beside this module. The user's file, `~/.config/m97/settings.toml` by
default, overrides them key by key: a table merges into the table of the same name, and any other value —
an array included — replaces the default whole. So the m97 tree stays as shipped, and an update never
collides with a local edit.
"""

import importlib.resources
import logging
import pathlib
import tomllib

__all__ = ["SettingsError",
           "USER_SETTINGS",
           "merge", "load"]

logger = logging.getLogger(__name__)

USER_SETTINGS = pathlib.Path("~/.config/m97/settings.toml")


class SettingsError(ValueError):
    """A settings file that cannot be used: unparseable, or a value of the wrong type."""


def _compatible(default, value):
    # An integer where a float is expected is fine (`interval = 2`); the reverse would truncate, and a bool
    # is an int to Python but never what an integer setting means.
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def merge(defaults, overrides, *, source="settings"):
    """Merge `overrides` into `defaults`, returning a new dict and leaving both arguments untouched.

    Tables merge recursively; any other value replaces the default. A key with no default is logged as a
    probable typo and skipped. A value whose type differs from its default's raises `SettingsError`;
    `source` names the file in that message and in the log.
    """
    def walk(defaults, overrides, prefix):
        merged = dict(defaults)
        for key, value in overrides.items():
            name = f"{prefix}{key}"
            if key not in defaults:
                logger.warning(f"{source}: unknown setting `{name}`, ignored (a typo?)")
                continue
            default = defaults[key]
            if not _compatible(default, value):
                raise SettingsError(f"{source}: `{name}` should be {type(default).__name__}, "
                                    f"got {type(value).__name__}: {value!r}")
            if isinstance(default, dict):
                merged[key] = walk(default, value, f"{name}.")
            else:
                merged[key] = value
                logger.info(f"{source}: `{name}` = {value!r}")
        return merged
    return walk(defaults, overrides, "")


def load(user_settings=USER_SETTINGS):
    """Return the settings: the shipped defaults, overridden by `user_settings` if that file exists.

    `user_settings` is a `str` or a `pathlib.Path`; a leading `~` is expanded. Raises `SettingsError` if
    either file is not valid TOML, or a value has the wrong type.
    """
    shipped = importlib.resources.files(__package__).joinpath("settings.toml")
    defaults = _parse(shipped.read_text(encoding="utf-8"), source="shipped settings.toml")
    path = pathlib.Path(user_settings).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info(f"{path}: not found, using the defaults")
        return defaults
    return merge(defaults, _parse(text, source=str(path)), source=str(path))


def _parse(text, *, source):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{source}: {exc}") from exc
