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

# Vaste staatschaal (zoals gangbaar bij tweedehands-luxeplatforms).
CONDITION_SCALE = ["nieuw", "zo goed als nieuw", "zeer goed", "goed", "redelijk", "onbekend"]
CONDITION_DEFINITIONS = """Staatschaal (kies precies één):
- nieuw: ongedragen, eventueel met labels; geen enkel gebruiksspoor zichtbaar.
- zo goed als nieuw: nauwelijks gedragen; hooguit minimale sporen die je alleen van dichtbij ziet.
- zeer goed: licht gedragen; kleine gebruikssporen (lichte krasjes op beslag, minimale slijtage aan hoeken).
- goed: duidelijk gedragen; zichtbare slijtage, lichte vlekken of verkleuring, maar zonder grote schade.
- redelijk: flinke gebruikssporen of schade (scheuren, rafels, grote vlekken, kapotte onderdelen).
- onbekend: de foto's zijn onvoldoende om de staat te beoordelen."""

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
                "beoordeling": {"type": "string", "enum": CONDITION_SCALE},
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
- Is iets op de foto onduidelijk (onscherp, slecht licht, product deels buiten beeld), meld dat in waarschuwingen.
- Verwijs niet naar merkpatronen of -kenmerken (zoals "dubbel-G", "LV-monogram", "CC-logo") in titel, beschrijving of SEO,
  tenzij de verkoper het merk heeft bevestigd. Beschrijf het patroon neutraal ("herhalend monogrampatroon").
- Informatie die door de verkoper is BEVESTIGD mag je als feit gebruiken en hoort in de tekst (afmetingen, materiaal, merk, bouwjaar, accessoires).
""" + "\n" + CONDITION_DEFINITIONS


STYLE_FILE = Path(__file__).resolve().parent.parent / "prompts" / "schrijfstijl.md"


def system_prompt() -> str:
    """SYSTEM + eventuele voorbeeldteksten van de eigenaar (prompts/schrijfstijl.md, regels met # worden genegeerd)."""
    try:
        examples = "\n".join(l for l in STYLE_FILE.read_text().splitlines() if not l.lstrip().startswith("#")).strip()
    except OSError:
        examples = ""
    if not examples:
        return SYSTEM
    return (SYSTEM + "\n\nSchrijfstijl: schrijf in dezelfde toon, zinslengte en opbouw als deze voorbeeldteksten van de "
            "eigenaar (neem GEEN feiten uit de voorbeelden over):\n" + examples[:6000])


def confirmed_text(info: dict | None) -> str:
    """Door de verkoper bevestigde gegevens als prompttekst."""
    if not info:
        return ""
    labels = {"merk": "Merk (door verkoper gecontroleerd)", "materiaal": "Materiaal (volgens label)",
              "breedte": "Breedte (cm)", "hoogte": "Hoogte (cm)", "diepte": "Diepte (cm)",
              "draaglengte": "Draaglengte/hengselhoogte (cm)", "bouwjaar": "Bouwjaar/periode",
              "accessoires": "Meegeleverd", "notities": "Overige opmerkingen verkoper"}
    rows = [f"- {labels.get(k, k)}: {v}" for k, v in info.items() if v not in (None, "", [])]
    return ("\nBEVESTIGDE informatie van de verkoper (mag als feit gebruikt worden):\n" + "\n".join(rows)) if rows else ""


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


def _n8n_vision(prompt: str, photo_paths: list[Path]) -> tuple[str, dict]:
    import httpx
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
    usage = body.get("usage") or {}
    return body.get("text") or "", {"model": f"{body.get('model', 'gpt-6-luna')} (n8n Gateway)",
                                    "input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0), "cost_usd": None}


def analyze_via_n8n(photo_paths: list[Path], confirmed: dict | None = None) -> tuple[dict, dict]:
    n = min(len(photo_paths), 6)
    prompt = (system_prompt() + f"\n\nJe krijgt één overzichtsafbeelding met {n} genummerde foto's (Foto 1..{n}) van hetzelfde product."
              + "\nAntwoord UITSLUITEND met één JSON-object (geen uitleg, geen markdown) dat voldoet aan dit JSON-schema:\n"
              + json.dumps(SCHEMA, ensure_ascii=False) + confirmed_text(confirmed))
    text, usage = _n8n_vision(prompt, photo_paths)
    try:
        data = _parse_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise TransientVisionError(f"Ongeldig AI-antwoord, opnieuw proberen: {e}") from e
    return data, usage


FACTCHECK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["oordeel", "problemen", "titel", "beschrijving_html", "seo_titel", "seo_beschrijving"],
    "properties": {
        "oordeel": {"type": "string", "enum": ["ok", "aangepast"]},
        "problemen": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "required": ["tekst", "reden"],
                                                  "properties": {"tekst": {"type": "string"}, "reden": {"type": "string"}}}},
        "titel": {"type": "string"}, "beschrijving_html": {"type": "string"},
        "seo_titel": {"type": "string"}, "seo_beschrijving": {"type": "string"},
    },
}


def factcheck(analysis: dict, photo_paths: list[Path], confirmed: dict | None = None) -> tuple[dict, dict]:
    """Tweede AI-ronde: een strenge eindredacteur controleert elke bewering tegen de foto's."""
    texts = {k: analysis.get(k) for k in ("titel", "beschrijving_html", "seo_titel", "seo_beschrijving")}
    prompt = ("Je bent een strenge eindredacteur en factchecker voor een Nederlandse tassenwebshop. Hieronder staan "
              "productteksten die een collega schreef, plus de foto's van het product en de lijst zichtbare gebreken.\n"
              "Controleer ELKE bewering:\n"
              "- Is het zichtbaar op de foto's of bevestigd door de verkoper? Zo niet: verwijderen of neutraal herformuleren.\n"
              "- Staan ALLE zichtbare gebreken eerlijk in de beschrijving? Zo niet: toevoegen.\n"
              "- Geen merknamen of merkpatronen (dubbel-G, monogram van een merk) zonder bevestiging door de verkoper.\n"
              "- Geen claims als 'echt leer', 'authentiek', 'origineel', '100%', 'luxe' zonder bevestiging.\n"
              "- seo_titel max 60 tekens, seo_beschrijving max 155 tekens. Behoud toon en HTML-opbouw.\n"
              "Geef 'oordeel': 'ok' als niets hoeft te veranderen (geef de teksten dan ongewijzigd terug), anders 'aangepast'.\n"
              "Antwoord UITSLUITEND met JSON volgens dit schema:\n" + json.dumps(FACTCHECK_SCHEMA, ensure_ascii=False)
              + "\n\nTeksten:\n" + json.dumps(texts, ensure_ascii=False)
              + "\n\nZichtbare gebreken volgens de analyse:\n"
              + json.dumps(analysis.get("staat", {}).get("zichtbare_gebreken", []), ensure_ascii=False)
              + confirmed_text(confirmed))
    text, usage = _n8n_vision(prompt, photo_paths)
    try:
        t = text.strip()
        data = json.loads(t[t.find("{"): t.rfind("}") + 1])
        missing = [k for k in FACTCHECK_SCHEMA["required"] if k not in data]
        if missing:
            raise ValueError(f"factcheck mist velden: {missing}")
    except (ValueError, json.JSONDecodeError) as e:
        raise TransientVisionError(f"Ongeldig factcheck-antwoord: {e}") from e
    return data, usage


def analyze(photo_paths: list[Path], confirmed: dict | None = None) -> tuple[dict, dict]:
    """Retourneert (analyse, usage). Gooit fouten door; worker bepaalt of retry zinvol is."""
    if config.VISION_PROVIDER == "n8n":
        if not config.N8N_ANALYZE_WEBHOOK_URL:
            raise VisionNotConfigured("N8N_ANALYZE_WEBHOOK_URL ontbreekt — AI-analyse staat uit.")
        return analyze_via_n8n(photo_paths, confirmed)
    if not config.ANTHROPIC_API_KEY:
        raise VisionNotConfigured("ANTHROPIC_API_KEY ontbreekt — AI-analyse staat uit.")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0, timeout=180)
    content: list[dict] = []
    for i, p in enumerate(photo_paths, 1):
        content.append({"type": "text", "text": f"Foto {i}:"})
        content.append(_encode(p))
    content.append({"type": "text", "text": "Analyseer dit product volgens de regels."
                    + confirmed_text(confirmed)})

    response = client.messages.create(
        model=config.VISION_MODEL,
        max_tokens=16000,
        system=system_prompt(),
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
