#version 440
// Luminous horizon ribbon — analytic distance-field glow, fully on the GPU.
// One full-screen-quad pass, no textures, no offscreen blur: O(pixels) ALU only.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4  qt_Matrix;
    float qt_Opacity;
    float time;      // seconds
    float energy;    // 0 idle .. 1 speaking
    vec2  res;       // item size in px
};

// curve height (0..1 of item height) at normalised x
float curveY(float x, float t, float amp) {
    float y = 0.44
            - 0.27 * exp(-pow((x - 0.17) / 0.19, 2.0))          // crest under the shoulder
            + 0.30 * pow(smoothstep(0.50, 1.02, x), 1.6);       // dive toward the orb
    y += sin(t * 1.9 + x * 6.0) * amp + sin(t * 1.1 - x * 3.3) * amp * 0.6;
    return y;
}

float lineDist(vec2 p, float t, float amp, float offPx) {
    float x  = p.x / res.x;
    float e  = 1.5 / res.x;
    float y  = curveY(x, t, amp) * res.y + offPx;
    float y2 = curveY(x + e, t, amp) * res.y + offPx;
    float slope = (y2 - y) / 1.5;
    return (p.y - y) / sqrt(1.0 + slope * slope);   // signed, + below the line
}

void main() {
    vec2  p   = qt_TexCoord0 * res;
    float x   = qt_TexCoord0.x;
    float amp = (3.0 + energy * 9.0) / res.y;

    float d   = lineDist(p, time, amp, 0.0);
    float ad  = abs(d);

    // horizontal envelope: soft entry on the left, dissolve on the right
    float fadeL = smoothstep(0.0, 0.07, x);
    float fadeR = 1.0 - smoothstep(0.70, 1.0, x);
    float env   = fadeL * fadeR;

    // ground mass under the line (occludes avatar's lower edge)
    float below = smoothstep(-1.0, 2.0, d);
    float depth = clamp(d / res.y, 0.0, 1.0);
    vec3  ground = mix(vec3(0.051, 0.078, 0.204), vec3(0.024, 0.035, 0.086), depth * 1.4);
    float groundA = below * 0.97 * fadeR;

    // colour along the ribbon
    vec3 cA = vec3(0.31, 0.51, 1.00);
    vec3 cB = vec3(0.62, 0.78, 1.00);
    vec3 cC = vec3(0.60, 0.44, 1.00);
    vec3 col = mix(cA, cB, smoothstep(0.05, 0.24, x));
    col = mix(col, cC, smoothstep(0.35, 0.70, x));

    // energy pulse travelling along the line while speaking
    float pulse = 1.0 + energy * 0.9 * pow(0.5 + 0.5 * sin(x * 11.0 - time * 3.6), 6.0);

    float core = exp(-ad * ad / (2.0 * 1.1 * 1.1));
    float mid  = exp(-ad / 6.0) * 0.55;
    float wide = exp(-ad / 26.0) * 0.20 * (0.8 + energy * 0.5);
    float glow = (core * 1.15 + mid + wide) * env * pulse;

    // secondary violet filament
    float d2 = abs(lineDist(p, time * 0.8 + 1.7, amp * 0.8, 14.0 + sin(time * 1.3) * 4.0));
    float fil = (exp(-d2 * d2 / 1.4) * 0.55 + exp(-d2 / 7.0) * 0.12)
              * smoothstep(0.08, 0.35, x) * (1.0 - smoothstep(0.55, 0.95, x));
    vec3 filCol = vec3(0.68, 0.48, 1.0);

    // white-hot core
    vec3 light = col * glow + vec3(0.85, 0.92, 1.0) * core * 0.55 * env + filCol * fil;
    float topFade = smoothstep(0.0, 40.0, p.y);       // no hard edge at the item top
    light *= topFade;
    float lightA = clamp(max(glow, fil) * topFade, 0.0, 1.0);

    // composite (premultiplied): ground, then additive light
    vec3  rgb = ground * groundA + light;
    float a   = clamp(groundA + lightA * (1.0 - groundA), 0.0, 1.0);
    fragColor = vec4(rgb, a) * qt_Opacity;
}
