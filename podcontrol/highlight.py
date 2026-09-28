"""Green rebuild for raw-saturated highlights (off-camera; EXPERIMENTAL, not wired in).

With the WB gains scaled down for highlight headroom (podcontrol's WB rung, green gain s < 1),
a pixel whose RAW green saturated comes out at green = s * full scale while red and blue keep
going: clipped areas turn magenta. The camera cannot fix it (per-channel clip in fixed ISP
hardware; the GK7205V200 has no 3D colour LUT), so do it on the frame, in linear light:

  where green sits on its plateau, green = mean(kR * R, kB * B) over the channels that are
  not clipped themselves, never below the plateau; kR = G/R and kB = G/B are the scene's
  colour ratios measured on bright unclipped pixels. Both R and B clipped -> the pixel was
  saturated in every channel -> white.

Known in raw processing as highlight reconstruction. Tested on .101 against a 4x darker
frame (2026-09-28): 2% green error, <2% colour error, where clip-to-white or G = max(R,B)
were 14%. Frames are the camera's 8-bit full-range pure gamma 0.5 (lin = (v/255)^2).
"""
import numpy as np

CLIP = 253          # 8-bit code at/above which R or B counts as clipped


def plateau(g, lo=100):
    """Green plateau code: the most common code among the top of the green histogram, if it
    stands out (a real clip plateau), else None."""
    v = g[g > lo]
    if v.size < 1000:
        return None
    h = np.bincount(v.ravel(), minlength=256)
    top = int(np.argmax(h[lo:])) + lo
    others = np.median(h[max(lo, top - 20):top - 2]) if top - 2 > lo else 0
    # the plateau is the top of the distribution; a few demosaic-edge pixels above it are normal
    return top if h[top] > 8 * max(others, 1) and top >= np.percentile(v, 99.9) - 3 else None


MIN_REF = 0.005     # reference pixels needed (fraction of the frame) to trust the scene's ratios


def rebuild_green(rgb, plateau_code=None, local=0, fallback=(1.0, 1.0)):
    """rgb: HxWx3 uint8 (R, G, B order). Returns (rgb_out uint8, info). local > 0 uses
    local ratios in a local x local window (needs OpenCV); 0 = one scene-wide ratio.
    fallback = (kR, kB) when too little of the frame is unclipped to measure them: the
    default 1.0 is NEUTRAL GREY (the rung scales the proper WB gains by one factor, so a grey
    object has R = G = B after the gains); a caller can pass the ratios of a recent frame."""
    R8, G8, B8 = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    p = plateau(G8) if plateau_code is None else plateau_code
    if p is None:
        return rgb, {"plateau": None, "clipped": 0.0}
    lin = (rgb.astype(np.float64) / 255.0) ** 2
    R, G, B = lin[..., 0], lin[..., 1], lin[..., 2]
    gp = (p / 255.0) ** 2
    clipG = G8 >= p - 1
    cR, cB = R8 >= CLIP, B8 >= CLIP
    ref = (~clipG) & (G > 0.6 * gp) & ~cR & ~cB & (R > 1e-4) & (B > 1e-4)
    if ref.mean() >= MIN_REF:
        kR, kB = np.median(G[ref] / R[ref]), np.median(G[ref] / B[ref])
        src = "scene"
    else:                                                  # (almost) everything clipped
        kR, kB = fallback
        src = "fallback"
        local = 0
    if local:
        import cv2
        w = cv2.blur(ref.astype(np.float64), (local, local))
        lr = cv2.blur(np.where(ref, G / np.maximum(R, 1e-6), 0), (local, local)) / np.maximum(w, 1e-9)
        lb = cv2.blur(np.where(ref, G / np.maximum(B, 1e-6), 0), (local, local)) / np.maximum(w, 1e-9)
        kR = np.where(w > 0.02, lr, kR)
        kB = np.where(w > 0.02, lb, kB)
    eR, eB = R * kR, B * kB
    n = (~cR).astype(float) + (~cB).astype(float)
    est = (np.where(~cR, eR, 0) + np.where(~cB, eB, 0)) / np.maximum(n, 1)
    est = np.where(n == 0, 1.0, est)                       # all three saturated -> white
    Gn = np.where(clipG, np.clip(np.maximum(est, gp), 0, 1), G)
    out = rgb.copy()
    out[..., 1] = np.round(255.0 * np.sqrt(Gn)).astype(np.uint8)
    both = clipG & cR & cB
    out[both] = 255
    return out, {"plateau": p, "clipped": float(clipG.mean()), "ratios": src, "kR": float(np.median(kR)),
                 "kB": float(np.median(kB)), "all_saturated": float(both.mean())}


if __name__ == "__main__":
    import sys
    import cv2
    for path in sys.argv[1:]:
        img = cv2.imread(path)[:, :, ::-1]
        out, info = rebuild_green(img)
        dst = path.rsplit(".", 1)[0] + "_rebuilt.png"
        cv2.imwrite(dst, out[:, :, ::-1])
        print(path, info, "->", dst)
