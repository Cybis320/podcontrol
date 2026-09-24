"""Define the pod: the set of cameras to control.

A pod can come from (in resolution order):
  1. an explicit IP list         (CLI --cameras, or from_ips)
  2. a pod JSON file             (--pod pod.json, or $PODCONTROL_POD)
  3. an RMS Stations directory   (--stations-dir, or $PODCONTROL_STATIONS_DIR,
                                  else ~/source/Stations if it holds .config files)
  4. the built-in default        (192.168.42.101 .. .106)

Preferring the RMS Stations directory matters: it carries each camera's
data_dir, which is what lets the frame source read RMS's own saved frames
instead of opening a second RTSP session on a capturing camera.

A camera has an id, an ip (for the :9600 daemon + RTSP), an optional
data_dir (where RMS writes FramesFiles we can read for previews/metering) and
an optional mask_path: the station's RMS mask.bmp (0 = excluded). Metering
ignores masked pixels so a bright light in a masked zone (a street lamp, a
roof edge) cannot drive the pod's shared AE or the WB calibration.
"""
import os, re, glob, json

DEFAULT_IPS = ["192.168.42.%d" % n for n in range(101, 107)]


def rms_root():
    """The RMS checkout RMS runs from ($PODCONTROL_RMS_DIR, the installed RMS
    package's parent, or ~/source/RMS). RMS resolves a relative
    camera_settings_path against this directory (its cwd), not the station's."""
    d = os.environ.get("PODCONTROL_RMS_DIR")
    if d and os.path.isdir(os.path.expanduser(d)):
        return os.path.expanduser(d)
    try:
        import RMS
        d = os.path.dirname(os.path.dirname(os.path.abspath(RMS.__file__)))
        if os.path.isdir(d):
            return d
    except Exception:
        pass
    return os.path.expanduser("~/source/RMS")


def resolve_settings_path(spec, config_dir):
    """Mirror RMS: an explicit camera_settings_path is taken relative to the
    RMS root (its cwd) -- we also accept it next to the station config; the
    default is <config dir>/camera_settings.json, else RMS's own."""
    cands = []
    if spec:
        spec = os.path.expanduser(spec)
        if os.path.isabs(spec):
            cands.append(spec)
        else:
            cands += [os.path.join(config_dir, spec), os.path.join(rms_root(), spec)]
    cands += [os.path.join(config_dir, "camera_settings.json"),
              os.path.join(rms_root(), "camera_settings.json")]
    for c in cands:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return ""


class Station:
    def __init__(self, station_id, ip, data_dir="", mask_path="", platepar_path="",
                 settings_path=""):
        self.id = station_id
        self.ip = ip
        self.data_dir = os.path.expanduser(data_dir) if data_dir else ""
        self.mask_path = os.path.expanduser(mask_path) if mask_path else ""
        # RMS platepar: lets us place the sun in the frame (sun exclusion mask)
        self.platepar_path = os.path.expanduser(platepar_path) if platepar_path else ""
        # RMS camera_settings*.json: the authoritative day/night exposure lines,
        # used to hand a camera back EXACTLY as RMS configures it
        self.settings_path = os.path.expanduser(settings_path) if settings_path else ""

    def mode_cmd(self, mode):
        """The daemon exposure command RMS sends for `mode` ('day'|'night'),
        e.g. 'auto --min-exptime 30 --max-exptime 39970 --max-dgain 1024', or
        None if the settings file is missing or has no such line."""
        if not self.settings_path or not os.path.isfile(self.settings_path):
            return None
        try:
            data = json.load(open(self.settings_path))
        except Exception:
            return None
        for entry in data.get(mode, []) or []:
            if (isinstance(entry, list) and len(entry) >= 3 and entry[0] == "Isp"
                    and entry[1] in ("auto", "manual")):
                return " ".join(str(t) for t in entry[1:])
        return None

    def mode_colour_cmds(self, mode, keys=("ccm", "satu")):
        """The colour commands RMS replays for `mode` ('day'|'night'), in order,
        e.g. ['ccm off', 'satu 128'] for day and ['ccm on', 'satu 0'] for night.
        Taken from the station's camera_settings file, so handing colour back
        uses RMS's own authoritative values rather than a guess."""
        if not self.settings_path or not os.path.isfile(self.settings_path):
            return []
        try:
            data = json.load(open(self.settings_path))
        except Exception:
            return []
        out = []
        for entry in data.get(mode, []) or []:
            if isinstance(entry, list) and len(entry) >= 3 and entry[0] == "Isp" and entry[1] in keys:
                out.append(" ".join(str(t) for t in entry[1:]))
        return out

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
    return [Station(c.get("id") or c["ip"], c["ip"], c.get("data_dir", ""), c.get("mask", ""),
                    c.get("platepar", ""), c.get("settings", ""))
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
            # RMS: mask = <config dir>/<basename of [Capture] mask>, default mask.bmp
            mask_name = os.path.basename(_get(txt, "mask", "") or "mask.bmp")
            pp_name = os.path.basename(_get(txt, "platepar_name", "") or "platepar_cmn2010.cal")
            sp = resolve_settings_path(_get(txt, "camera_settings_path", ""),
                                       os.path.dirname(cfg))
            out.append(Station(sid, m.group(1), _get(txt, "data_dir", ""),
                               os.path.join(os.path.dirname(cfg), mask_name),
                               os.path.join(os.path.dirname(cfg), pp_name), sp))
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
    for cand in (os.path.expanduser("~/source/Stations"),):
        if os.path.isdir(cand):
            pod = discover_stations(cand)
            if pod:
                return pod
    return from_ips(DEFAULT_IPS)


# back-compat alias
def discover(stations_dir=None):
    return get_pod(stations_dir=stations_dir)


if __name__ == "__main__":
    for s in get_pod():
        print(s, "data_dir:", s.data_dir or "(none -> RTSP grab)",
              "mask:", s.mask_path if s.mask_path and os.path.isfile(s.mask_path) else "(none)",
              "platepar:", "ok" if s.platepar_path and os.path.isfile(s.platepar_path) else "(none)",
              "day:", s.mode_cmd("day") or "(none)")
