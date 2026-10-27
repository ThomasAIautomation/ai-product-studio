"""Maakt fictieve testfoto's van een tas (met zichtbare kras) op een rommelige achtergrond."""
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

def make(path, seed=1, color=(120, 72, 40)):
    rng = np.random.default_rng(seed)
    w, h = 1600, 1200
    y = np.linspace(0, 1, h)[:, None, None]
    bg = (np.array([150, 140, 120]) * (1 - y) + np.array([90, 80, 70]) * y) * np.ones((h, w, 3))
    bg += rng.normal(0, 12, (h, w, 3))
    img = Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(25):  # rommel op de achtergrond
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        d.rectangle((x0, y0, x0 + rng.integers(20, 120), y0 + rng.integers(20, 80)), fill=tuple(int(v) for v in rng.integers(60, 200, 3)))
    bag = Image.new("RGBA", (w, h))
    bd = ImageDraw.Draw(bag)
    bd.polygon([(520, 520), (1080, 520), (1160, 1000), (440, 1000)], fill=color + (255,))
    bd.arc((620, 300, 980, 700), 180, 360, fill=(70, 40, 22, 255), width=34)
    bd.rectangle((760, 600, 840, 650), fill=(200, 180, 90, 255))  # sluiting
    tex = rng.normal(0, 9, (h, w, 1))
    arr = np.asarray(bag).astype(np.float32)
    arr[..., :3] = np.clip(arr[..., :3] + tex, 0, 255)
    bag = Image.fromarray(arr.astype(np.uint8))
    bd = ImageDraw.Draw(bag)
    bd.line([(600, 760), (700, 820), (735, 900)], fill=(205, 190, 170, 255), width=5)  # beschadiging/kras
    bd.ellipse((950, 840, 1000, 880), fill=(80, 50, 30, 255))  # vlek
    img.paste(bag, (0, 0), bag)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(path, quality=92)

if __name__ == "__main__":
    for i, p in enumerate(sys.argv[1:]):
        make(p, seed=i + 1, color=[(120, 72, 40), (30, 30, 34), (150, 40, 50)][i % 3])
