"""Beheerdersmeldingen: bundelt meldingen en stuurt ze via de n8n-workflow "Beheerdersmelding (e-mail)".

- Meldingen worden 60 s verzameld en daarna in één mail verstuurd (geen mailstorm bij een batch).
- `key` voorkomt herhaling: dezelfde melding wordt binnen `cooldown` seconden niet opnieuw verstuurd.
- Lukt versturen niet, dan blijft de melding in de wachtrij en wordt het later opnieuw geprobeerd.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from . import config

log = logging.getLogger("studio.alerts")
LEVELS = ["info", "waarschuwing", "fout", "kritiek"]
_buffer: list[dict] = []
_sent: dict[str, float] = {}
_lock = threading.Lock()


def link(pid: int | None) -> str | None:
    base = config.PUBLIC_BASE_URL or "http://localhost:8000"
    return f"{base}/#product-{pid}" if pid else None


def notify(level: str, title: str, message: str, product_id: int | None = None,
           key: str | None = None, cooldown: float = 6 * 3600) -> bool:
    """Zet een melding in de wachtrij. Geeft False als hij binnen de cooldown al verstuurd is."""
    now = time.time()
    with _lock:
        if key and now - _sent.get(key, 0) < cooldown:
            return False
        if key:
            _sent[key] = now
        _buffer.append({"level": level, "title": title, "message": message[:1500], "link": link(product_id)})
    return True


def flush() -> int:
    """Verstuurt alle wachtende meldingen als één mail. Retourneert het aantal verstuurde meldingen."""
    if not config.N8N_ALERT_WEBHOOK_URL:
        with _lock:
            n = len(_buffer)
            for item in _buffer:
                log.warning("MELDING (geen alert-webhook ingesteld): %s – %s", item["title"], item["message"])
            _buffer.clear()
        return 0
    with _lock:
        items = list(_buffer)
    if not items:
        return 0
    level = max((i["level"] for i in items), key=LEVELS.index)
    subject = items[0]["title"] if len(items) == 1 else f"{len(items)} meldingen"
    try:
        r = httpx.post(config.N8N_ALERT_WEBHOOK_URL, headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET},
                       json={"level": level, "subject": subject, "items": items}, timeout=60)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Melding versturen mislukt, later opnieuw: %s", e)
        return 0
    with _lock:
        del _buffer[:len(items)]
    return len(items)


def send_now(level: str, subject: str, items: list[dict], intro: str = "", html_extra: str = "") -> bool:
    """Directe mail zonder bundeling (gebruikt voor het dagoverzicht)."""
    if not config.N8N_ALERT_WEBHOOK_URL:
        return False
    try:
        httpx.post(config.N8N_ALERT_WEBHOOK_URL, headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET}, timeout=60,
                   json={"level": level, "subject": subject, "intro": intro, "items": items,
                         "html_extra": html_extra}).raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("Dagoverzicht versturen mislukt: %s", e)
        return False
