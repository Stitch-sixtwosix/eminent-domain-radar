"""Draws the LinkedIn banner: a planned project's line crossing a field of parcels, the ones it touches picked out.
Run: .venv/bin/python brand/make_banner.py (needs Pillow). LinkedIn shows a company banner at 1128 x 191."""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, SS = 2256, 382, 2                     # twice LinkedIn's size, drawn at twice that again and reduced
NAVY, LINE, HIT, WHITE, ORANGE = (27, 35, 51), (58, 70, 92), (74, 88, 112), (255, 255, 255), (224, 102, 46)
w, h = W * SS, H * SS
im = Image.new("RGB", (w, h), NAVY)
d = ImageDraw.Draw(im, "RGBA")
random.seed(11)

# the project's alignment: a road running left to right with a gentle bend
road = lambda x: h * 0.50 + math.sin(x / w * math.pi * 1.1 - 0.9) * h * 0.16 + (x / w) * h * 0.26       # stays low on the right, under the words
slope = lambda x: (road(x + 10) - road(x - 10)) / 20

# parcels: lots of uneven width on both sides of the road, in two rows each side
x = -200.0
while x < w + 200:
    lot = random.uniform(70, 190) * SS
    for side in (-1, 1):
        depth0 = 14 * SS
        for row in range(3):
            depth = random.uniform(70, 120) * SS if row < 2 else random.uniform(150, 260) * SS
            pts = []
            for px, off in ((x, depth0), (x + lot, depth0), (x + lot, depth0 + depth), (x, depth0 + depth)):
                a = math.atan(slope(px))
                pts.append((px - math.sin(a) * off * side, road(px) + math.cos(a) * off * side))
            touching = row == 0
            d.polygon(pts, fill=(HIT + (120,)) if touching and random.random() < 0.8 else None, outline=LINE + (255,))
            d.line(pts + [pts[0]], fill=(LINE if not touching else (120, 136, 160)) + (255,), width=2 * SS)
            depth0 += depth
    x += lot

# the road itself, in the orange of the logo's bar
pts = [(px, road(px)) for px in range(-50, w + 60, 12)]
d.line(pts, fill=ORANGE + (255,), width=11 * SS, joint="curve")

# a darker field behind the words so they read over the map
shade = Image.new("L", (w, h), 0)
sd = ImageDraw.Draw(shade)
for i in range(w):
    t = max(0.0, min(1.0, (i / w - 0.30) / 0.22))
    sd.line([(i, 0), (i, h)], fill=int(225 * t))
im.paste(Image.new("RGB", (w, h), NAVY), (0, 0), shade)
d = ImageDraw.Draw(im, "RGBA")
d.line([(px, road(px)) for px in range(-50, w + 60, 12)], fill=ORANGE + (255,), width=11 * SS, joint="curve")


def font(size, weight):
    for path, index in (("/System/Library/Fonts/Avenir Next.ttc", {"bold": 0, "regular": 7}[weight]), ("/System/Library/Fonts/HelveticaNeue.ttc", {"bold": 1, "regular": 0}[weight])):
        try:
            return ImageFont.truetype(path, size * SS, index=index)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


big, small = font(62, "bold"), font(35, "regular")
right = w - 110 * SS
lines = [("Planned public projects,", big, WHITE), ("mapped to the parcels and owners in their path.", small, (205, 213, 224))]
y = 62 * SS
for text, f, colour in lines:
    tw = d.textlength(text, font=f)
    box = d.textbbox((0, 0), text, font=f)
    d.text((right - tw, y), text, font=f, fill=colour + (255,))
    y += (box[3] - box[1]) + 34 * SS

im.resize((W, H), Image.LANCZOS).save(os.path.join(HERE, "eminenttracker-linkedin-banner.png"))
