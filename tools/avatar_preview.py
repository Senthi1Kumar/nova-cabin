"""CPU reference of qml/shaders/avatar.frag (same maths, numpy).

Used to tune the rig without a GPU and to render review sheets / GIFs:

    python tools/avatar_preview.py sheet  out.png      # expression contact sheet
    python tools/avatar_preview.py gif    out.gif      # 6 s simulated turn
    python tools/avatar_preview.py grid   out.png      # landmark overlay for re-rigging
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import map_coordinates

ROOT = Path(__file__).resolve().parents[1]
RIG = json.loads((ROOT / "assets/avatar/rig.json").read_text())
IMG = np.asarray(Image.open(ROOT / "assets/avatar" / RIG["image"]).convert("RGBA"), np.float32) / 255.0
H, W = IMG.shape[:2]
# premultiply like the GPU texture
IMG[..., :3] *= IMG[..., 3:4]


def sstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def render(jaw=0.0, rnd=0.0, smile=0.0, blink=(0.0, 0.0), gaze=(0.0, 0.0), brow=0.0,
           head_rot=0.0, head_off=(0.0, 0.0)):
    y, x = np.mgrid[0:H, 0:W].astype(np.float32) + 0.5
    m, hd = RIG["mouth"], RIG["head"]

    # ── 1. head (outermost): rotate/translate everything above the neck ──
    px, py = hd["pivot"]
    wh = 1.0 - sstep(hd["neck_top"], hd["neck_bottom"], y)
    a = -head_rot * wh
    dx, dy = x - px - head_off[0] * wh, y - py - head_off[1] * wh
    sx = px + dx * np.cos(a) - dy * np.sin(a)
    sy = py + dx * np.sin(a) + dy * np.cos(a)

    # ── 2. mouth ──
    hw = m["half_width"] * (1 - 0.22 * max(rnd, 0) + 0.10 * max(-rnd, 0))
    ox = (sx - m["cx"]) / hw
    lip = np.clip(1 - ox * ox, 0, 1) ** 0.7                      # lip-gap profile
    seam = m["seam_center"] - (m["seam_center"] - m["seam_corner"]) * np.clip(ox * ox, 0, 1.5) \
        - smile * 2.5 * np.clip(ox * ox, 0, 1.5)
    J = m["jaw_px"]
    open_ = max(jaw, 0.0)
    close = max(-jaw, 0.0)
    wide = np.exp(-((sx - m["cx"]) / 46.0) ** 2)               # chin moves as a block
    lipm = sstep(0.0, 0.3, lip)
    gap = J * open_ * (0.55 * wide + 0.45 * lip)
    # lips move rigidly (sharp seam); cheeks/chin outside the lips stretch smoothly
    ramp = lipm * sstep(seam - 0.6, seam + 0.6, sy) + (1 - lipm) * sstep(seam - 6, seam + 22, sy)
    vfall = ramp * (1 - sstep(m["chin"] - 8, m["chin"] + 48, sy))
    D = gap - J * close * 0.45 * lip
    if close > 0:   # pressing: lower lip rides up over the teeth
        vfall = np.maximum(vfall, sstep(seam - close * 7 - 1, seam - close * 7 + 1, sy) * lipm
                           * (1 - sstep(m["chin"] - 8, m["chin"] + 48, sy)))
    syj = sy - D * vfall
    # anti-aliased cavity: below the seam AND above the displaced lower lip
    cav_mask = (sstep(seam - 0.7, seam + 0.7, sy) * sstep(-0.8, 0.8, seam - (sy - gap)) * lipm
                * (open_ > 0.01))
    cav_t = np.clip((sy - seam) / np.maximum(gap, 1e-3), 0, 1)
    # upper lip lifts a touch when the jaw drops
    up = J * open_ * 0.10 * lip * np.exp(-((sy - seam + 6) / 7.0) ** 2) * (sy <= seam)
    syj = syj + up
    # rounding pulls corners in, spreading pushes them out
    band = np.exp(-((sy - seam) / 12.0) ** 2)
    corner = np.exp(-((np.abs(sx - m["cx"]) - m["half_width"]) / 9.0) ** 2)
    sxj = sx + rnd * 4.0 * np.sign(sx - m["cx"]) * corner * band
    # smile lifts the corners
    syj = syj + smile * 3.0 * corner * band
    sx, sy = sxj, syj

    # ── 3. brows ──
    b = RIG["brows"]
    wb = np.exp(-(((sx - b["cx"]) / b["sx"]) ** 2 + ((sy - b["cy"]) / b["sy"]) ** 2))
    sy = sy + brow * b["raise_px"] * wb

    # ── 4. eyes: blink (lid skin stretches down) then gaze ──
    lash = np.zeros_like(sx)
    for e, bl, in zip(RIG["eyes"], blink):
        ex, ey, ew, eh, L = e["cx"], e["cy"], e["half_w"], e["half_h"], e["lid_px"]
        o = (sx - ex) / ew
        inside_x = np.clip(1 - o * o, 0, 1)
        half = eh * np.sqrt(inside_x)
        top, bot = ey - half, ey + half * 0.9
        edge = top + bl * (bot - top) * 1.12
        T = top - L
        wx = sstep(0.0, 0.18, inside_x)
        lw = e.get("lash_px", 5.0)                                # lash/crease thickness
        inband = (sy > T) & (sy < edge) & (bl > 0.01)
        lash = np.maximum(lash, np.exp(-((sy - edge + 0.5) / 1.2) ** 2) * wx * min(bl * 2.0, 1.0) * 0.35 * (bl > 0.6))
        skin = T + (sy - T) * (L - lw) / np.maximum(edge - lw - T, 1e-3)    # lid skin band, stretched
        lashes = top - (edge - sy)                                        # real lashes ride the edge
        mapped = np.where(sy < edge - lw, skin, lashes)
        sy = np.where(inband, sy + (mapped - sy) * wx, sy)
        # gaze: shift what is seen through the eye opening
        r = ((sx - ex) / (ew * 0.70)) ** 2 + ((sy - ey) / (eh * 0.85)) ** 2
        wg = 1 - sstep(0.35, 1.0, r)
        sx = sx - gaze[0] * e["gaze_px"][0] * wg
        sy = sy - gaze[1] * e["gaze_px"][1] * wg

    # ── sample ──
    out = np.stack([map_coordinates(IMG[..., c], [sy - 0.5, sx - 0.5], order=1, mode="constant")
                    for c in range(4)], -1)

    # ── mouth cavity shading (premultiplied) ──
    t = cav_t
    dark = np.array([0.13, 0.04, 0.06])
    tongue = np.array([0.50, 0.20, 0.24])
    lower_teeth = np.array([0.80, 0.78, 0.78])
    col = dark[None, None, :] * (0.7 + 0.6 * (1 - np.abs(ox[..., None])))
    col = col + (tongue - dark) * sstep(0.55, 1.0, t)[..., None] * 0.8 * open_
    col = col + (lower_teeth - col) * (sstep(0.82, 0.97, t) * sstep(0.55, 0.9, open_)
                                       * np.clip(1 - ox * ox * 2.2, 0, 1))[..., None] * 0.25
    shade = 1 - 0.45 * np.exp(-(t / 0.18) ** 2)                 # shadow under upper teeth
    col = col * shade[..., None]
    cm = cav_mask[..., None].astype(np.float32)
    out[..., :3] = out[..., :3] * (1 - cm) + col * cm * out[..., 3:4].clip(0.9, 1)
    out[..., :3] *= (1 - lash[..., None])
    return out


def to_img(arr, bg=(9, 14, 32)):
    rgb = arr[..., :3] + np.array(bg, np.float32)[None, None] / 255 * (1 - arr[..., 3:4])
    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))


def sheet(path):
    poses = [
        ("neutral", {}), ("blink", dict(blink=(1, 1))), ("half blink", dict(blink=(0.5, 0.5))),
        ("AA  jaw 1.0", dict(jaw=1.0)), ("EE  spread", dict(jaw=0.35, rnd=-0.8)),
        ("OO  round", dict(jaw=0.45, rnd=1.0)), ("MBP closed", dict(jaw=-0.35)),
        ("listening", dict(brow=0.6, head_rot=-0.03, gaze=(0.6, 0.0))),
        ("thinking", dict(gaze=(-0.9, -0.9), brow=0.4, head_rot=0.025, head_off=(0, -3), rnd=0.25)),
        ("smile", dict(smile=1.0)),
    ]
    tiles = []
    for name, kw in poses:
        im = to_img(render(**kw)).crop((265, 140, 455, 370)).resize((380, 460), Image.LANCZOS)
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, 380, 34), fill=(0, 0, 0))
        d.text((10, 8), name, fill=(230, 235, 255))
        tiles.append(im)
    cols = 5
    rows = math.ceil(len(tiles) / cols)
    out = Image.new("RGB", (cols * 380, rows * 460))
    for i, t in enumerate(tiles):
        out.paste(t, ((i % cols) * 380, (i // cols) * 460))
    out.save(path)


def gif(path, fps=20):
    """Full turn: idle → listening → thinking → speaking, behaviour mirrors AvatarRig.qml."""
    import random
    sys.path.insert(0, str(ROOT))
    from cabin.lipsync import VisemeTrack
    rng = random.Random(3)
    timeline = [("idle", 2.4), ("listening", 3.6), ("thinking", 1.8), ("speaking", 5.6), ("idle", 1.2)]
    track = VisemeTrack()
    t0 = sum(d for _, d in timeline[:3]) + 0.15
    for w in "Done — here’s your weekend at Lake Tahoe. One quick charge in Reno.".split():
        dur = (0.6 + 0.08 * len(w)) / 2.7 + (0.3 if w.endswith((".", "—", ",")) else 0)
        track.add_word(w, t0, dur)
        t0 += dur
    speak_end = t0

    ch = dict(jaw=0, rnd=0, smile=.3, brow=0, gx=0, gy=0, rot=0, hx=0, hy=0)
    vel = {k: 0.0 for k in ch}
    def spring(k, target, om, dt):          # exact critically damped step (same as QML)
        y, v = ch[k] - target, vel[k]
        e, c = math.exp(-om * dt), v + om * (ch[k] - target)
        ch[k], vel[k] = target + (y + c * dt) * e, (v - om * c * dt) * e
    noise = lambda t, s: .5 * math.sin(t * .61 + s) + .3 * math.sin(t * 1.37 + s * 2.1) + .2 * math.sin(t * 2.71 + s * 3.7)
    blink_at, next_blink, gaze_t, next_sac = -9, 1.5, (0.1, 0), 0.5
    frames, dt, t = [], 1 / fps, 0.0
    bounds, acc = [], 0
    for m, d in timeline:
        bounds.append((acc, acc + d, m)); acc += d
    last = None
    while t < acc:
        m = next(mm for a0, a1, mm in bounds if a0 <= t < a1)
        if m != last:
            blink_at, next_sac, last = t, t + .15, m
        lv = 0.0
        if m == "speaking" and t < speak_end:
            lv = .3 + .6 * abs(math.sin(t * 2 * math.pi * 2.3))
        elif m == "listening":
            lv = .2 + .6 * rng.random() * abs(math.sin(t * 13))
        jawT = rndT = 0; smileT, browT = .3, 0
        rotT, hxT, hyT = .012 * noise(t * .5, 1.3), 2 * noise(t * .4, 4.1), 1.5 * noise(t * .45, 7.7)
        if m == "listening":
            smileT, browT, rotT = .18, .45, -.035
            hyT = 1.5 * noise(t * .5, 1.1) + (2.2 if (t * 1.4) % 1 < .2 else 0)
        elif m == "thinking":
            smileT, browT, rndT, jawT, rotT, hyT, hxT = .05, .35, .25, -.12, .03, -3.5, -1.5
        elif m == "speaking":
            j, r = track.sample(t)
            jawT, rndT = j * (.75 + .45 * lv), r
            browT = .12 + .5 * max(0, math.sin(t * 2.1)) ** 6
            hyT = 1.2 * noise(t * .7, 9.1) - 1.5 * max(0, math.sin(t * 2.1)) ** 6
        if t >= next_sac:
            if m == "thinking": gaze_t, next_sac = (rng.uniform(-1, -.6), rng.uniform(-1, -.65)), t + rng.uniform(.9, 1.8)
            elif m == "listening": gaze_t, next_sac = (.45 + rng.uniform(-.08, .08), rng.uniform(-.08, .08)), t + rng.uniform(.6, 1.4)
            elif m == "speaking": gaze_t, next_sac = (.35 + rng.uniform(-.1, .1), rng.uniform(-.1, .1)), t + rng.uniform(.8, 2)
            else: gaze_t, next_sac = (rng.uniform(-.45, .55), rng.uniform(-.35, .3)), t + rng.uniform(1, 3.2)
        if t >= next_blink:
            blink_at, next_blink = t, t + rng.uniform(2.2, 4.5)
        u = t - blink_at
        bl = 0 if u < 0 else u / .07 if u < .07 else 1 if u < .11 else max(0, 1 - ((u - .11) / .13) ** 2 * (3 - 2 * (u - .11) / .13)) if u < .24 else 0
        spring("jaw", jawT, 34 if m == "speaking" else 14, dt); spring("rnd", rndT, 26, dt)
        spring("smile", smileT, 4, dt); spring("brow", browT, 9, dt)
        spring("gx", gaze_t[0], 38, dt); spring("gy", gaze_t[1], 38, dt)
        spring("rot", rotT, 3.2, dt); spring("hx", hxT, 3, dt); spring("hy", hyT, 3.5 if m != "speaking" else 7, dt)
        im = to_img(render(jaw=ch["jaw"], rnd=max(-1, min(1, ch["rnd"])), smile=max(0, ch["smile"]),
                           blink=(bl, bl), gaze=(ch["gx"], ch["gy"]), brow=max(0, ch["brow"]),
                           head_rot=ch["rot"], head_off=(ch["hx"], ch["hy"])))
        fr = im.crop((150, 60, 490, 460)).resize((306, 360), Image.LANCZOS)
        dr = ImageDraw.Draw(fr)
        dr.rectangle((0, 0, 306, 24), fill=(6, 9, 20))
        dr.text((10, 6), m.upper(), fill=(170, 180, 230))
        frames.append(fr)
        t += dt
    pal = frames[len(frames) // 2].quantize(colors=220, method=Image.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.FLOYDSTEINBERG) for f in frames]
    q[0].save(path, save_all=True, append_images=q[1:], duration=int(1000 / fps), loop=0, optimize=True)


def grid(path):
    im = to_img(render()).resize((W * 3, H * 3), Image.NEAREST)
    d = ImageDraw.Draw(im)
    for v in range(0, W, 10):
        d.line([(v * 3, 0), (v * 3, H * 3)], fill=(255, 0, 0) if v % 50 == 0 else (90, 90, 0))
    for v in range(0, H, 10):
        d.line([(0, v * 3), (W * 3, v * 3)], fill=(255, 0, 0) if v % 50 == 0 else (90, 90, 0))
    im.save(path)


if __name__ == "__main__":
    mode, out = sys.argv[1], sys.argv[2]
    {"sheet": sheet, "gif": gif, "grid": grid}[mode](out)
