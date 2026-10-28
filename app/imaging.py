"""Productfoto-bewerking met pixelgarantie.

Principe: de tas zelf wordt NOOIT door een generatief model opnieuw getekend.
1. Een segmentatiemodel (rembg / IS-Net, lokaal en gratis) bepaalt welke pixels bij de tas horen.
   Een tweede model (U²-Net) controleert of er geen deel van de tas (hengsel, riem) is weggevallen;
   kleine gaten in het product (vaak krassen/vlekken) worden met originele pixels opgevuld.
2. Alleen de omgeving wordt vervangen (studio-achtergrond, schaduw of een sfeerfoto/modelfoto).
3. De originele tas-pixels worden 1-op-1, op originele resolutie, zonder schalen of
   kleurcorrectie teruggezet. Beschadigingen, krassen en slijtage blijven dus exact zichtbaar.
4. Na het samenstellen wordt per pixel gecontroleerd dat elke tas-pixel identiek is aan het
   origineel (integriteitscontrole). Afwijking = de afbeelding wordt afgekeurd.
5. Opslag is lossless (PNG, of lossless WebP als PNG te groot is voor Shopify) — geen JPEG-artefacten.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

CORE_ALPHA = 250          # pixels met alpha >= dit zijn "tas" en worden exact behouden
MARGIN = 0.12             # witruimte rond de tas, relatief aan de canvasgrootte
SHOPIFY_MAX_BYTES = 20 * 1024 * 1024

PRIMARY_MODEL = "isnet-general-use"   # in tests het nauwkeurigst op hengsels/riemen
CHECK_MODEL = "u2net"                 # tweede, onafhankelijk model voor de volledigheidscontrole
_sessions: dict = {}


def _rembg_session(name: str):
    if name not in _sessions:
        from rembg import new_session
        _sessions[name] = new_session(name)
    return _sessions[name]


def load_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # alleen rotatie volgens camera-EXIF, geen pixelbewerking
    return img.convert("RGB")


def _raw_mask(img: Image.Image, model: str) -> np.ndarray:
    from rembg import remove
    return np.asarray(remove(img, session=_rembg_session(model)).getchannel("A"))


def _refine(alpha: np.ndarray) -> tuple[np.ndarray, dict]:
    """Verwijder losse snippers achtergrond en vul kleine gaten ín het product.

    Kleine gaten zijn vaak juist beschadigingen (kras, vlek, slijtplek) die een segmentatiemodel
    ten onrechte als achtergrond ziet. Die moeten zichtbaar blijven, dus worden ze opgevuld met
    de originele pixels. Grote gaten (bijv. de ruimte binnen een hengsel) blijven open.
    """
    from scipy import ndimage
    solid = alpha > 128
    labels, n = ndimage.label(solid)
    report = {"islands_removed": 0, "holes_filled": 0}
    if n == 0:
        return alpha, report
    sizes = ndimage.sum(solid, labels, range(1, n + 1))
    largest = sizes.max()
    keep = np.zeros(n + 1, bool)
    keep[1:] = sizes >= 0.005 * largest
    report["islands_removed"] = int((~keep[1:]).sum())
    report["separate_parts"] = int(keep[1:].sum())
    alpha = np.where(keep[labels], alpha, 0).astype(np.uint8)

    holes, nh = ndimage.label(~(alpha > 128))
    border = set(np.unique(np.concatenate([holes[0], holes[-1], holes[:, 0], holes[:, -1]])))
    hole_sizes = ndimage.sum(np.ones_like(holes), holes, range(1, nh + 1))
    product_area = (alpha > 128).sum()
    for idx, size in enumerate(hole_sizes, 1):
        if idx not in border and size < 0.02 * product_area:
            alpha[holes == idx] = 255
            report["holes_filled"] += 1
    return alpha, report


def segment(img: Image.Image) -> tuple[Image.Image, dict]:
    """Alpha-masker (255 = tas) + rapport van de volledigheidscontrole."""
    primary, report = _refine(_raw_mask(img, PRIMARY_MODEL))
    check, _ = _refine(_raw_mask(img, CHECK_MODEL))
    p, c = primary > 128, check > 128
    area = max(int(p.sum()), 1)
    # Pixels die het controlemodel wél als product ziet maar het hoofdmodel niet = mogelijk ontbrekend deel.
    report["possibly_missing_ratio"] = round(float((c & ~p).sum()) / area, 4)
    report["agreement_iou"] = round(float((p & c).sum()) / max(int((p | c).sum()), 1), 4)
    report["review_required"] = bool(
        report["possibly_missing_ratio"] > 0.01 or report["islands_removed"] or report.get("separate_parts", 1) > 1
    )
    return Image.fromarray(primary, "L"), report


@dataclass
class Composite:
    image: Image.Image
    mask: Image.Image
    offset: tuple[int, int]
    integrity: dict


def _bbox(mask: Image.Image) -> tuple[int, int, int, int]:
    bbox = mask.point(lambda a: 255 if a > 8 else 0).getbbox()
    if not bbox:
        raise ValueError("Geen product gevonden in de foto (segmentatie leeg).")
    return bbox


def _studio_background(size: tuple[int, int], style: str) -> Image.Image:
    w, h = size
    if style == "wit":
        return Image.new("RGB", size, (255, 255, 255))
    # Zachte studio-gradient: licht warmgrijs boven naar wit bij de vloer.
    y = np.linspace(0, 1, h)[:, None]
    top, bottom = np.array([236, 233, 228]), np.array([250, 249, 247])
    grad = (top * (1 - y) + bottom * y)[:, None, :].repeat(w, axis=1)
    x = np.linspace(-1, 1, w)[None, :]
    vignette = 1 - 0.05 * (x ** 2)
    arr = np.clip(grad * vignette[:, :, None], 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _contact_shadow(canvas: Image.Image, mask_on_canvas: Image.Image) -> Image.Image:
    """Zachte vloerschaduw onder het product. Raakt alleen achtergrondpixels."""
    w, h = canvas.size
    m = np.asarray(mask_on_canvas, dtype=np.float32) / 255
    rows = np.where(m.max(axis=1) > 0.5)[0]
    cols = np.where(m.max(axis=0) > 0.5)[0]
    bottom, left, right = rows.max(), cols.min(), cols.max()
    shadow = Image.new("L", (w, h), 0)
    ell_h = max(6, int((right - left) * 0.06))
    from PIL import ImageDraw
    ImageDraw.Draw(shadow).ellipse(
        (left + (right - left) * 0.04, bottom - ell_h // 2, right - (right - left) * 0.04, bottom + ell_h // 2),
        fill=110,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(4, ell_h)))
    dark = Image.new("RGB", (w, h), (60, 55, 50))
    return Image.composite(dark, canvas, shadow)


def compose(original: Image.Image, mask: Image.Image, style: str = "studio",
            scene: Image.Image | None = None, anchor: tuple[float, float] | None = None) -> Composite:
    """Zet de originele tas-pixels onveranderd op een nieuwe achtergrond.

    style: "wit" | "studio" | "scene" (scene = sfeer-/modelfoto, op schaal van de tas gebracht).
    anchor: (x, y) relatief punt in de scene waar het midden-boven van de tas (hengsel) komt,
            bijv. de hand van het model.
    """
    l, t, r, b = _bbox(mask)
    bag = original.crop((l, t, r, b))
    bag_mask = mask.crop((l, t, r, b))
    bw, bh = bag.size

    if style == "scene":
        if scene is None:
            raise ValueError("Stijl 'scene' vereist een sfeerfoto in data/scenes/.")
        # De scène wordt geschaald naar de tas — nooit andersom.
        target_h = int(bh / 0.55)
        scale = target_h / scene.height
        bg = scene.convert("RGB").resize((max(int(scene.width * scale), bw + 2), target_h), Image.LANCZOS)
        ax, ay = anchor or (0.5, 0.3)
        ox = int(bg.width * ax - bw / 2)
        oy = int(bg.height * ay)
        ox = min(max(ox, 0), bg.width - bw)
        oy = min(max(oy, 0), bg.height - bh)
    else:
        side = int(max(bw, bh) / (1 - 2 * MARGIN))
        bg = _studio_background((side, side), style)
        ox, oy = (side - bw) // 2, int((side - bh) * 0.55)

    full_mask = Image.new("L", bg.size, 0)
    full_mask.paste(bag_mask, (ox, oy))
    if style != "scene":
        bg = _contact_shadow(bg, full_mask)

    out = bg.copy()
    out.paste(bag, (ox, oy), bag_mask)
    # Harde garantie: kern-pixels van de tas worden letterlijk gekopieerd (geen alpha-mix).
    core = bag_mask.point(lambda a: 255 if a >= CORE_ALPHA else 0)
    out.paste(bag, (ox, oy), core)

    integrity = verify(original.crop((l, t, r, b)), core, out, (ox, oy))
    return Composite(out, full_mask, (ox, oy), integrity)


def verify(bag: Image.Image, core: Image.Image, out: Image.Image, offset: tuple[int, int]) -> dict:
    """Vergelijk elke tas-pixel in het resultaat met het origineel."""
    ox, oy = offset
    src = np.asarray(bag, dtype=np.int16)
    dst = np.asarray(out.crop((ox, oy, ox + bag.width, oy + bag.height)), dtype=np.int16)
    sel = np.asarray(core) > 0
    diff = np.abs(src - dst)[sel]
    mismatches = int((diff.max(axis=1) > 0).sum()) if diff.size else 0
    return {
        "product_pixels": int(sel.sum()),
        "mismatched_pixels": mismatches,
        "max_channel_diff": int(diff.max()) if diff.size else 0,
        "scale": 1.0,
        "passed": mismatches == 0 and int(sel.sum()) > 0,
    }


def save_lossless(img: Image.Image, path_stem: Path) -> Path:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if buf.tell() <= SHOPIFY_MAX_BYTES:
        path = path_stem.with_suffix(".png")
    else:
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=True, quality=100, method=4)
        path = path_stem.with_suffix(".webp")
    path.write_bytes(buf.getvalue())
    # Controle dat het opgeslagen bestand exact gelijk is aan wat we samenstelden.
    with Image.open(path) as reread:
        if not np.array_equal(np.asarray(reread.convert("RGB")), np.asarray(img)):
            raise RuntimeError(f"Lossless opslag mislukt voor {path.name}")
    return path


def pick_scene() -> tuple[Image.Image, tuple[float, float]] | None:
    """Eerste sfeerfoto in data/scenes. Optioneel ankerpunt in bestandsnaam: naam__x0.42_y0.35.jpg"""
    from .config import SCENE_DIR
    files = sorted(p for p in SCENE_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if not files:
        return None
    p = files[0]
    anchor = (0.5, 0.3)
    if "__x" in p.stem:
        try:
            parts = p.stem.split("__x")[1].split("_y")
            anchor = (float(parts[0]), float(parts[1]))
        except (IndexError, ValueError):
            pass
    return Image.open(p).convert("RGB"), anchor


def photo_quality(img: Image.Image, mask: Image.Image) -> list[str]:
    """Signaleert foto's die opnieuw gemaakt moeten worden. Wijzigt niets aan de foto."""
    from scipy import ndimage
    issues = []
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    m = np.asarray(mask) > 128
    if min(img.size) < 1000:
        issues.append(f"Lage resolutie ({img.width}×{img.height}); minimaal 1000 px aan de korte zijde aanbevolen.")
    if m.any():
        sharp = float(ndimage.laplace(gray)[m].var())
        if sharp < 15:
            issues.append(f"Foto lijkt onscherp (scherptescore {sharp:.0f}).")
        mean = float(gray[m].mean())
        if mean < 40:
            issues.append("Product is erg donker belicht; details/beschadigingen kunnen wegvallen.")
        if (gray[m] > 250).mean() > 0.05:
            issues.append("Delen van het product zijn overbelicht (uitgebeten wit).")
        rows, cols = np.where(m)
        h, w = m.shape
        edge = 2
        if rows.min() < edge or cols.min() < edge or rows.max() >= h - edge or cols.max() >= w - edge:
            issues.append("Product raakt de rand van de foto — mogelijk valt een deel buiten beeld.")
        if m.mean() < 0.04:
            issues.append("Product beslaat maar een klein deel van de foto; ga dichterbij staan.")
    return issues


def process_photo(src: Path, out_dir: Path, style: str, manual_mask: Image.Image | None = None) -> dict:
    """manual_mask: handmatig gecorrigeerde uitsnede uit het dashboard (vervangt de AI-segmentatie)."""
    original = load_image(src)
    if manual_mask is not None:
        if manual_mask.size != original.size:
            manual_mask = manual_mask.resize(original.size, Image.NEAREST)
        mask = manual_mask.convert("L")
        completeness = {"manual": True, "review_required": False, "possibly_missing_ratio": 0.0,
                        "agreement_iou": 1.0, "islands_removed": 0, "holes_filled": 0}
    else:
        mask, completeness = segment(original)
        area = (np.asarray(mask) > 128).mean()
        if area < 0.01 or completeness["agreement_iou"] < 0.1:
            raise ValueError(f"Geen tas herkend op deze foto (productoppervlak {area:.1%}, modellen "
                             f"{completeness['agreement_iou']:.0%} eens). Controleer of de juiste foto is geüpload.")
    scene = anchor = None
    if style == "scene":
        picked = pick_scene()
        if picked is None:
            raise ValueError("Geen sfeerfoto gevonden in data/scenes — upload er een via het dashboard.")
        scene, anchor = picked
    comp = compose(original, mask, style, scene, anchor)
    if not comp.integrity["passed"]:
        raise RuntimeError(f"Integriteitscontrole mislukt: {comp.integrity}")
    out = save_lossless(comp.image, out_dir / f"{src.stem}_{style}")
    mask_path = out_dir / f"{src.stem}_mask.png"
    mask.save(mask_path)
    integrity = comp.integrity | {"completeness": completeness, "quality_issues": photo_quality(original, mask)}
    return {"processed_path": str(out), "mask_path": str(mask_path), "integrity": integrity}
