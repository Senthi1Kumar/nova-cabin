#version 440
// Digital-human face rig: image-space warp of a single portrait.
// Mirrors tools/avatar_preview.py (CPU reference) — keep the two in sync.
//
// Inverse mapping per output pixel: head → mouth → brows → eyes, then sample.
// Everything is analytic: one texture fetch, ~120 ALU ops/pixel.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4  qt_Matrix;
    float qt_Opacity;
    // articulation
    float jaw;        // -0.35 pressed .. 1 open
    float rnd;        // -1 spread .. 1 round
    float smile;
    float brow;
    vec2  blink;      // left, right 0..1
    vec2  gaze;       // -1..1
    float headRot;    // radians
    vec2  headOff;    // px
    // rig (portrait pixel space)
    vec2  imgSize;
    vec4  mouthA;     // cx, seamCenter, seamCorner, halfWidth
    vec4  mouthB;     // jawPx, chinY, -, -
    vec4  eyeL;       // cx, cy, halfW, halfH
    vec4  eyeLx;      // lidPx, lashPx, gazePxX, gazePxY
    vec4  eyeR;
    vec4  eyeRx;
    vec4  browR;      // cx, cy, sx, sy
    vec4  headR;      // pivotX, pivotY, neckTop, neckBottom
    float browPx;
};
layout(binding = 1) uniform sampler2D source;

float sstep(float a, float b, float x) { return smoothstep(a, b, x); }

void eyeWarp(inout vec2 s, inout float lash, vec4 e, vec4 ex, float bl) {
    float o = (s.x - e.x) / e.z;
    float inX = clamp(1.0 - o * o, 0.0, 1.0);
    float half_ = e.w * sqrt(inX);
    float top = e.y - half_;
    float bot = e.y + half_ * 0.9;
    float edge = top + bl * (bot - top) * 1.12;
    float L = ex.x, lw = ex.y;
    float T = top - L;
    float wx = sstep(0.0, 0.18, inX);
    if (bl > 0.01 && s.y > T && s.y < edge) {
        if (bl > 0.6) lash = max(lash, exp(-pow((s.y - edge + 0.5) / 1.2, 2.0)) * wx * min(bl * 2.0, 1.0) * 0.35);
        float skin = T + (s.y - T) * (L - lw) / max(edge - lw - T, 1e-3);
        float lashes = top - (edge - s.y);
        float mapped = (s.y < edge - lw) ? skin : lashes;
        s.y = mix(s.y, mapped, wx);
    }
    // gaze: shift what is seen through the opening
    float r = pow((s.x - e.x) / (e.z * 0.70), 2.0) + pow((s.y - e.y) / (e.w * 0.85), 2.0);
    float wg = 1.0 - sstep(0.35, 1.0, r);
    s -= gaze * ex.zw * wg;
}

void main() {
    vec2 p = qt_TexCoord0 * imgSize;

    // ── head (outermost) ──
    float wh = 1.0 - sstep(headR.z, headR.w, p.y);
    float a = -headRot * wh;
    vec2 d = p - headR.xy - headOff * wh;
    vec2 s = headR.xy + vec2(d.x * cos(a) - d.y * sin(a), d.x * sin(a) + d.y * cos(a));

    // ── mouth ──
    float rp = max(rnd, 0.0), rn = max(-rnd, 0.0);
    float hw = mouthA.w * (1.0 - 0.22 * rp + 0.10 * rn);
    float ox = (s.x - mouthA.x) / hw;
    float ox2 = clamp(ox * ox, 0.0, 1.5);
    float lip = pow(clamp(1.0 - ox * ox, 0.0, 1.0), 0.7);
    float seam = mouthA.y - (mouthA.y - mouthA.z) * ox2 - smile * 2.5 * ox2;
    float J = mouthB.x, chin = mouthB.y;
    float open_ = max(jaw, 0.0), close = max(-jaw, 0.0);
    float wide = exp(-pow((s.x - mouthA.x) / 46.0, 2.0));
    float lipm = sstep(0.0, 0.3, lip);
    float gap = J * open_ * (0.55 * wide + 0.45 * lip);
    float ramp = lipm * sstep(seam - 0.6, seam + 0.6, s.y) + (1.0 - lipm) * sstep(seam - 6.0, seam + 22.0, s.y);
    float chinFade = 1.0 - sstep(chin - 8.0, chin + 48.0, s.y);
    float vfall = ramp * chinFade;
    float D = gap - J * close * 0.45 * lip;
    if (close > 0.0) {
        float c0 = seam - close * 7.0;
        vfall = max(vfall, sstep(c0 - 1.0, c0 + 1.0, s.y) * lipm * chinFade);
    }
    float cavMask = sstep(seam - 0.7, seam + 0.7, s.y) * sstep(-0.8, 0.8, seam - (s.y - gap)) * lipm
                  * step(0.01, open_);
    float cavT = clamp((s.y - seam) / max(gap, 1e-3), 0.0, 1.0);
    float sy = s.y - D * vfall;
    sy += J * open_ * 0.10 * lip * exp(-pow((s.y - seam + 6.0) / 7.0, 2.0)) * step(s.y, seam);
    float band = exp(-pow((s.y - seam) / 12.0, 2.0));
    float corner = exp(-pow((abs(s.x - mouthA.x) - mouthA.w) / 9.0, 2.0));
    float sx = s.x + rnd * 4.0 * sign(s.x - mouthA.x) * corner * band;
    sy += smile * 3.0 * corner * band;
    s = vec2(sx, sy);

    // ── brows ──
    float wb = exp(-(pow((s.x - browR.x) / browR.z, 2.0) + pow((s.y - browR.y) / browR.w, 2.0)));
    s.y += brow * browPx * wb;

    // ── eyes ──
    float lash = 0.0;
    eyeWarp(s, lash, eyeL, eyeLx, blink.x);
    eyeWarp(s, lash, eyeR, eyeRx, blink.y);

    // ── sample (premultiplied) ──
    vec2 uv = s / imgSize;
    vec4 c = texture(source, uv);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) c = vec4(0.0);

    // ── mouth interior ──
    if (cavMask > 0.0) {
        vec3 dark = vec3(0.13, 0.04, 0.06);
        vec3 col = dark * (0.7 + 0.6 * (1.0 - abs(ox)));
        col += (vec3(0.50, 0.20, 0.24) - dark) * sstep(0.55, 1.0, cavT) * 0.8 * open_;
        col += (vec3(0.80, 0.78, 0.78) - col) * sstep(0.82, 0.97, cavT) * sstep(0.55, 0.9, open_)
               * clamp(1.0 - ox * ox * 2.2, 0.0, 1.0) * 0.25;
        col *= 1.0 - 0.45 * exp(-pow(cavT / 0.18, 2.0));
        c.rgb = mix(c.rgb, col * max(c.a, 0.9), cavMask);
    }
    c.rgb *= 1.0 - lash;
    fragColor = c * qt_Opacity;
}
