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


def frame(t):
    """t runs 0..1 over one loop: the bar shoots out, a ping rings from its tip, it rests, then it draws back."""
    im = Image.new("RGB", (N, N), NAVY)
    d = ImageDraw.Draw(im, "RGBA")
    grow = ease(t / 0.22) if t < 0.22 else 1.0 if t < 0.86 else 1 - ease((t - 0.86) / 0.14)
    end = 12.2 + (29.5 - 12.2) * grow
    for start in (0.20, 0.36):           # the ping: two rings from the bar's tip, fading as they widen
        if start <= t < start + 0.42:
            k = (t - start) / 0.42
            r, a = (1.5 + 13 * ease(k)) * SCALE, int(255 * (1 - k) ** 1.2)
            d.ellipse([X(29.5) - r, Y(16) - r, X(29.5) + r, Y(16) + r], outline=ORANGE + (a,), width=max(3, int(1.1 * SCALE * (1 - k) + 3)))
    d.polygon([(X(x), Y(y)) for x, y in E], fill=WHITE)
    if grow > 0.01:
        d.rectangle([X(12.2), Y(13.9), X(end), Y(18.1)], fill=ORANGE)
        if 0.22 <= t < 0.5:              # a highlight runs along the bar once
            hx = 12.2 + (end - 12.2) * (t - 0.22) / 0.28
            d.rectangle([X(max(12.2, hx - 2.2)), Y(13.9), X(min(end, hx + 0.6)), Y(18.1)], fill=GLOW + (230,))
        tip = 1.0 + (0.9 * math.sin(min(1, max(0, (t - 0.18) / 0.2)) * math.pi) if t < 0.4 else 0)
        r = tip * 2.1 * SCALE
        d.ellipse([X(end) - r, Y(16) - r, X(end) + r, Y(16) + r], fill=ORANGE)
    return im.resize((S, S), Image.LANCZOS)


FRAMES = 44
frames = [frame(i / FRAMES) for i in range(FRAMES)]
palette = frames[14].quantize(colors=48, method=Image.MEDIANCUT)
q = [f.quantize(palette=palette, dither=Image.NONE) for f in frames]
durations = [45] * FRAMES
durations[int(FRAMES * 0.84)] = 900     # rest on the finished mark before it draws back
q[0].save(os.path.join(HERE, "eminenttracker-logo-animated.gif"), save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True, disposal=1)
frame(0.80).save(os.path.join(HERE, "eminenttracker-profile.png"))
