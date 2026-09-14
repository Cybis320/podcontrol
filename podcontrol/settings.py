"""User settings persisted across restarts (~/.config/podcontrol/settings.json,
$PODCONTROL_SETTINGS overrides): toolbar values, Shared AE on/off (an armed
pod resumes after a restart) and window geometry. Saved on every change (debounced)
and on close; missing keys fall back to the built-in defaults."""
import os, json

DEFAULT_PATH = os.path.expanduser("~/.config/podcontrol/settings.json")


def path():
    return os.environ.get("PODCONTROL_SETTINGS") or DEFAULT_PATH


def load():
    try:
        with open(path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


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
