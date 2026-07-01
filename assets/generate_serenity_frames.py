#!/usr/bin/env python3
"""Generate an animated "flying Serenity" sprite sequence for the nice!view.

Reads the authoritative 140x68 1-bit bitmap from the LVGL C array in
``boards/shields/nice_view_custom/widgets/serenity.c`` and bakes three effects
into N frames:

  1. spinning tail  -- a rotor cut into the round engine bulb
  2. jitter fwd/back -- the ship bobs a couple of pixels along its travel axis
  3. animated trails -- the speed-lines scroll in the direction of travel

Serenity flies cockpit-first (to the LEFT), so the trails scroll leftward.

Output:
  * boards/shields/nice_view_custom/widgets/serenity_frames.c  (compiled firmware)
  * a preview contact-sheet PNG + per-frame PNGs in the scratchpad, and an
    animated GIF if ImageMagick/ffmpeg is available (eyeball before flashing)

Pure standard-library Python -- no Pillow required. Re-run after tweaking the
tuning constants below.
"""

import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

# --- paths --------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERENITY_C = os.path.join(REPO, "boards/shields/nice_view_custom/widgets/serenity.c")
OUT_C = os.path.join(REPO, "boards/shields/nice_view_custom/widgets/serenity_frames.c")
# Preview images are throwaway; keep them out of the repo by default.
PREVIEW_DIR = os.environ.get(
    "SERENITY_PREVIEW_DIR", os.path.join(tempfile.gettempdir(), "serenity_preview")
)

# --- tuning constants ---------------------------------------------------------
# Serenity flies cockpit-first (the round body is on the RIGHT); the baked lines
# to its left are the tail/exhaust trails. We keep those original trail pixels
# and animate them by sweeping "negative" (white) gaps through them and randomly
# blinking individual pixels on/off, so they shimmer irregularly.
W, H = 140, 68
FRAMES = 16          # loop length; random flicker means no visible seam
BOB_PX = 2           # +/- horizontal bob amplitude (pixels)

TRAIL_GAP_PERIOD = 13   # spacing of the sweeping white gaps (pixels)
TRAIL_GAP_WIDTH = 2     # width of each sweeping gap (pixels)
TRAIL_GAP_SCROLL = 2    # pixels the gaps sweep backwards (leftward) per frame
TRAIL_FLICKER = 0.16    # per-frame chance any given trail pixel blinks out
TRAIL_SEED = 0x5E12     # seed so the random flicker is reproducible

PREVIEW_SCALE = 3

WHITE, BLACK = 1, 0  # index values: 1 = background (white), 0 = ship (black)


# --- bitmap helpers -----------------------------------------------------------
class Bitmap:
    """A W x H buffer of index values (0 = black ship, 1 = white background)."""

    def __init__(self, fill=WHITE):
        self.px = [[fill] * W for _ in range(H)]

    def get(self, x, y):
        if 0 <= x < W and 0 <= y < H:
            return self.px[y][x]
        return WHITE

    def set(self, x, y, v):
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = v


def parse_serenity():
    """Return the source bitmap as a Bitmap (dropping the 8 palette bytes)."""
    src = open(SERENITY_C).read()
    m = re.search(r"SERENITY_map\[\]\s*=\s*\{(.*?)\};", src, re.S)
    if not m:
        sys.exit("could not find SERENITY_map[] in " + SERENITY_C)
    allb = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]{2}", m.group(1))]
    data = allb[8:]  # skip the two 4-byte palette entries
    rowbytes = (W + 7) // 8  # 18
    bmp = Bitmap()
    for y in range(H):
        for x in range(W):
            byte = data[y * rowbytes + (x >> 3)]
            bmp.px[y][x] = (byte >> (7 - (x & 7))) & 1
    return bmp


def split_ship_and_trails(bmp):
    """Flood-fill the connected body (the cockpit/round mass on the right) and
    treat every other black pixel as trail. Returns (ship set, trails set) of
    original pixel coordinates -- the trails keep their native, irregular shape.
    """
    # Seed: the rightmost black pixel (guaranteed to sit in the body).
    seed = None
    for x in range(W - 1, -1, -1):
        for y in range(H):
            if bmp.px[y][x] == BLACK:
                seed = (x, y)
                break
        if seed:
            break
    if not seed:
        sys.exit("no black pixels found in source bitmap")

    ship = set()
    stack = [seed]
    while stack:
        x, y = stack.pop()
        if (x, y) in ship:
            continue
        if not (0 <= x < W and 0 <= y < H):
            continue
        if bmp.px[y][x] != BLACK:
            continue
        ship.add((x, y))
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    trails = {(x, y) for y in range(H) for x in range(W)
              if bmp.px[y][x] == BLACK and (x, y) not in ship}
    return ship, sorted(trails)


def compose_frame(f, ship, trails):
    bmp = Bitmap(WHITE)

    # Jitter: bob the whole thing forwards/backwards along the travel axis.
    dx = round(BOB_PX * math.sin(2 * math.pi * f / FRAMES))
    for (x, y) in ship:
        bmp.set(x + dx, y, BLACK)

    # Trails: start from the original, irregular trail pixels, then thin them
    # out two ways so they read as streaming rather than a static shape --
    #   (a) sweeping white gaps that drift backwards (leftward) each frame, and
    #   (b) a per-frame random chance that any pixel blinks out entirely.
    rng = random.Random(TRAIL_SEED + f)
    for (x, y) in trails:
        in_gap = ((x + f * TRAIL_GAP_SCROLL) % TRAIL_GAP_PERIOD) < TRAIL_GAP_WIDTH
        if in_gap or rng.random() < TRAIL_FLICKER:
            continue
        bmp.set(x + dx, y, BLACK)
    return bmp


# --- C emission ---------------------------------------------------------------
C_HEADER = """#ifdef __has_include
    #if __has_include("lvgl.h")
        #ifndef LV_LVGL_H_INCLUDE_SIMPLE
            #define LV_LVGL_H_INCLUDE_SIMPLE
        #endif
    #endif
#endif

#if defined(LV_LVGL_H_INCLUDE_SIMPLE)
    #include "lvgl.h"
#else
    #include "lvgl/lvgl.h"
#endif

#ifndef LV_ATTRIBUTE_MEM_ALIGN
#define LV_ATTRIBUTE_MEM_ALIGN
#endif

/* Generated by assets/generate_serenity_frames.py -- do not edit by hand. */
"""


def pack_rows(bmp):
    """Pack a Bitmap into LVGL INDEXED_1BIT row bytes (padding bits = 0)."""
    rowbytes = (W + 7) // 8  # 18
    out = []
    for y in range(H):
        for b in range(rowbytes):
            byte = 0
            for bit in range(8):
                x = b * 8 + bit
                if x < W and bmp.px[y][x] == WHITE:
                    byte |= 1 << (7 - bit)
            out.append(byte)
    return out


def emit_c(frames):
    rowbytes = (W + 7) // 8
    data_size = 8 + rowbytes * H  # palette + pixels == 1232
    lines = [C_HEADER]
    names = []
    for i, bmp in enumerate(frames, 1):
        name = "serenity_%02d" % i
        names.append(name)
        rows = pack_rows(bmp)
        lines.append(
            "const LV_ATTRIBUTE_MEM_ALIGN LV_ATTRIBUTE_LARGE_CONST uint8_t %s_map[] = {"
            % name
        )
        lines.append("  0x00, 0x00, 0x00, 0xff, \t/*Color of index 0*/")
        lines.append("  0xff, 0xff, 0xff, 0xff, \t/*Color of index 1*/")
        lines.append("")
        for y in range(H):
            row = rows[y * rowbytes:(y + 1) * rowbytes]
            lines.append("  " + " ".join("0x%02x," % b for b in row))
        lines.append("};")
        lines.append("")
        lines.append("const lv_img_dsc_t %s = {" % name)
        lines.append("  .header.cf = LV_IMG_CF_INDEXED_1BIT,")
        lines.append("  .header.always_zero = 0,")
        lines.append("  .header.reserved = 0,")
        lines.append("  .header.w = %d," % W)
        lines.append("  .header.h = %d," % H)
        lines.append("  .data_size = %d," % data_size)
        lines.append("  .data = %s_map," % name)
        lines.append("};")
        lines.append("")
    open(OUT_C, "w").write("\n".join(lines))
    return names


# --- preview ------------------------------------------------------------------
def write_png(path, bmp, scale=PREVIEW_SCALE):
    ow, oh = W * scale, H * scale
    raw = bytearray()
    for y in range(oh):
        raw.append(0)  # filter byte
        for x in range(ow):
            raw.append(255 if bmp.px[y // scale][x // scale] == WHITE else 0)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", ow, oh, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(raw))
    open(path, "wb").write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def write_contact_sheet(path, frames, cols=4, scale=2, gap=4):
    rows = (len(frames) + cols - 1) // cols
    cw, ch = W * scale + gap, H * scale + gap
    ow, oh = cols * cw + gap, rows * ch + gap
    grid = [[200] * ow for _ in range(oh)]  # grey background
    for i, bmp in enumerate(frames):
        r, c = divmod(i, cols)
        ox, oy = gap + c * cw, gap + r * ch
        for y in range(H * scale):
            for x in range(W * scale):
                grid[oy + y][ox + x] = 255 if bmp.px[y // scale][x // scale] == WHITE else 0
    raw = bytearray()
    for y in range(oh):
        raw.append(0)
        raw.extend(grid[y])

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", ow, oh, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(raw))
    open(path, "wb").write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def try_make_gif(frame_paths, gif_path, ms=800):
    delay_cs = max(2, round(ms / len(frame_paths) / 10))  # centiseconds/frame
    if shutil.which("magick") or shutil.which("convert"):
        exe = "magick" if shutil.which("magick") else "convert"
        cmd = [exe, "-delay", str(delay_cs), "-loop", "0"] + frame_paths + [gif_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return gif_path
        except Exception:
            return None
    return None


def main():
    src = parse_serenity()
    ship, trails = split_ship_and_trails(src)
    print("ship pixels: %d  trail pixels: %d" % (len(ship), len(trails)))

    frames = [compose_frame(f, ship, trails) for f in range(FRAMES)]
    names = emit_c(frames)
    print("wrote %d frames -> %s" % (len(names), OUT_C))

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    frame_paths = []
    for i, bmp in enumerate(frames, 1):
        p = os.path.join(PREVIEW_DIR, "frame_%02d.png" % i)
        write_png(p, bmp)
        frame_paths.append(p)
    sheet = os.path.join(PREVIEW_DIR, "contact_sheet.png")
    write_contact_sheet(sheet, frames)
    print("preview frames + contact sheet -> %s" % PREVIEW_DIR)
    gif = try_make_gif(frame_paths, os.path.join(PREVIEW_DIR, "serenity_anim.gif"))
    if gif:
        print("animated preview -> %s" % gif)
    else:
        print("(install ImageMagick for an animated GIF; contact sheet written instead)")


if __name__ == "__main__":
    main()
