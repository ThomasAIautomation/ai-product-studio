"""Sfeerbeelden: een AI maakt een LEGE scène, daarna zetten wij de echte tas erin.

Waarom zo: generatieve beeldmodellen tekenen een product altijd opnieuw. Hier komt de tas
nooit in het generatieve model; alleen de omgeving wordt gegenereerd. Daarna:
1. Een vision-model wijst het plaatsingspunt aan (tafelblad of hand) en een referentiemaat
   (tafeldiameter of schouderbreedte) in de scène.
2. Met de afmetingen van de tas (cm) wordt de schaal bepaald. De SCÈNE wordt geschaald naar
   de tas — de tas blijft op originele resolutie (schaal 1:1).
3. De originele tas-pixels worden geplakt en per pixel gecontroleerd (zoals bij de studiofoto's).
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import config, imaging

DEFAULT_DIMS_CM = {"breedte": 38.0, "hoogte": 28.0, "diepte": 12.0}  # aanname als maten ontbreken


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    size: str
    prompt: str
    locate_prompt: str
    reference_cm: float


PRESETS = {
    "parijs": Preset(
        key="parijs", label="Parijs – terrastafeltje", size="1536x1024",
        prompt=("Photorealistic luxury lifestyle photograph for a fashion webshop. A small round white marble bistro table "
                "on a classic Parisian cafe terrace, WIDE SHOT: the table is small in the frame, its top occupies only about 40% of "
                "the image width and sits in the lower third of the image, with lots of open space above it. Seen from "
                "slightly above table height. The table top is completely "
                "EMPTY and clear in the center (leave the center of the table free, no objects there). Soft, diffuse "
                "morning light in the shade of a cafe awning, gentle warm sunlight on the street behind. Blurred "
                "background: Haussmann facades, wicker bistro chairs, a small coffee cup at the far left edge of the "
                "table. Shallow depth of field, 50mm lens, high-end editorial look. No people, no bags, no text."),
        locate_prompt=("Je ziet een foto van een tafeltje. Geef UITSLUITEND JSON terug met genormaliseerde coördinaten "
                       "(0..1, x van links, y van boven) van de afgebeelde foto: "
                       '{"tafel_links_x": linkerrand tafelblad, "tafel_rechts_x": rechterrand tafelblad, '
                       '"plaats_x": horizontaal midden van de grootste VRIJE plek op het tafelblad, '
                       '"plaats_y": verticale positie op het tafelblad waar de onderkant van een tas zou staan '
                       '(ongeveer halverwege tussen achter- en voorrand van het blad)}'),
        reference_cm=60.0,  # gangbare diameter van een bistrotafel
    ),
    "model": Preset(
        key="model", label="Model – tas in de hand", size="1024x1536",
        prompt=("Photorealistic luxury fashion editorial photograph. An elegant adult woman in a minimal camel trench coat "
                "walking on a sunny Paris street, framed from head to knees, facing the camera. Her right arm hangs "
                "relaxed alongside her body and her right hand is EMPTY, slightly curled as if about to hold a bag "
                "handle, clearly visible at hip height on the left side of the image, with free space below the hand. "
                "Soft diffuse daylight, blurred Haussmann buildings, shallow depth of field, 85mm lens. "
                "She is NOT holding anything. No bags, no logos, no text."),
        locate_prompt=("Je ziet een fashionfoto van een persoon. Geef UITSLUITEND JSON terug met genormaliseerde "
                       "coördinaten (0..1, x van links, y van boven) van de afgebeelde foto: "
                       '{"hand_x": midden van de lege hand die naar beneden hangt, "hand_y": onderkant van die hand '
                       '(waar een hengsel zou liggen), "schouder_links_x": buitenrand linkerschouder, '
                       '"schouder_rechts_x": buitenrand rechterschouder}'),
        reference_cm=42.0,  # gemiddelde schouderbreedte volwassen vrouw
    ),
}


class SceneError(RuntimeError):
    pass


def _n8n_url(path: str) -> str:
    base = config.env("N8N_URL", "https://supplierresponse.app.n8n.cloud").rstrip("/")
    return f"{base}/webhook/ai-product-studio/{path}"


def generate_scene(preset: Preset, quality: str = "medium") -> Image.Image:
    r = httpx.post(_n8n_url("scene"), json={"prompt": preset.prompt, "size": preset.size, "quality": quality},
                   headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET}, timeout=300)
    if r.status_code >= 300 or not r.headers.get("content-type", "").startswith("image/"):
        raise SceneError(f"Scène genereren mislukt ({r.status_code}): {r.text[:200]}")
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def locate(scene: Image.Image, preset: Preset) -> dict:
    buf = io.BytesIO()
    small = scene.copy()
    small.thumbnail((1536, 1536))
    small.save(buf, "JPEG", quality=90)
    r = httpx.post(config.N8N_ANALYZE_WEBHOOK_URL, data={"prompt": preset.locate_prompt},
                   files={"image": ("scene.jpg", buf.getvalue(), "image/jpeg")},
                   headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET}, timeout=180)
    if r.status_code >= 300:
        raise SceneError(f"Plaatsing bepalen mislukt ({r.status_code})")
    text = r.json().get("text", "")
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    if not all(0 <= float(v) <= 1 for v in data.values()):
        raise SceneError(f"Ongeldige coördinaten: {data}")
    return {k: float(v) for k, v in data.items()}


def place(original: Image.Image, mask: Image.Image, scene: Image.Image, preset: Preset, anchors: dict,
          dims_cm: dict | None = None) -> tuple[Image.Image, dict]:
    dims = {**DEFAULT_DIMS_CM, **(dims_cm or {})}
    l, t, r, b = mask.point(lambda a: 255 if a > 8 else 0).getbbox()
    bag, bag_mask = original.crop((l, t, r, b)), mask.crop((l, t, r, b))
    W, H = scene.size

    if preset.key == "parijs":
        ref_px = (anchors["tafel_rechts_x"] - anchors["tafel_links_x"]) * W
        cx, bottom = anchors["plaats_x"] * W, anchors["plaats_y"] * H
    else:
        ref_px = abs(anchors["schouder_rechts_x"] - anchors["schouder_links_x"]) * W
        cx, top = anchors["hand_x"] * W, anchors["hand_y"] * H
    px_per_cm = ref_px / preset.reference_cm
    target_w = dims["breedte"] * px_per_cm           # gewenste tasbreedte in scène-pixels
    f = bag.width / target_w                         # scène-schaal zodat de tas 1:1 blijft
    scene_s = scene.resize((max(1, round(W * f)), max(1, round(H * f))), Image.LANCZOS)
    cx, ref = cx * f, None
    ox = round(cx - bag.width / 2)
    if preset.key == "parijs":
        oy = round(bottom * f - bag.height)
    else:
        oy = round(top * f - bag.height * 0.04)      # hengsel net onder de hand
    if oy < 0 or oy + bag.height > scene_s.height or ox < 0 or ox + bag.width > scene_s.width:
        raise SceneError("De tas past op ware grootte niet in deze scène (te groot voor het beeld). Probeer opnieuw "
                         "voor een nieuwe scène, of controleer de afmetingen.")

    full_mask = Image.new("L", scene_s.size, 0)
    full_mask.paste(bag_mask, (ox, oy))
    out = _shadow(scene_s, full_mask, table=preset.key == "parijs")
    out.paste(bag, (ox, oy), bag_mask)
    core = bag_mask.point(lambda a: 255 if a >= imaging.CORE_ALPHA else 0)
    out.paste(bag, (ox, oy), core)
    integrity = imaging.verify(bag, core, out, (ox, oy))
    integrity.update(scene=preset.key, dims_cm=dims, dims_assumed=not dims_cm,
                     scene_scale=round(f, 4), px_per_cm=round(px_per_cm * f, 2))
    return out, integrity


def _shadow(scene: Image.Image, full_mask: Image.Image, table: bool) -> Image.Image:
    """Zachte schaduw onder/achter de tas; raakt alleen scènepixels."""
    m = np.asarray(full_mask) > 128
    rows, cols = np.where(m)
    if not rows.size:
        return scene
    sh = Image.new("L", scene.size, 0)
    d = ImageDraw.Draw(sh)
    w = int(cols.max() - cols.min())
    bottom = int(rows.max())
    if table:
        # Contactschaduw (donker, smal) + zachte slagschaduw (breed, licht).
        h = max(8, int(w * 0.06))
        x0, x1 = int(cols.min()), int(cols.max())
        d.ellipse((x0 + w * 0.02, bottom - h * 0.5, x1 - w * 0.02, bottom + h * 0.5), fill=110)
        sh = sh.filter(ImageFilter.GaussianBlur(float(max(6, h))))
        contact = Image.new("L", scene.size, 0)
        ImageDraw.Draw(contact).rectangle((x0 + w * 0.04, bottom - 3, x1 - w * 0.04, bottom + 3), fill=200)
        sh = Image.fromarray(np.maximum(np.asarray(sh), np.asarray(contact.filter(ImageFilter.GaussianBlur(4.0)))))
        blur = 0.0
    else:
        sh.paste(full_mask.point(lambda a: 90 if a > 128 else 0), (int(w * 0.03), int(w * 0.04)))
        blur = max(6.0, w * 0.04)
    if blur:
        sh = sh.filter(ImageFilter.GaussianBlur(float(blur)))
    dark = Image.new("RGB", scene.size, (35, 30, 25))
    return Image.composite(dark, scene, sh)


def make_scene_image(original_path: Path, mask_path: Path, out_dir: Path, preset_key: str,
                     dims_cm: dict | None = None, scene: Image.Image | None = None) -> dict:
    preset = PRESETS[preset_key]
    original = imaging.load_image(original_path)
    mask = Image.open(mask_path).convert("L")
    scene = scene or generate_scene(preset)
    anchors = locate(scene, preset)
    out, integrity = place(original, mask, scene, preset, anchors, dims_cm)
    if not integrity["passed"]:
        raise SceneError(f"Integriteitscontrole mislukt: {integrity}")
    integrity["anchors"] = anchors
    path = imaging.save_lossless(out, out_dir / f"{original_path.stem}_sfeer_{preset_key}")
    return {"path": str(path), "integrity": integrity}
