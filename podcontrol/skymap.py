"""All-sky composite: the pod's cameras projected together on one sky map.

For every pixel of a sky map -- a zenith-centred azimuthal-equidistant view
(the "all-sky camera" look: north up, east LEFT, as when looking up) or an
az/alt panorama -- each station's RMS platepar says which sensor pixel looks
in that direction (RMS raDecToXYPP at the platepar's own epoch: a fixed
camera's pixel <-> alt/az map never changes, so no ephemeris is involved).
Directions behind the camera fold back into the frame through the gnomonic
projection, so every candidate is verified by a round trip through
xyToRaDecPP (genuine pixels agree to ~0.001 deg, folded ones by >100 deg).

The composite is one cv2.remap per camera on the newest COMPLETE frame set
(frames.newest_complete_set: one capture time for all cameras), blended
where fields overlap with feathered weights (0 at a frame edge, 1 a few
degrees in) so seams fade; pixels under the station's RMS mask get a tiny
weight so an unmasked neighbour wins there but the area is still filled when
nothing else covers it.

Cost. The lookup tables take ~1.5 s per station at 900 px and are cached on
disk keyed by the platepar, the mask and the map parameters. SkyRenderer
then keeps everything static precomputed (normalised blend weights,
footprint contours) and re-blends only when the frame set changes (every
~50 s on a capturing pod: six INTER_AREA downscales + six remaps over each
camera's bounding box, ~25 ms); a cycle with the same set costs one copy of
the cached composite plus the markers and text (~3 ms). A frame is
downscaled once per file to the map's scale (cached), never decoded twice.

    python -m podcontrol.skymap                          # -> /tmp/podcontrol/skymap.png
    python -m podcontrol.skymap --size 1000 --loop 5 --out sky.png
    python -m podcontrol.skymap --proj pano --compass    # panorama, east to the right
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os, math, time, json, hashlib, threading
import numpy as np
import cv2

from podcontrol import frames, sunmask

CACHE_DIR = os.path.expanduser("~/.cache/podcontrol/skymap")
LUT_VERSION = 2
BACKGROUND = (45, 45, 45)          # sky with no camera covering it (BGR)
GRID_COLOUR = (90, 90, 90)
TEXT_COLOUR = (235, 235, 235)
DIM_COLOUR = (120, 120, 120)
ROUND_TRIP_TOL_DEG = 0.5
# one colour per pod position (BGR): N, E, S, W, N-zenith, S-zenith on the US05 pod
PALETTE = [(80, 80, 255), (0, 165, 255), (0, 220, 255), (100, 230, 100), (255, 200, 80), (255, 120, 255)]
# overlay layers (the maps frames.mask_for(..., layers=True) + highlight_maps
# give per camera) with their tints -- the same colours as the preview tiles
OVERLAY_KEYS = ("static", "sun", "flare", "moon", "clip")
OVERLAY_TINTS = {"static": ((60, 60, 220), 0.45), "sun": ((40, 190, 255), 0.45),
                 "flare": ((255, 110, 170), 0.45), "moon": ((255, 200, 140), 0.45),
                 "clip": ((255, 0, 255), 0.75)}
DRIVE_COLOURS = {"clipping": (214, 90, 255), "headroom": (255, 224, 90), "at target": (118, 199, 127)}


def _sep_deg(alt1, az1, alt2, az2):
    a1, z1, a2, z2 = (np.radians(v) for v in (alt1, az1, alt2, az2))
    c = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(z1 - z2)
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


class SkyGrid:
    """Geometry of the output map: pixel <-> (alt, az).

    proj 'polar': zenith at the centre, alt_min at the rim, radius linear in
    zenith distance (azimuthal equidistant). North up; east LEFT (sky view,
    imagery keeps its handedness) unless compass=True (east right, a map).
    proj 'pano': equirectangular, az left->right S W N E S (north centred),
    alt top->bottom 90 -> alt_min."""

    def __init__(self, size=800, proj="polar", alt_min=0.0, compass=False, margin=None):
        self.size, self.proj, self.alt_min, self.compass = int(size), proj, float(alt_min), bool(compass)
        if proj == "polar":
            self.w = self.h = self.size
            self.margin = 24 if margin is None else int(margin)
            self.R = self.size / 2.0 - self.margin
            self.cx = self.cy = (self.size - 1) / 2.0
        elif proj == "pano":
            self.w = self.size
            self.h = max(1, int(round(self.size * (90.0 - self.alt_min) / 360.0)))
            self.margin = 0
        else:
            raise ValueError("proj must be 'polar' or 'pano'")
        self._inside = None

    def params(self):
        return {"size": self.size, "proj": self.proj, "alt_min": self.alt_min, "compass": self.compass,
                "margin": self.margin}

    @property
    def px_per_deg(self):
        """Radial (polar) / vertical (pano) scale of the map."""
        if self.proj == "polar":
            return self.R / (90.0 - self.alt_min)
        return self.h / (90.0 - self.alt_min)

    @property
    def inside(self):
        """Bool (h, w): pixels that belong to the sky map (the horizon disc
        for 'polar'). Cached; the alt/az arrays themselves are not kept."""
        if self._inside is None:
            self._inside = self.altaz()[2]
        return self._inside

    def altaz(self):
        """(alt, az, inside): (h, w) arrays in degrees + the inside mask."""
        ys, xs = np.mgrid[0:self.h, 0:self.w].astype(np.float64)
        if self.proj == "polar":
            dx, dy = xs - self.cx, ys - self.cy
            r = np.hypot(dx, dy)
            alt = 90.0 - r / self.px_per_deg
            ex = dx if self.compass else -dx
            az = np.degrees(np.arctan2(ex, -dy)) % 360.0
            return alt, az, r <= self.R
        az = (180.0 + (xs + 0.5) / self.w * 360.0) % 360.0
        alt = 90.0 - (ys + 0.5) / self.px_per_deg
        return alt, az, np.ones((self.h, self.w), bool)

    def xy(self, alt, az):
        """Map pixel (float x, y) of a direction; arrays or scalars."""
        alt, az = np.asarray(alt, float), np.asarray(az, float)
        if self.proj == "polar":
            r = (90.0 - alt) * self.px_per_deg
            s = np.sin(np.radians(az)) * (1.0 if self.compass else -1.0)
            return self.cx + r * s, self.cy - r * np.cos(np.radians(az))
        x = ((az - 180.0) % 360.0) / 360.0 * self.w - 0.5
        y = (90.0 - alt) * self.px_per_deg - 0.5
        return x, y


class CamLUT:
    """Per-station lookup: for each map pixel the (downscaled) frame pixel to
    sample, and its blend weight (0 = the camera does not see it). The maps
    are stored cropped to the footprint's bounding box (bbox = y0, y1, x0, x1
    in map pixels) so the per-camera remap + accumulate touch only the ~1/7
    of the map the camera sees."""

    def __init__(self, map_x, map_y, weight, bbox, shape, factor, small, centre_xy, fov_xy):
        self.map_x, self.map_y, self.weight = (np.ascontiguousarray(a) for a in (map_x, map_y, weight))
        self.bbox, self.shape = tuple(int(v) for v in bbox), tuple(int(v) for v in shape)
        self.factor, self.small = int(factor), tuple(int(v) for v in small)
        self.centre_xy, self.fov_xy = tuple(float(v) for v in centre_xy), tuple(float(v) for v in fov_xy)
        self.covered_crop = self.weight > 0
        self.covered = np.zeros(self.shape, bool)
        y0, y1, x0, x1 = self.bbox
        self.covered[y0:y1, x0:x1] = self.covered_crop
        self._contours = None

    @property
    def contours(self):
        """Footprint outline(s) in map coordinates (computed once)."""
        if self._contours is None:
            cs, _ = cv2.findContours(self.covered.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            self._contours = cs
        return self._contours

    @classmethod
    def from_full(cls, map_x, map_y, weight, factor, small, centre_xy, fov_xy):
        ys, xs = np.nonzero(weight > 0)
        bbox = (int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1) if ys.size else (0, 0, 0, 0)
        y0, y1, x0, x1 = bbox
        return cls(map_x[y0:y1, x0:x1], map_y[y0:y1, x0:x1], weight[y0:y1, x0:x1], bbox, weight.shape,
                   factor, small, centre_xy, fov_xy)

    def save(self, path):
        np.savez_compressed(path, map_x=self.map_x, map_y=self.map_y, weight=self.weight,
                            bbox=np.array(self.bbox), shape=np.array(self.shape), factor=self.factor,
                            small=np.array(self.small), centre_xy=np.array(self.centre_xy),
                            fov_xy=np.array(self.fov_xy))

    @classmethod
    def load(cls, path):
        z = np.load(path)
        return cls(z["map_x"], z["map_y"], z["weight"], z["bbox"], z["shape"], int(z["factor"]),
                   z["small"], z["centre_xy"], z["fov_xy"])


def _lut_key(station, grid, feather_deg, mask_weight):
    def _stamp(p):
        try:
            s = os.stat(p)
            return [int(s.st_mtime), s.st_size]
        except OSError:
            return None
    d = {"v": LUT_VERSION, "id": station.id, "pp": _stamp(station.platepar_path),
         "mask": _stamp(station.mask_path) if station.mask_path else None,
         "grid": grid.params(), "feather": round(float(feather_deg), 3), "mask_weight": round(float(mask_weight), 4)}
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def build_lut(station, grid, feather_deg=2.5, mask_weight=0.02):
    """Compute the station's lookup table for this grid (a few seconds)."""
    from RMS.Formats.Platepar import Platepar
    from RMS.Astrometry.ApplyAstrometry import xyToRaDecPP, raDecToXYPP, getFOVSelectionRadius
    from RMS.Astrometry.Conversions import altAz2RADec, raDec2AltAz
    pp = Platepar()
    pp.read(station.platepar_path, use_flat=None)
    W, H = int(pp.X_res), int(pp.Y_res)
    jd = float(pp.JD)                      # any epoch: pixel <-> alt/az is fixed

    def to_altaz(x, y):
        _, ra, dec, _ = xyToRaDecPP(np.full(np.size(x), jd), np.atleast_1d(x).astype(float),
                                    np.atleast_1d(y).astype(float), np.ones(np.size(x)), pp,
                                    extinction_correction=False, jd_time=True, precompute_pointing_corr=True)
        az, alt = raDec2AltAz(ra, dec, jd, pp.lat, pp.lon)
        return alt, az

    alt_c, az_c = (float(v[0]) for v in to_altaz([W / 2.0], [H / 2.0]))
    fov_r = float(getFOVSelectionRadius(pp)) + 3.0

    alt, az, inside = grid.altaz()
    sep = _sep_deg(alt, az, alt_c, az_c)
    idx = np.flatnonzero(inside & (sep < fov_r))
    map_x = np.full(alt.shape, -1.0, np.float32)
    map_y = np.full(alt.shape, -1.0, np.float32)
    weight = np.zeros(alt.shape, np.float32)
    factor = max(1, int(math.floor(float(pp.F_scale) / grid.px_per_deg)))
    small = (int(math.ceil(W / factor)), int(math.ceil(H / factor)))
    cx_map, cy_map = grid.xy(alt_c, az_c)
    if idx.size:
        a, z = alt.ravel()[idx], az.ravel()[idx]
        ra, dec = altAz2RADec(z, a, jd, pp.lat, pp.lon)
        x, y = raDecToXYPP(ra, dec, jd, pp)
        ok = (x >= 0) & (x <= W - 1) & (y >= 0) & (y <= H - 1) & np.isfinite(x) & np.isfinite(y)
        if ok.any():
            a2, z2 = to_altaz(x[ok], y[ok])
            good = _sep_deg(a[ok], z[ok], a2, z2) < ROUND_TRIP_TOL_DEG
            sel = idx[ok][good]
            xs, ys = x[ok][good], y[ok][good]
            # feather: 0 at the frame edge -> 1 feather_deg inside (smoothstep)
            d_edge = np.minimum(np.minimum(xs, W - 1 - xs), np.minimum(ys, H - 1 - ys))
            wgt = np.clip(d_edge / max(1.0, feather_deg * float(pp.F_scale)), 0.0, 1.0)
            wgt = wgt * wgt * (3.0 - 2.0 * wgt)
            wgt = np.maximum(wgt, 1e-3)              # the edge row itself still counts a little
            keep = frames.load_mask(station)
            if keep is not None and keep.shape == (H, W):
                masked = ~keep[np.clip(np.rint(ys).astype(int), 0, H - 1), np.clip(np.rint(xs).astype(int), 0, W - 1)]
                wgt = np.where(masked, wgt * mask_weight, wgt)
            map_x.ravel()[sel] = ((xs + 0.5) / factor - 0.5).astype(np.float32)
            map_y.ravel()[sel] = ((ys + 0.5) / factor - 0.5).astype(np.float32)
            weight.ravel()[sel] = wgt.astype(np.float32)
    return CamLUT.from_full(map_x, map_y, weight, factor, small, (float(cx_map), float(cy_map)), (alt_c, az_c))


_LUTS = {}
_BUILD_LOCK = threading.Lock()


def lut_for(station, grid, feather_deg=2.5, mask_weight=0.02, cache=True):
    """The station's LUT for this grid: memory -> disk cache -> build. None
    when the station has no readable platepar. Thread-safe (one build at a
    time)."""
    if not station.platepar_path or not os.path.isfile(station.platepar_path):
        return None
    key = _lut_key(station, grid, feather_deg, mask_weight)
    hit = _LUTS.get(key)
    if hit is not None:
        return hit
    with _BUILD_LOCK:
        hit = _LUTS.get(key)
        if hit is not None:
            return hit
        path = os.path.join(CACHE_DIR, "%s_%s.npz" % (station.id, key))
        lut = None
        if cache and os.path.isfile(path):
            try:
                lut = CamLUT.load(path)
            except Exception:
                lut = None
        if lut is None:
            try:
                t0 = time.time()
                lut = build_lut(station, grid, feather_deg, mask_weight)
                if cache:
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    lut.save(path)
                print("skymap: %s LUT built in %.1fs (%.1f%% of the map)" % (
                    station.id, time.time() - t0, 100.0 * lut.covered.mean()))
            except Exception as e:
                print("skymap: %s LUT failed (%s)" % (station.id, e))
                return None
        _LUTS[key] = lut
        return lut


# Frames downscaled to the map's scale, once per saved file (immutable) and
# size; shared by every renderer that uses the same factor.
_SMALL = {}
SMALL_MAX = 24


def small_frame(img, path, size):
    """img downscaled to size (w, h); img may be None when path is given
    (decoded only on a cache miss). None if nothing could be read."""
    key = (path, size) if path else None
    if key:
        hit = _SMALL.get(key)
        if hit is not None:
            return hit
    if img is None:
        img = frames.imread_cached(path) if path else None
        if img is None:
            return None
    small = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    if small.ndim == 2:
        small = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    if key:
        if len(_SMALL) >= SMALL_MAX:
            _SMALL.pop(next(iter(_SMALL)))
        _SMALL[key] = small
    return small


def pod_frames(pod, decode=True):
    """({station_id: bgr_or_None}, {station_id: path}, capture_epoch_or_None,
    coherent): the newest complete set when there is one, else each
    station's newest saved frame (coherent=False: mixed capture times).
    decode=False leaves the images None (compose() decodes a file only when
    its downscaled version is not cached yet)."""
    slot, paths = frames.newest_complete_set(pod)
    if slot:
        imgs = {sid: (frames.imread_cached(p) if decode else None) for sid, p in paths.items()}
        return imgs, paths, slot, True
    imgs, paths, ts = {}, {}, []
    for st in pod:
        img, src, t, p = frames.frame_for(st, allow_grab=False, with_path=True)
        if img is not None:
            imgs[st.id] = img
            paths[st.id] = p
            if t:
                ts.append(t)
    return imgs, paths, (max(ts) if ts else None), False


def _text(img, s, xy, scale=0.45, colour=TEXT_COLOUR, thick=1):
    x, y = int(round(xy[0])), int(round(xy[1]))
    cv2.putText(img, s, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thick, cv2.LINE_AA)


def draw_grid(img, grid):
    """Alt circles / az lines and the cardinal points."""
    if grid.proj == "polar":
        c = (int(round(grid.cx)), int(round(grid.cy)))
        for a in (0.0, 30.0, 60.0):
            if a >= grid.alt_min:
                cv2.circle(img, c, int(round((90 - a) * grid.px_per_deg)), GRID_COLOUR, 1, cv2.LINE_AA)
        for azd in range(0, 360, 30):
            x, y = grid.xy(grid.alt_min, azd)
            x2, y2 = grid.xy(60.0, azd)
            cv2.line(img, (int(round(x2)), int(round(y2))), (int(round(x)), int(round(y))), GRID_COLOUR, 1, cv2.LINE_AA)
        for name, azd in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            x, y = grid.xy(grid.alt_min, azd)
            dx, dy = x - grid.cx, y - grid.cy
            n = math.hypot(dx, dy) or 1.0
            _text(img, name, (x + dx / n * 12 - 5, y + dy / n * 12 + 5), 0.5, TEXT_COLOUR, 1)
    else:
        for a in (30.0, 60.0):
            if a >= grid.alt_min:
                _, y = grid.xy(a, 0)
                cv2.line(img, (0, int(round(y))), (grid.w - 1, int(round(y))), GRID_COLOUR, 1)
                _text(img, "%d" % a, (4, y - 3), 0.4)
        for name, azd in (("S", 180), ("W", 270), ("N", 0), ("E", 90)):
            x, _ = grid.xy(90.0, azd)
            x = int(round(x))
            cv2.line(img, (x, 0), (x, grid.h - 1), GRID_COLOUR, 1)
            _text(img, name, (x + 4, 14), 0.5)


def draw_bodies(img, pod, grid, t):
    """Sun (yellow) and moon (pale blue) markers at epoch t, when above alt_min."""
    st = next((s for s in pod if sunmask.available(s)), None)
    if st is None or t is None:
        return
    for body, col in (("sun", (0, 230, 255)), ("moon", (255, 220, 170))):
        b = sunmask.body_altaz(st, body, t)
        if not b or b["alt"] < grid.alt_min:
            continue
        x, y = grid.xy(b["alt"], b["az"])
        p = (int(round(x)), int(round(y)))
        cv2.circle(img, p, 7, col, 1, cv2.LINE_AA)
        cv2.line(img, (p[0] - 11, p[1]), (p[0] + 11, p[1]), col, 1, cv2.LINE_AA)
        cv2.line(img, (p[0], p[1] - 11), (p[0], p[1] + 11), col, 1, cv2.LINE_AA)
        _text(img, "%s %.0f" % (body, b["alt"]) + (" %.0f%%" % b["phase"] if "phase" in b else ""),
              (x + 10, y - 8), 0.4, col)


class SkyRenderer:
    """The pod on one grid, with three cache levels so a cycle in which
    nothing changed costs almost nothing:

      base    = composite + static decorations (grid, footprints, ids);
                re-blended only when the frame set (the saved files) changes;
      tinted  = base + overlay tints; redone when the base or any overlay map
                changes (the sun zone moves in 30 s buckets, the clip map per
                frame), the per-camera warps being reused while their maps are
                the same arrays;
      frame   = a copy of the above + the dynamic text (telemetry, driving
                badge, sun/moon, caption) -- every cycle, ~5 ms.

    Thread-safe: one render at a time."""

    def __init__(self, pod, grid, feather_deg=2.5, mask_weight=0.02):
        self.pod, self.grid = list(pod), grid
        self.by_id = {st.id: st for st in self.pod}
        self.feather_deg, self.mask_weight = float(feather_deg), float(mask_weight)
        self.luts = {}
        self._wcache = {}              # frozenset(ids) -> (wnorm per id, covered, covered fraction)
        self._base_key, self._base, self._used = None, None, []
        self._static_small = {}        # sid -> small bool of the station's RMS mask (static)
        self._ov_small = {}            # sid -> (identity tuple, pinned maps, warped flags, version)
        self._ov_version = 0
        self._ov_key, self._ov = None, None
        self._lock = threading.Lock()

    @property
    def ready(self):
        return any(v is not None for v in self.luts.values())

    def ensure(self):
        """Load or build every station's LUT (seconds per station the first
        time, then from the disk cache). Call from a background thread."""
        for st in self.pod:
            if st.id not in self.luts:
                self.luts[st.id] = lut_for(st, self.grid, self.feather_deg, self.mask_weight)
        return self.ready

    def _weights(self, ids):
        """Normalised blend weight per camera for this set of cameras (static;
        cached), the covered mask and the covered fraction of the sky."""
        hit = self._wcache.get(ids)
        if hit is not None:
            return hit
        h, w = self.grid.h, self.grid.w
        wsum = np.zeros((h, w), np.float32)
        for sid in ids:
            lut = self.luts[sid]
            y0, y1, x0, x1 = lut.bbox
            wsum[y0:y1, x0:x1] += lut.weight
        cov = wsum > 0
        inv = np.zeros_like(wsum)
        inv[cov] = 1.0 / wsum[cov]
        wn = {}
        for sid in ids:
            lut = self.luts[sid]
            y0, y1, x0, x1 = lut.bbox
            wn[sid] = (lut.weight * inv[y0:y1, x0:x1])[:, :, None]
        inside = self.grid.inside
        frac = float(cov[inside].mean()) if inside.any() else 0.0
        if len(self._wcache) > 8:
            self._wcache.clear()
        self._wcache[ids] = (wn, cov, frac)
        return self._wcache[ids]

    def compose(self, imgs, paths=None, show_grid=True, outlines=True):
        """(bgr, used_ids): the blended map for these frames ({id: bgr or
        None} + {id: path}) with the static decorations; cached on the
        frames' file paths so an unchanged set costs nothing."""
        paths = paths or {}
        ids = [st.id for st in self.pod if self.luts.get(st.id) is not None
               and (imgs.get(st.id) is not None or paths.get(st.id))]
        key = ((tuple((sid, paths.get(sid)) for sid in ids), show_grid, outlines)
               if ids and all(paths.get(sid) for sid in ids) else None)
        if key is not None and key == self._base_key:
            return self._base, self._used
        h, w = self.grid.h, self.grid.w
        used = []
        acc = None
        for sid in ids:
            lut = self.luts[sid]
            small = small_frame(imgs.get(sid), paths.get(sid), lut.small)
            if small is None:
                continue
            if acc is None:
                acc = np.zeros((h, w, 3), np.float32)
            used.append(sid)
        if used:
            wn, cov, _ = self._weights(frozenset(used))
            for sid in used:
                lut = self.luts[sid]
                small = small_frame(imgs.get(sid), paths.get(sid), lut.small)
                warped = cv2.remap(small, lut.map_x, lut.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                y0, y1, x0, x1 = lut.bbox
                acc[y0:y1, x0:x1] += warped * wn[sid]
            # normalised weights sum to 1 where covered: the blend is a
            # weighted mean, never above 255 (bar float rounding)
            np.minimum(acc, 255.0, out=acc)
            out = acc.astype(np.uint8)
            out[~cov] = BACKGROUND
        else:
            out = np.empty((h, w, 3), np.uint8)
            out[:] = BACKGROUND
        out[~self.grid.inside] = 0
        if show_grid:
            draw_grid(out, self.grid)
        if outlines:
            self._footprints(out, used)
        self._base_key, self._base, self._used = key, out, used
        return out, used

    def _flags_for(self, sid, lay):
        """One camera's overlay bits (OVERLAY_KEYS order), warped onto the
        map and cropped to its bbox. Cached while the layer maps are the same
        arrays (they are, between sun buckets / new frames); the previous
        maps are pinned so a recycled id can never match."""
        lut = self.luts[sid]
        dyn = OVERLAY_KEYS[1:]
        ident = tuple(id(lay.get(k)) for k in dyn)
        hit = self._ov_small.get(sid)
        if hit is not None and hit[0] == ident:
            return hit[2], hit[3]
        fs = None
        st_small = self._static_small.get(sid)
        if st_small is None:
            keep = frames.load_mask(self.by_id[sid]) if sid in self.by_id else None
            if keep is not None:
                m8 = np.ascontiguousarray(~keep).view(np.uint8) * 255
                st_small = cv2.resize(m8, lut.small, interpolation=cv2.INTER_AREA) > 0
            else:
                st_small = False
            self._static_small[sid] = st_small
        if st_small is not False:
            fs = np.where(st_small, np.uint8(1), np.uint8(0))
        for bit, key in enumerate(dyn, start=1):
            m = lay.get(key)
            if m is None:
                continue
            m8 = np.ascontiguousarray(m).view(np.uint8) * 255
            ms = cv2.resize(m8, lut.small, interpolation=cv2.INTER_AREA) > 0     # any coverage
            if fs is None:
                fs = np.zeros(ms.shape, np.uint8)
            fs[ms] |= np.uint8(1 << bit)
        warped = None
        if fs is not None:
            warped = cv2.remap(fs, lut.map_x, lut.map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
            warped[~lut.covered_crop] = 0
        self._ov_version += 1
        self._ov_small[sid] = (ident, [lay.get(k) for k in dyn], warped, self._ov_version)
        return warped, self._ov_version

    def tinted(self, base, layers):
        """base + the overlay tints (what the metering ignores / reacts to):
        layers = {station_id: {"sun"|"flare"|"moon"|"clip": full-res bool}}
        (frames.mask_for(..., layers=True) + highlight_maps; the static RMS
        mask is taken from the station itself). Cached until the base or a
        map changes."""
        parts = []
        for sid, lay in layers.items():
            if self.luts.get(sid) is None or lay is None:
                continue
            warped, ver = self._flags_for(sid, lay)
            if warped is not None:
                parts.append((sid, warped, ver))
        key = (self._base_key, tuple((sid, ver) for sid, _, ver in parts))
        if key == self._ov_key and self._ov is not None:
            return self._ov
        out = base.copy()
        if parts:
            flags = np.zeros((self.grid.h, self.grid.w), np.uint8)
            for sid, warped, _ in parts:
                y0, y1, x0, x1 = self.luts[sid].bbox
                flags[y0:y1, x0:x1] |= warped
            for bit, key_ in enumerate(OVERLAY_KEYS):
                m = (flags & (1 << bit)) > 0
                if m.any():
                    col, a = OVERLAY_TINTS[key_]
                    out[m] = ((1 - a) * out[m] + a * np.array(col, np.float32)).astype(np.uint8)
        self._ov_key, self._ov = key, out
        return out

    def _footprints(self, out, used):
        """Static: outline each camera in its colour (dim when it has no frame)."""
        for i, st in enumerate(self.pod):
            lut = self.luts.get(st.id)
            if lut is None:
                continue
            col = PALETTE[i % len(PALETTE)]
            if st.id not in used:
                col = tuple(int(v * 0.4) for v in col)
            cv2.drawContours(out, lut.contours, -1, col, 1, cv2.LINE_AA)

    def _label_xy(self, lut):
        x, y = lut.centre_xy
        return min(max(x - 22, 4), self.grid.w - 120), y + 4

    def decorate(self, out, t=None, used=None, telem=None, drive=None, coherent=True, covered=None,
                 bodies=True, title=True, labels=True):
        """Dynamic decorations, drawn in place (on top of the tints, so they
        stay legible): each camera's id, exposure / total gain and driving
        badge at its field centre, sun/moon markers, the caption.

        `labels` is the text half of the FOV overlay and goes with `outlines`;
        the caption stays either way, so the frame time is always readable."""
        used = used if used is not None else self._used
        for i, st in enumerate(self.pod if labels else []):
            lut = self.luts.get(st.id)
            if lut is None:
                continue
            col = PALETTE[i % len(PALETTE)]
            if st.id not in used:
                col = tuple(int(v * 0.4) for v in col)
            x, y = self._label_xy(lut)
            _text(out, st.id, (x, y), 0.42, col, 1)
            tel = (telem or {}).get(st.id)
            if tel is not None:
                if tel.get("online"):
                    gain = ((tel.get("again_x") or 1.0) * (tel.get("dgain_x") or 1.0)
                            * (tel.get("ispdgain_x") or 1.0))
                    _text(out, "%sus  x%.2f" % (tel.get("exp_us", "?"), gain), (x, y + 14), 0.36, TEXT_COLOUR, 1)
                else:
                    _text(out, "no daemon", (x, y + 14), 0.36, DIM_COLOUR, 1)
            d = (drive or {}).get(st.id)
            if d and d[0]:
                _text(out, "<< DRIVING: %s" % (d[1] or ""), (x, y + 28), 0.36,
                      DRIVE_COLOURS.get(d[1], (214, 90, 255)), 1)
        if bodies:
            draw_bodies(out, self.pod, self.grid, t)
        if title:
            if t:
                when = "%s UTC (%ds old)" % (time.strftime("%H:%M:%S", time.gmtime(t)), max(0, int(time.time() - t)))
            else:
                when = "no frame time"
            _text(out, "%s%s  %d/%d cameras%s" % (
                when, "" if coherent else " MIXED TIMES", len(used), len(self.pod),
                ("  sky covered %.0f%%" % (100 * covered)) if covered is not None else ""),
                  (6, self.grid.h - 8), 0.45)

    def render(self, imgs=None, paths=None, t=None, coherent=True, telem=None, drive=None, layers=None,
               show_grid=True, outlines=True, **kw):
        """Full render: (bgr, info). imgs/paths default to the newest complete
        set; layers (see tinted) are optional. `outlines` draws the camera
        footprints and pairs with `labels` (passed through to decorate): both
        are the FOV overlay and are normally switched together."""
        with self._lock:
            if imgs is None:
                imgs, paths, t, coherent = pod_frames(self.pod, decode=False)
            self.ensure()
            base, used = self.compose(imgs, paths, show_grid, outlines)
            out = (self.tinted(base, layers) if layers else base).copy()
            covered = self._weights(frozenset(used))[2] if used else 0.0
            self.decorate(out, t, used, telem, drive, coherent, covered, **kw)
            return out, {"t": t, "used": used, "coherent": coherent, "covered": covered}


def render(pod, grid=None, imgs=None, t=None, feather_deg=2.5, mask_weight=0.02, **kw):
    """One-shot composite of the pod (a fresh SkyRenderer; the GUI keeps one)."""
    r = SkyRenderer(pod, grid or SkyGrid(), feather_deg, mask_weight)
    return r.render(imgs, t=t, **kw)


if __name__ == "__main__":
    import argparse
    from podcontrol.stations import get_pod
    ap = argparse.ArgumentParser(description="All-sky composite of the pod from the newest frame set.")
    ap.add_argument("--size", type=int, default=800, help="map size in px (polar: diameter; pano: width)")
    ap.add_argument("--proj", choices=("polar", "pano"), default="polar")
    ap.add_argument("--alt-min", type=float, default=0.0, help="lowest altitude shown (deg)")
    ap.add_argument("--compass", action="store_true", help="east to the right (map view) instead of left (sky view)")
    ap.add_argument("--feather", type=float, default=2.5, help="blend feather at frame edges (deg)")
    ap.add_argument("--mask-weight", type=float, default=0.02, help="blend weight of RMS-masked pixels (0 = never shown)")
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--no-outline", action="store_true",
                    help="no FOV overlay: neither footprint outlines nor camera labels")
    ap.add_argument("--no-cache", action="store_true", help="rebuild the lookup tables")
    ap.add_argument("--out", default=os.path.join(frames.SCRATCH, "skymap.png"))
    ap.add_argument("--loop", type=float, default=0, help="re-render every N seconds (0 = once)")
    args = ap.parse_args()
    pod = get_pod()
    grid = SkyGrid(args.size, args.proj, args.alt_min, args.compass)
    if args.no_cache:
        for st in pod:
            key = _lut_key(st, grid, args.feather, args.mask_weight)
            _LUTS.pop(key, None)
            p = os.path.join(CACHE_DIR, "%s_%s.npz" % (st.id, key))
            if os.path.isfile(p):
                os.remove(p)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    renderer = SkyRenderer(pod, grid, args.feather, args.mask_weight)
    last_t = None
    while True:
        t0 = time.time()
        img, info = renderer.render(show_grid=not args.no_grid, outlines=not args.no_outline,
                                    labels=not args.no_outline)
        dt = time.time() - t0
        if info["t"] != last_t or not args.loop:
            cv2.imwrite(args.out, img)
            last_t = info["t"]
            print("wrote %s  (%dx%d, %d/%d cameras, frames %s%s, sky covered %.0f%%, render %.0f ms)" % (
                args.out, img.shape[1], img.shape[0], len(info["used"]), len(pod),
                time.strftime("%H:%M:%S UTC", time.gmtime(info["t"])) if info["t"] else "?",
                "" if info["coherent"] else " MIXED TIMES", 100 * info["covered"], 1000 * dt))
        if not args.loop:
            break
        time.sleep(max(1.0, args.loop))
