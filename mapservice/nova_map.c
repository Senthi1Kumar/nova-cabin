/* In-process MapLibre view for the Qt nav panel. The worker, style and route
   layers are the DriveOS ones. The car is not a map layer: each frame carries
   the car's pixel, and the DriveOS puck is stamped there so it stays on that spot. */
#include "map/map_engine.h"
#include "map/map_puck.h"
#include "map/map_style.h"
#include "map/map_worker.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define STYLE_NIGHT "mapbox://styles/mapbox/navigation-night-v1"
#define STYLE_FREE "https://tiles.openfreemap.org/styles/dark"
#define EMPTY "{\"type\":\"FeatureCollection\",\"features\":[]}"

typedef struct {
    char style[160];
    char token[512];
    char cache[512];
    dos_mapw_t *worker;
    uint8_t *clean;    /* the frame without a puck, so a later car move can be restamped */
    uint8_t *shown;
    size_t cap;
    uint32_t fw, fh, stride, tag_seq;
} nova_map_t;

static uint8_t s_puck[MAP_PUCK_PX * MAP_PUCK_PX * 4];
static int s_puck_ready;

static void puck_art(void)
{
    int i;
    if (s_puck_ready) return;
    map_puck_render(s_puck, MAP_PUCK_PX);   /* premultiplied RGBA; the frame is straight BGRA */
    for (i = 0; i < MAP_PUCK_PX * MAP_PUCK_PX; i++) {
        uint8_t *p = s_puck + i * 4, r = p[0], g = p[1], b = p[2], a = p[3];
        p[0] = a ? (uint8_t)(b * 255 / a) : 0;
        p[1] = a ? (uint8_t)(g * 255 / a) : 0;
        p[2] = a ? (uint8_t)(r * 255 / a) : 0;
    }
    s_puck_ready = 1;
}

static void sample_puck(float x, float y, uint8_t out[4])
{
    int x0, y0, i;
    float fx, fy;
    const uint8_t *p00, *p10, *p01, *p11;
    if (x < 0.f || y < 0.f || x >= MAP_PUCK_PX - 1 || y >= MAP_PUCK_PX - 1) {
        out[0] = out[1] = out[2] = out[3] = 0;
        return;
    }
    x0 = (int)x;
    y0 = (int)y;
    fx = x - x0;
    fy = y - y0;
    p00 = s_puck + (y0 * MAP_PUCK_PX + x0) * 4;
    p10 = p00 + 4;
    p01 = p00 + MAP_PUCK_PX * 4;
    p11 = p01 + 4;
    for (i = 0; i < 4; i++) {
        float v = p00[i] * (1.f - fx) * (1.f - fy) + p10[i] * fx * (1.f - fy)
                + p01[i] * (1.f - fx) * fy + p11[i] * fx * fy;
        out[i] = (uint8_t)(v + 0.5f);
    }
}

/* rot_deg is clockwise from the frame's up, the same angle DriveOS gives LVGL. */
static void stamp_puck(uint8_t *dst, uint32_t w, uint32_t h, uint32_t stride,
                       float cx, float cy, float rot_deg)
{
    float rad = rot_deg * (float)(M_PI / 180.0), cs = cosf(rad), sn = sinf(rad), half = MAP_PUCK_PX / 2.f;
    int x0 = (int)floorf(cx - half - 1.f), y0 = (int)floorf(cy - half - 1.f);
    int x1 = (int)ceilf(cx + half + 1.f), y1 = (int)ceilf(cy + half + 1.f);
    int x, y;
    puck_art();
    for (y = y0; y <= y1; y++) {
        if (y < 0 || y >= (int)h) continue;
        for (x = x0; x <= x1; x++) {
            float dx, dy, sx, sy;
            uint8_t s[4], *d;
            unsigned a, inv;
            if (x < 0 || x >= (int)w) continue;
            dx = (x + 0.5f) - cx;
            dy = (y + 0.5f) - cy;
            sx = dx * cs + dy * sn + half;
            sy = -dx * sn + dy * cs + half;
            sample_puck(sx, sy, s);
            if (!s[3]) continue;
            d = dst + (size_t)y * stride + (size_t)x * 4;
            a = s[3];
            inv = 255 - a;
            d[0] = (uint8_t)((s[0] * a + d[0] * inv + 127) / 255);
            d[1] = (uint8_t)((s[1] * a + d[1] * inv + 127) / 255);
            d[2] = (uint8_t)((s[2] * a + d[2] * inv + 127) / 255);
            d[3] = 255;
        }
    }
}

static int grow(uint8_t **buf, size_t *cap, size_t need)
{
    uint8_t *p;
    if (*buf && *cap >= need) return 1;
    p = realloc(*buf, need);
    if (!p) return 0;
    *buf = p;
    if (need > *cap) *cap = need;
    return 1;
}

static void add_layers(dos_map_t *m)
{
    /* The night style requests live-traffic tiles. On the Pi those downloads arrive
       truncated ("gzip decompression failed") while the streets tiles are fine.
       DriveOS drops the two sources; route colour comes from Directions. */
    const char *tp = getenv("DOS_MAP_TRAFFIC_TILES");
    if (!(tp && *tp && strcmp(tp, "0"))) {
        static const char *const live[] = {"mapbox-traffic", "mapbox-incidents"};
        dos_map_drop_sources(m, live, 2);
    }
    dos_map_set_geojson(m, "dos-route", EMPTY);
    dos_map_set_geojson(m, "dos-pins", EMPTY);
    dos_map_add_layer_json(m,
        "{\"id\":\"dos-route-casing\",\"type\":\"line\",\"source\":\"dos-route\","
        "\"layout\":{\"line-join\":\"round\",\"line-cap\":\"round\"},"
        "\"paint\":{\"line-color\":\"#0a1a45\",\"line-width\":" DOS_STYLE_ROUTE_CASING_WIDTH "}}");
    dos_map_add_layer_json(m,
        "{\"id\":\"dos-route-line\",\"type\":\"line\",\"source\":\"dos-route\","
        "\"layout\":{\"line-join\":\"round\",\"line-cap\":\"round\"},"
        "\"paint\":{\"line-width\":" DOS_STYLE_ROUTE_LINE_WIDTH ","
        "\"line-color\":[\"match\",[\"get\",\"c\"],"
        "\"l\",\"#4a86ff\",\"m\",\"#f4b63f\",\"h\",\"#e8553d\",\"s\",\"#9b1c2e\",\"#4a86ff\"]}}");
    dos_map_add_layer_json(m,
        "{\"id\":\"dos-pin-dot\",\"type\":\"circle\",\"source\":\"dos-pins\","
        "\"paint\":{\"circle-radius\":[\"match\",[\"get\",\"k\"],\"dest\",9,\"charge\",8,7],"
        "\"circle-color\":[\"match\",[\"get\",\"k\"],\"dest\",\"#ffb25e\",\"charge\",\"#f5c84c\","
        "\"poi\",\"#c9b6ff\",\"#8b5cf6\"],\"circle-stroke-color\":\"#ffffff\",\"circle-stroke-width\":2}}");
}

nova_map_t *nova_map_create(double lat, double lon, const char *token, const char *cache,
                            uint32_t width, uint32_t height)
{
    nova_map_t *n = calloc(1, sizeof *n);
    if (!n) return NULL;
    snprintf(n->style, sizeof n->style, "%s", (token && token[0]) ? STYLE_NIGHT : STYLE_FREE);
    if (token) snprintf(n->token, sizeof n->token, "%s", token);
    if (cache) snprintf(n->cache, sizeof n->cache, "%s", cache);
    dos_map_cfg_t cfg = {
        .width = width ? width : 1024, .height = height ? height : 600,
        .lat = lat, .lon = lon, .zoom = 16, .bearing = 0, .pitch = 0,
        .style_url = n->style,
        .access_token = n->token[0] ? n->token : NULL,
        .cache_path = n->cache[0] ? n->cache : NULL,
    };
    n->worker = dos_mapw_create(&cfg, add_layers);
    if (!n->worker) {
        free(n);
        return NULL;
    }
    return n;
}

void nova_map_destroy(nova_map_t *n)
{
    if (!n) return;
    dos_mapw_destroy(n->worker);
    free(n->clean);
    free(n->shown);
    free(n);
}

void nova_map_set_period(nova_map_t *n, uint32_t ms)
{
    if (n && n->worker) dos_mapw_set_period(n->worker, ms);
}

void nova_map_set_car(nova_map_t *n, double lat, double lon, double heading)
{
    if (!n || !n->worker) return;
    dos_mapw_set_car(n->worker, lat, lon, heading, 0);
}

void nova_map_follow(nova_map_t *n, int on, int heading_up, double zoom, double pitch,
                     double pad_top_px, double ease_ms)
{
    if (n && n->worker)
        dos_mapw_follow(n->worker, on, heading_up, zoom, pitch, pad_top_px, ease_ms);
}

void nova_map_set_route(nova_map_t *n, const char *geojson)
{
    if (n && n->worker)
        dos_mapw_set_geojson(n->worker, "dos-route", geojson && geojson[0] ? geojson : EMPTY);
}

void nova_map_set_pins(nova_map_t *n, const char *geojson)
{
    if (n && n->worker)
        dos_mapw_set_geojson(n->worker, "dos-pins", geojson && geojson[0] ? geojson : EMPTY);
}

void nova_map_fit(nova_map_t *n, double south, double west, double north, double east, double pad, double ms)
{
    if (n && n->worker)
        dos_mapw_fit(n->worker, south, west, north, east, pad, ms);
}

void nova_map_camera(nova_map_t *n, double lat, double lon, double zoom, double ms)
{
    if (n && n->worker)
        dos_mapw_camera_to(n->worker, lat, lon, zoom, (double)NAN, 0, -1, ms);
}

const uint8_t *nova_map_take(nova_map_t *n, uint32_t *w, uint32_t *h, uint32_t *stride)
{
    dos_mapw_frame_t frame = {0};
    const uint8_t *px;
    size_t bytes;
    if (!n || !n->worker) return NULL;
    px = dos_mapw_take(n->worker, &frame);
    if (px && frame.w && frame.h && frame.stride) {
        bytes = (size_t)frame.stride * frame.h;
        if (!grow(&n->clean, &n->cap, bytes) || !grow(&n->shown, &n->cap, bytes)) return NULL;
        memcpy(n->clean, px, bytes);
        n->fw = frame.w;
        n->fh = frame.h;
        n->stride = frame.stride;
    } else if (!n->clean || !n->fw || frame.tag_seq == n->tag_seq) {
        return NULL;
    }
    bytes = (size_t)n->stride * n->fh;
    memcpy(n->shown, n->clean, bytes);
    if (frame.tag.car_on)
        stamp_puck(n->shown, n->fw, n->fh, n->stride, frame.tag.car_x, frame.tag.car_y, frame.tag.car_rot);
    n->tag_seq = frame.tag_seq;
    if (w) *w = n->fw;
    if (h) *h = n->fh;
    if (stride) *stride = n->stride;
    return n->shown;
}

int nova_map_failed(nova_map_t *n, char *why, size_t cap)
{
    if (!n || !n->worker) return 1;
    return dos_mapw_failed(n->worker, why, cap) ? 1 : 0;
}
