"""Productanalyse + Nederlandse teksten via Claude (vision, structured output).

Regel: alleen beschrijven wat zichtbaar is. Alles wat niet uit de foto's te bewijzen is
(materiaal, merk-echtheid, afmetingen, herkomst) gaat naar `niet_vast_te_stellen` en komt
niet als feit in de producttekst.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import anthropic
from PIL import Image

from . import config

CONFIDENCE = {"type": "string", "enum": ["hoog", "middel", "laag"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "waargenomen_kenmerken", "staat", "niet_vast_te_stellen", "titel", "korte_omschrijving",
        "beschrijving_html", "producttype", "categorie", "tags", "seo_titel", "seo_beschrijving",
        "handle", "waarschuwingen",
    ],
    "properties": {
        "waargenomen_kenmerken": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kenmerk", "waarde", "zekerheid", "bewijs"],
                "properties": {
                    "kenmerk": {"type": "string"}, "waarde": {"type": "string"},
                    "zekerheid": CONFIDENCE, "bewijs": {"type": "string"},
                },
            },
        },
        "staat": {
            "type": "object", "additionalProperties": False,
            "required": ["beoordeling", "zichtbare_gebreken"],
            "properties": {
                "beoordeling": {"type": "string", "enum": ["nieuw ogend", "zeer goed", "goed", "redelijk", "matig", "onbekend"]},
                "zichtbare_gebreken": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["omschrijving", "locatie", "foto"],
                        "properties": {"omschrijving": {"type": "string"}, "locatie": {"type": "string"}, "foto": {"type": "integer"}},
                    },
                },
            },
        },
        "niet_vast_te_stellen": {"type": "array", "items": {"type": "string"}},
        "titel": {"type": "string"},
        "korte_omschrijving": {"type": "string"},
        "beschrijving_html": {"type": "string"},
        "producttype": {"type": "string"},
        "categorie": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "seo_titel": {"type": "string"},
        "seo_beschrijving": {"type": "string"},
        "handle": {"type": "string"},
        "waarschuwingen": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """Je bent productredacteur voor een Nederlandse webshop in (tweedehands en nieuwe) tassen.
Je krijgt één of meer foto's van precies één product.

Strikte regels:
- Beschrijf alleen wat aantoonbaar zichtbaar is. Geef bij elk kenmerk de zekerheid en waarop je het baseert (welke foto, welk detail).
- Materiaal: noem iets alleen "leer", "suède", "canvas" enz. als een label/tekst op de foto dat bevestigt. Anders formuleer je visueel ("leerlook", "glad oppervlak", "lijkt op") en zet je het materiaal in niet_vast_te_stellen.
- Merk: noem alleen een merk als een logo/label leesbaar is, en voeg dan altijd een waarschuwing toe dat echtheid niet is geverifieerd. Gebruik geen merknaam in titel of SEO-tekst zonder die bevestiging.
- Afmetingen, gewicht, herkomst, bouwjaar, prijs en originaliteit kun je niet uit een foto halen: zet ze in niet_vast_te_stellen.
- Zichtbare beschadigingen, slijtage, vlekken en krassen MOETEN eerlijk in staat.zichtbare_gebreken én in de beschrijving staan. Verzwijg of verzacht niets.
- Schrijf in natuurlijk, verkoopgericht maar eerlijk Nederlands. Geen overdreven claims ("hoogwaardig", "luxe", "duurzaam") die je niet kunt onderbouwen.
- beschrijving_html: korte alinea + <ul> met zichtbare kenmerken + een alinea "Staat" met de gebreken. Alleen <p>, <ul>, <li>, <strong>.
- seo_titel max 60 tekens, seo_beschrijving max 155 tekens, handle in kleine letters met koppeltekens.
- categorie: een Shopify-achtige categorie zoals "Tassen > Handtassen" of "Tassen > Rugzakken".
- Is iets op de foto onduidelijk (onscherp, slecht licht, product deels buiten beeld), meld dat in waarschuwingen."""


class VisionNotConfigured(RuntimeError):
    pass


def _encode(path: Path, max_side: int = 1568) -> dict:
    # Verkleinde kopie alléén voor de analyse; het origineel blijft onaangeroerd.
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = config.MODEL_PRICES.get(model, (0.0, 0.0))
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


class TransientVisionError(RuntimeError):
    pass


def contact_sheet(photo_paths: list[Path], max_side: int = 2048) -> bytes:
    """Alle foto's van één product samen in één genummerd overzichtsbeeld (alleen voor de analyse)."""
    from PIL import ImageDraw
    imgs = [Image.open(p).convert("RGB") for p in photo_paths[:6]]
    cols = 1 if len(imgs) == 1 else 2
    rows = (len(imgs) + cols - 1) // cols
    cell = max_side // cols
    sheet = Image.new("RGB", (cell * cols, cell * rows), "white")
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(imgs):
        im.thumbnail((cell - 8, cell - 48))
        x, y = (i % cols) * cell, (i // cols) * cell
        sheet.paste(im, (x + (cell - im.width) // 2, y + 44))
        draw.rectangle((x + 4, y + 4, x + 150, y + 40), fill="black")
        draw.text((x + 14, y + 10), f"Foto {i + 1}", fill="white", font_size=24)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    missing = [k for k in SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"AI-antwoord mist velden: {missing}")
    data.setdefault("staat", {}).setdefault("zichtbare_gebreken", [])
    return data


def analyze_via_n8n(photo_paths: list[Path], hint: str = "") -> tuple[dict, dict]:
    import httpx
    n = min(len(photo_paths), 6)
    prompt = (SYSTEM + f"\n\nJe krijgt één overzichtsafbeelding met {n} genummerde foto's (Foto 1..{n}) van hetzelfde product."
              + "\nAntwoord UITSLUITEND met één JSON-object (geen uitleg, geen markdown) dat voldoet aan dit JSON-schema:\n"
              + json.dumps(SCHEMA, ensure_ascii=False)
              + (f"\nExtra info van de verkoper (niet geverifieerd): {hint}" if hint else ""))
    try:
        r = httpx.post(config.N8N_ANALYZE_WEBHOOK_URL, data={"prompt": prompt},
                       files={"image": ("product.jpg", contact_sheet(photo_paths), "image/jpeg")},
                       headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET}, timeout=180)
    except httpx.TransportError as e:
        raise TransientVisionError(f"n8n onbereikbaar: {e}") from e
    if r.status_code >= 500 or r.status_code == 429:
        raise TransientVisionError(f"n8n-analyse {r.status_code}: {r.text[:200]}")
    if r.status_code >= 300:
        raise RuntimeError(f"n8n-analyse {r.status_code}: {r.text[:300]}")
    body = r.json()
    try:
        data = _parse_json(body.get("text") or "")
    except (ValueError, json.JSONDecodeError) as e:
        raise TransientVisionError(f"Ongeldig AI-antwoord, opnieuw proberen: {e}") from e
    usage = body.get("usage") or {}
    return data, {"model": f"{body.get('model', 'gpt-6-luna')} (n8n Gateway)",
                  "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                  "cost_usd": None}


def analyze(photo_paths: list[Path], hint: str = "") -> tuple[dict, dict]:
    """Retourneert (analyse, usage). Gooit fouten door; worker bepaalt of retry zinvol is."""
    if config.VISION_PROVIDER == "n8n":
        if not config.N8N_ANALYZE_WEBHOOK_URL:
            raise VisionNotConfigured("N8N_ANALYZE_WEBHOOK_URL ontbreekt — AI-analyse staat uit.")
        return analyze_via_n8n(photo_paths, hint)
    if not config.ANTHROPIC_API_KEY:
        raise VisionNotConfigured("ANTHROPIC_API_KEY ontbreekt — AI-analyse staat uit.")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0, timeout=180)
    content: list[dict] = []
    for i, p in enumerate(photo_paths, 1):
        content.append({"type": "text", "text": f"Foto {i}:"})
        content.append(_encode(p))
    content.append({"type": "text", "text": "Analyseer dit product volgens de regels."
                    + (f"\nExtra info van de verkoper (niet geverifieerd): {hint}" if hint else "")})

    response = client.messages.create(
        model=config.VISION_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Model weigerde de analyse (refusal).")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Analyse afgekapt (max_tokens).")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    model = getattr(response, "model", config.VISION_MODEL)
    usage = {
        "model": model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": cost_usd(model, response.usage.input_tokens, response.usage.output_tokens),
    }
    return data, usage


def is_transient(exc: Exception) -> bool:
    return isinstance(exc, (TransientVisionError, anthropic.RateLimitError, anthropic.APIConnectionError,
                            anthropic.InternalServerError, anthropic.APITimeoutError)) or (
        isinstance(exc, anthropic.APIStatusError) and exc.status_code in (408, 409, 429, 529)
    )
