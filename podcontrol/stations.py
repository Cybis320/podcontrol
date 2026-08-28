"""Define the pod: the set of cameras to control.

A pod can come from (in resolution order):
  1. an explicit IP list         (CLI --cameras, or from_ips)
  2. a pod JSON file             (--pod pod.json, or $PODCONTROL_POD)
  3. an RMS Stations directory   (--stations-dir, or $PODCONTROL_STATIONS_DIR)
  4. the built-in default        (192.168.42.101 .. .106)

A camera has an id, an ip (for the :9600 daemon + RTSP), and an optional
data_dir (where RMS writes FramesFiles we can read for previews/metering).
"""
import os, re, glob, json

DEFAULT_IPS = ["192.168.42.%d" % n for n in range(101, 107)]


class Station:
    def __init__(self, station_id, ip, data_dir=""):
        self.id = station_id
        self.ip = ip
        self.data_dir = os.path.expanduser(data_dir) if data_dir else ""

    @property
    def frames_dir(self):
        return os.path.join(self.data_dir, "FramesFiles") if self.data_dir else ""

    def __repr__(self):
        return "Station(%s, %s)" % (self.id, self.ip)


def from_ips(ips, id_prefix="cam"):
    """Build a pod from a list of IPs; id is the last octet (or prefix+idx)."""
    out = []
    for i, ip in enumerate(ips):
        last = ip.rsplit(".", 1)[-1]
        out.append(Station("%s%s" % (id_prefix, last), ip))
    return out


def parse_ip_spec(spec):
    """'192.168.42.101-106' or '192.168.42.101,.102' or full list -> [ips]."""
    ips = []
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r'(\d+\.\d+\.\d+\.)(\d+)-(\d+)$', part)
        if m:
            base, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            ips += ["%s%d" % (base, n) for n in range(a, b + 1)]
        elif re.match(r'\d+\.\d+\.\d+\.\d+$', part):
            ips.append(part)
        elif part.startswith("."):  # shorthand continuation of previous base
            base = ips[-1].rsplit(".", 1)[0] if ips else "192.168.42"
            ips.append(base + part)
    return ips


def from_pod_file(path):
    """JSON: {"cameras":[{"id":..,"ip":..,"data_dir":..}, ...]}."""
    data = json.load(open(os.path.expanduser(path)))
    return [Station(c.get("id") or c["ip"], c["ip"], c.get("data_dir", ""))
            for c in data.get("cameras", data if isinstance(data, list) else [])]


def discover_stations(stations_dir):
    """Read RMS station .config files under stations_dir."""
    def _get(txt, key, d=None):
        m = re.search(r'^\s*%s\s*:\s*(.+?)\s*$' % re.escape(key), txt, re.MULTILINE)
        return m.group(1).strip() if m else d
    out = []
    for cfg in sorted(glob.glob(os.path.join(os.path.expanduser(stations_dir), "*", ".config"))):
        txt = open(cfg).read()
        sid = _get(txt, "stationID") or os.path.basename(os.path.dirname(cfg))
        m = re.search(r'rtsp://(\d+\.\d+\.\d+\.\d+)', _get(txt, "device", "") or "")
        if m:
            out.append(Station(sid, m.group(1), _get(txt, "data_dir", "")))
    return out


def get_pod(ips=None, pod_file=None, stations_dir=None):
    ips = ips or os.environ.get("PODCONTROL_CAMERAS")
    """Resolve the pod from the highest-priority source that is provided/found."""
    if ips:
        return from_ips(parse_ip_spec(ips) if isinstance(ips, str) else ips)
    pod_file = pod_file or os.environ.get("PODCONTROL_POD")
    if pod_file and os.path.exists(os.path.expanduser(pod_file)):
        return from_pod_file(pod_file)
    for cand in ("pod.json", os.path.expanduser("~/.config/podcontrol/pod.json")):
        if os.path.exists(cand):
            return from_pod_file(cand)
    stations_dir = stations_dir or os.environ.get("PODCONTROL_STATIONS_DIR")
    if stations_dir and os.path.isdir(os.path.expanduser(stations_dir)):
        return discover_stations(stations_dir)
    return from_ips(DEFAULT_IPS)


# back-compat alias
def discover(stations_dir=None):
    return get_pod(stations_dir=stations_dir)


if __name__ == "__main__":
    for s in get_pod():
        print(s, "data_dir:", s.data_dir or "(none -> RTSP grab)")
