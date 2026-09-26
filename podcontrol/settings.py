"""User settings persisted across restarts (~/.config/podcontrol/settings.json,
$PODCONTROL_SETTINGS overrides): toolbar values, Shared AE on/off (an armed
pod resumes after a restart) and window geometry. Saved on every change (debounced)
and on close.

A missing key falls back to defaults.json shipped beside this module, which
carries the pod's working configuration so a fresh install comes up already set
up the way the pod is run rather than on generic factory values. The user's own
file always wins key by key, so an existing install is untouched and a new
default only fills a gap.
"""
import os, json

DEFAULT_PATH = os.path.expanduser("~/.config/podcontrol/settings.json")
# Shipped defaults live in the package so an editable install and a wheel agree.
DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json")


def path():
    return os.environ.get("PODCONTROL_SETTINGS") or DEFAULT_PATH


def _read(p):
    try:
        with open(p) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def defaults():
    """The settings a fresh install starts from. Deliberately carries no window
    geometry: a saved size and position from another machine can land the window
    offscreen, so the app places its own window on a first run."""
    return _read(DEFAULTS_PATH)


def load():
    """Shipped defaults with the user's saved settings layered over them."""
    d = defaults()
    d.update(_read(path()))
    return d


def save(d):
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        tmp = path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, path())
        return True
    except OSError:
        return False
