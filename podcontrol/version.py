"""What code is actually running.

`__version__` on its own cannot answer "did the update get picked up?", because
it only moves when somebody bumps it, while the thing that changes on every
update is the commit. So this reports the git revision of the checkout the
package was imported from.

It is read ONCE, at import. That is deliberate rather than lazy: a running
process's code is fixed at startup, so after the hourly updater pulls, this
keeps showing the OLD revision until the app restarts -- which is exactly the
signal to look for. A display that re-read git would show the new commit while
the old controller was still driving the pod, which is the confusion it exists
to prevent.

Falls back to the packaged version when the install is not a git checkout (a
wheel), and to bare `__version__` when git is unavailable.
"""
import os
import subprocess

from podcontrol import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args):
    """A git command in the checkout, or None. Short timeout: the app must start
    even where git hangs on a dead network mount."""
    try:
        out = subprocess.run(("git",) + args, cwd=ROOT, timeout=5.0,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace").strip() or None


def _probe():
    d = {"version": __version__, "rev": None, "when": None,
         "branch": None, "dirty": False, "root": ROOT}
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return d
    d["rev"] = _git("rev-parse", "--short", "HEAD")
    if d["rev"] is None:
        return d
    d["when"] = _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
    d["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
    # Tracked modifications only (-uno): an untracked file is not code this
    # process loaded, so it should not make the build read as modified. _git
    # returns None for empty output, which is exactly the clean case.
    d["dirty"] = _git("status", "--porcelain", "-uno") is not None
    return d


INFO = _probe()


def short():
    """One compact line for the window: version, revision, `+` when modified."""
    if not INFO["rev"]:
        return "v%s" % INFO["version"]
    return "v%s  %s%s" % (INFO["version"], INFO["rev"], "+" if INFO["dirty"] else "")


def detail():
    """The hover explanation, including why this can lag the checkout."""
    if not INFO["rev"]:
        return ("Pod Control v%s\nInstalled from a package, not a git checkout,\n"
                "so there is no revision to show." % INFO["version"])
    lines = ["Pod Control v%s" % INFO["version"],
             "revision %s%s" % (INFO["rev"], " (modified)" if INFO["dirty"] else ""),
             "committed %s" % (INFO["when"] or "?"),
             "branch %s" % (INFO["branch"] or "?"),
             INFO["root"],
             "",
             "This is the code THIS process loaded, read once at startup.",
             "After the hourly updater pulls, it keeps showing the old",
             "revision until the app restarts -- so if it does not match",
             "the checkout, the update has not been picked up yet."]
    return "\n".join(lines)
