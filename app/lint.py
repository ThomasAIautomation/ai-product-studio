"""Automatische tekstcontrole vóór publicatie. Geeft per probleem een niveau ('fout' blokkeert zonder bevestiging)."""
from __future__ import annotations

import re

BRANDS = ["gucci", "louis vuitton", "chanel", "prada", "hermès", "hermes", "dior", "fendi", "saint laurent", "ysl",
          "celine", "céline", "bottega veneta", "burberry", "balenciaga", "givenchy", "michael kors", "coach", "loewe",
          "valentino", "versace", "miu miu", "mulberry", "goyard", "dubbel-g", "dubbele g", "gg-", "lv-monogram",
          "cc-logo", "monogram canvas"]
CLAIMS = ["echt leer", "100%", "authentiek", "origineel", "gegarandeerd", "luxe", "designer", "nooit gebruikt"]
DEFECT_WORDS = ["slijtage", "beschadig", "vlek", "kras", "verkleur", "gebrek", "gebruikssporen", "rafel", "scheur",
                "plooi", "kreuk", "materiaalverlies", "staat"]


def _text(p: dict) -> str:
    return " ".join(str(p.get(k) or "") for k in ("title", "description_html", "seo_title", "seo_description")).lower()


def check(p: dict) -> list[dict]:
    """p: product zoals uit db.product_full (met 'analysis' en optioneel 'seller')."""
    issues: list[dict] = []
    add = lambda level, msg: issues.append({"level": level, "message": msg})
    seller = p.get("seller") or {}
    text = _text(p)
    title = p.get("title") or ""
    if not title.strip():
        add("fout", "Titel ontbreekt.")
    elif len(title) > 70:
        add("waarschuwing", f"Titel is lang ({len(title)} tekens); boven ±70 wordt hij afgekapt in Google en de webshop.")
    if len(p.get("seo_title") or "") > 60:
        add("waarschuwing", f"SEO-titel is {len(p['seo_title'])} tekens (max 60).")
    if len(p.get("seo_description") or "") > 155:
        add("waarschuwing", f"SEO-beschrijving is {len(p['seo_description'])} tekens (max 155).")
    if not seller.get("merk"):
        found = sorted({b for b in BRANDS if re.search(r"(?<![a-z])" + re.escape(b), text)})
        if found:
            add("fout", f"Merk(verwijzing) in de tekst zonder bevestigd merk: {', '.join(found)}. "
                        "Vul het merk in bij 'Wat jij weet' of haal de verwijzing weg.")
    found_claims = sorted({c for c in CLAIMS if c in text and not (c == "echt leer" and seller.get("materiaal"))})
    if found_claims and not seller.get("merk"):
        add("waarschuwing", f"Claims die bewijs vragen: {', '.join(found_claims)}.")
    defects = ((p.get("analysis") or {}).get("staat") or {}).get("zichtbare_gebreken") or []
    desc = (p.get("description_html") or "").lower()
    if defects and not any(w in desc for w in DEFECT_WORDS):
        add("fout", f"De AI zag {len(defects)} gebrek(en), maar de beschrijving noemt geen gebreken of staat.")
    if not p.get("price"):
        add("info", "Geen prijs ingevuld (wordt €0,00 in Shopify).")
    if not (p.get("dims") or seller.get("breedte")):
        add("info", "Geen afmetingen ingevuld; kopers willen die graag weten.")
    return issues
