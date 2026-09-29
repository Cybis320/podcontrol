"""Individual AE: every camera of the pod driven by its OWN controller.

The shared AE drives the pod as one camera: the darkest need wins, so the camera looking at
the sun darkens all six. Now that every saved frame carries its own exposure (RMS
save_frame_metadata) and a pod view can be brought back to one common exposure afterwards
(podcontrol.radiance / the sky view's "const exp"), each camera can expose for its own scene.

IndividualAE runs one SharedAE per camera, each on a one-camera PodController, and presents
SharedAE's interface to the app. Each camera therefore keeps ALL of the shared logic, applied
to itself alone: its own metering (camera meter clean zones or frames), highlight priority,
slow slews, its own WB rung (s from 1.0 down to 1/max gain) and the night latch -- at night
every camera latches to the same RMS night line, so the pod is identical there. The one
AEConfig object is shared, so the GUI's settings reach all six.
"""
import statistics

from podcontrol.podctl import PodController
from podcontrol.sharedae import SharedAE, AEConfig


class IndividualAE(object):
    individual = True

    def __init__(self, pod, cfg=None):
        self.pod = pod
        self.cfg = cfg or AEConfig()
        self.subs = {st.id: SharedAE(PodController([st]), cfg=self.cfg) for st in pod.stations}
        self.last = {}

    # ---- helpers
    def _each(self):
        return list(self.subs.items())

    @staticmethod
    def _only(d, sid):
        return {sid: d[sid]} if d and sid in d else {}

    def _med(self, name, default=None):
        v = [getattr(s, name) for s in self.subs.values() if getattr(s, name, None) is not None]
        return statistics.median(v) if v else default

    # ---- attributes the app reads or writes on the controller
    @property
    def li(self):
        return self._med("li", 0.0)

    @li.setter
    def li(self, v):
        for s in self.subs.values():
            s.li = v

    @property
    def target(self):
        return self._med("target", 0.0)

    @target.setter
    def target(self, v):
        for s in self.subs.values():
            s.target = v

    @property
    def wb_base(self):
        return next((s.wb_base for s in self.subs.values() if s.wb_base), None)

    @wb_base.setter
    def wb_base(self, v):
        for s in self.subs.values():
            s.wb_base = v

    @property
    def _applied_wb_scale(self):
        return min(s._applied_wb_scale for s in self.subs.values())

    @_applied_wb_scale.setter
    def _applied_wb_scale(self, v):
        for s in self.subs.values():
            s._applied_wb_scale = v

    @property
    def t_apply(self):
        return max(s.t_apply for s in self.subs.values())

    @property
    def t_seed(self):
        return max(s.t_seed for s in self.subs.values())

    @property
    def latched(self):
        return all(getattr(s, "latched", False) for s in self.subs.values())

    @property
    def sun_alt(self):
        return next(iter(self.subs.values())).sun_alt

    @property
    def state(self):
        return next(iter(self.subs.values())).state

    def _max_li(self):
        return next(iter(self.subs.values()))._max_li()

    def _min_li(self):
        return next(iter(self.subs.values()))._min_li()

    def wb_scale_at(self, t):
        return min(s.wb_scale_at(t) for s in self.subs.values())

    # ---- the controller interface
    def update_sun(self, alt_deg, rising):
        for s in self.subs.values():
            s.update_sun(alt_deg, rising)

    def takeover(self, poll):
        for sid, s in self._each():
            s.takeover(self._only(poll, sid))

    def release(self, timeout=5.0):
        out = {}
        for sid, s in self._each():
            try:
                out[sid] = s.release(timeout=timeout)
            except Exception as e:
                out[sid] = e
        return out

    def note_cameras(self, poll):
        return any([s.note_cameras(self._only(poll, sid)) for sid, s in self._each()])

    def repin_needed(self, poll, tol=0.06):
        return any([s.repin_needed(self._only(poll, sid), tol=tol) for sid, s in self._each()])

    def apply(self, platform="imx291", timeout=5.0):
        return {sid: s.apply(platform=platform, timeout=timeout) for sid, s in self._each()}

    def step(self, metering):
        infos = {sid: s.step(self._only(metering, sid)) for sid, s in self._each()}
        vals = lambda k: [i[k] for i in infos.values() if i.get(k) is not None]
        med = lambda k: statistics.median(vals(k)) if vals(k) else None
        mx = lambda k: max(vals(k)) if vals(k) else None
        reasons = vals("reason")
        top = max(set(reasons), key=reasons.count) if reasons else "hold"
        self.last = {
            "individual": True,
            "pod_lum": mx("pod_lum"), "pod_clip": mx("pod_clip"), "pod_peak": mx("pod_peak"),
            "li": med("li"), "target": med("target"), "to_go": med("to_go"), "d_stops": med("d_stops"),
            "exp_us": med("exp_us"), "analog_x": med("analog_x"), "boost_x": med("boost_x"),
            "total_gain_x": med("total_gain_x"),
            "reason": "individual: %s" % top, "driver": None, "driver_why": None,
            "needs": {sid: (i["needs"][sid] if sid in (i.get("needs") or {}) else (0.0, i.get("reason")))
                      for sid, i in infos.items()},
            "state": self.state, "wb_scale": med("wb_scale"),
            "changed": any(i.get("changed") for i in infos.values()),
            "per_cam": {sid: {"li": i.get("li"), "target": i.get("target"), "exp_us": i.get("exp_us"),
                              "total_gain_x": i.get("total_gain_x"), "wb_scale": i.get("wb_scale"),
                              "reason": i.get("reason")} for sid, i in infos.items()},
        }
        return self.last
