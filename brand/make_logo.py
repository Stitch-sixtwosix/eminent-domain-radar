"""Draws the animated EminentTracker mark and a still profile picture. Run: .venv/bin/python brand/make_logo.py (needs Pillow)."""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S, SS = 512, 3                      # output size, supersampling
N = S * SS
NAVY, WHITE, ORANGE, GLOW = (27, 35, 51), (255, 255, 255), (224, 102, 46), (255, 190, 140)
# Gmail and Google show profile pictures in a circle, so the mark is drawn smaller and centred on a full navy square.
SCALE, OX, OY = N * 0.60 / 32, N * 0.129, N * 0.20
X = lambda u: OX + u * SCALE
Y = lambda u: OY + u * SCALE
E = [(8, 5), (24, 5), (24, 9.2), (12.2, 9.2), (12.2, 22.8), (24, 22.8), (24, 27), (8, 27)]
ease = lambda t: 1 - (1 - t) ** 3


def back(t):                      # overshoots past 1, then settles: a pop
    c = 2.2
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def frame(t):
    """One loop: the E pops in, the bar fires across, the picture flashes on impact and shakes, rings keep pulsing, it snaps out."""
    flash = 0.27 <= t < 0.31
    ground, ink, bar = (ORANGE, NAVY, WHITE) if flash else (NAVY, WHITE, ORANGE)
    im = Image.new("RGB", (N, N), ground)
    d = ImageDraw.Draw(im, "RGBA")
    # size of the whole mark: pop in, hold, snap out
    z = back(t / 0.14) if t < 0.14 else 1.0 if t < 0.92 else max(0.0, 1 - ((t - 0.92) / 0.08) ** 2)
    beat = 1 + 0.045 * math.sin((t - 0.31) * math.pi * 2 / 0.155) if 0.31 <= t < 0.92 else 1   # a heartbeat with each ring
    z *= beat
    k = max(0.0, 1 - (t - 0.27) / 0.12) if t >= 0.27 else 0.0                                   # the shake after impact
    dx, dy = k * 1.6 * math.sin(t * 190), k * 1.1 * math.cos(t * 230)
    cx, cy = 19.0, 16.0                                                                         # the mark's own centre
    px = lambda u: X(cx + (u - cx) * z + dx)
    py = lambda u: Y(cy + (u - cy) * z + dy)
    for i in range(5):                   # rings from the bar's tip, one after another, wide enough to cross the picture
        start = 0.27 + i * 0.155
        if start <= t < start + 0.34:
            q = (t - start) / 0.34
            r, alpha = (2 + 30 * ease(q)) * SCALE, int(255 * (1 - q) ** 1.1)
            d.ellipse([px(29.5) - r, py(16) - r, px(29.5) + r, py(16) + r], outline=(NAVY if flash else ORANGE) + (alpha,), width=max(4, int(2.4 * SCALE * (1 - q) + 4)))
    if z > 0.02:
        d.polygon([(px(x), py(y)) for x, y in E], fill=ink)
    # the bar: fired in from off the left edge, leaving a streak behind it
    if 0.14 <= t < 0.27:
        q = ease((t - 0.14) / 0.13)
        head = -22 + (29.5 + 22) * q
        for j in range(9):               # the streak
            x0, x1 = head - 4 - j * 3.2, head - j * 3.2
            d.rectangle([X(x0), py(14.3 + j * 0.12), X(x1), py(17.7 - j * 0.12)], fill=bar + (int(255 * (1 - j / 9) ** 1.6),))
        r = 2.4 * SCALE
        d.ellipse([X(head) - r, py(16) - r, X(head) + r, py(16) + r], fill=GLOW)
    elif t >= 0.27 and z > 0.02:
        d.rectangle([px(12.2), py(13.9), px(29.5), py(18.1)], fill=bar)
        r = 2.1 * SCALE * z * (1.9 if flash else beat ** 6)
        d.ellipse([px(29.5) - r, py(16) - r, px(29.5) + r, py(16) + r], fill=bar)
        if 0.31 <= t < 0.92:             # a highlight keeps running along the bar
            hx = 12.2 + 17.3 * (((t - 0.31) / 0.155) % 1)
            d.rectangle([px(max(12.2, hx - 2.4)), py(13.9), px(min(29.5, hx + 0.8)), py(18.1)], fill=GLOW + (235,))
    return im.resize((S, S), Image.LANCZOS)


FRAMES = 54
frames = [frame(i / FRAMES) for i in range(FRAMES)]
first = int(FRAMES * 0.62)               # start on the finished mark, so a mail program that shows one frame shows the logo
frames = frames[first:] + frames[:first]
palette = Image.new("RGB", (S * 2, S)); palette.paste(frame(0.6), (0, 0)); palette.paste(frame(0.28), (S, 0))
palette = palette.quantize(colors=64, method=Image.MEDIANCUT)
q = [f.quantize(palette=palette, dither=Image.NONE) for f in frames]
q[0].save(os.path.join(HERE, "eminenttracker-logo-animated.gif"), save_all=True, append_images=q[1:], duration=48, loop=0, optimize=True, disposal=1)
small = [f.resize((200, 200), Image.LANCZOS).quantize(palette=palette, dither=Image.NONE) for f in frames]     # light enough for an email signature
small[0].save(os.path.join(HERE, "eminenttracker-logo-animated-small.gif"), save_all=True, append_images=small[1:], duration=48, loop=0, optimize=True, disposal=1)


def still():
    im = Image.new("RGB", (N, N), NAVY)
    d = ImageDraw.Draw(im)
    d.polygon([(X(x), Y(y)) for x, y in E], fill=WHITE)
    d.rectangle([X(12.2), Y(13.9), X(29.5), Y(18.1)], fill=ORANGE)
    r = 2.1 * SCALE
    d.ellipse([X(29.5) - r, Y(16) - r, X(29.5) + r, Y(16) + r], fill=ORANGE)
    return im.resize((S, S), Image.LANCZOS)


still().save(os.path.join(HERE, "eminenttracker-profile.png"))
