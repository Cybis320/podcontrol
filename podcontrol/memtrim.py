"""Give freed heap back to the OS.

podcontrol churns large numpy arrays every cycle: six frames decoded, masked,
metered and composited. glibc frees them promptly -- there is no leak, and RSS
plateaus either way -- but it keeps the freed pages on its own heap rather than
returning them, so the plateau sits about 110 MB higher than it needs to.
Measured over twelve cycles at the app's map size: 424 MB untrimmed against
315 MB trimmed, both flat.

Only malloc_trim does any work here. gc.collect() recovers nothing, because
numpy buffers are refcounted and die immediately rather than waiting on the
cycle collector, and it costs 32 ms against malloc_trim's 1.3 ms. So this does
not call it.

A no-op anywhere libc has no malloc_trim (musl, macOS); the import is resolved
once and the failure is remembered, so a miss costs nothing per cycle.
"""
import ctypes

_trim = False          # False = not looked up yet, None = unavailable


def _resolve():
    global _trim
    if _trim is False:
        try:
            _trim = ctypes.CDLL("libc.so.6").malloc_trim
        except (OSError, AttributeError):
            _trim = None
    return _trim


def trim():
    """Return free heap to the OS. True if it did something, False otherwise.

    ~1.3 ms, so it is cheap enough to call every cycle; it is pure bookkeeping
    in the allocator and never touches live objects."""
    f = _resolve()
    if f is None:
        return False
    try:
        return bool(f(0))
    except Exception:
        return False


def rss_mb():
    """This process's resident size, for logging. None where unavailable."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None
