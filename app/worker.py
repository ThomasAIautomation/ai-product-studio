"""Achtergrondverwerking: wachtrij, batchverwerking, herhaalpogingen met exponentiële backoff.

Statussen:
  queued -> processing -> review            (beeldbewerking + AI-analyse klaar, wacht op controle)
                       -> retry_wait       (tijdelijke fout, wordt automatisch opnieuw geprobeerd)
                       -> failed           (definitieve fout of max. pogingen bereikt)
  review -> publish_queued -> publishing -> published | publish_retry | publish_failed
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from pathlib import Path

from . import alerts, config, db, imaging, shopify, vision

log = logging.getLogger("studio.worker")
_stop = threading.Event()
UNRELIABLE_IOU = 0.80  # onder deze overeenstemming tussen de twee segmentatiemodellen: bewerking niet gebruiken


class Step:
    """Registreert een verwerkingsstap (duur, poging, kosten) in de events-tabel."""

    def __init__(self, pid: int, name: str, attempt: int):
        self.pid, self.name, self.attempt = pid, name, attempt
        self.extra: dict = {}

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        t1 = time.time()
        db.insert("events", product_id=self.pid, step=self.name, attempt=self.attempt,
                  status="error" if exc else "ok", started_at=self.t0, ended_at=t1,
                  duration_ms=int((t1 - self.t0) * 1000),
                  message=str(exc)[:1000] if exc else self.extra.pop("message", None), **self.extra)
        return False


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, shopify.ShopifyError):
        return exc.transient
    if isinstance(exc, (vision.VisionNotConfigured, ValueError)):
        return False
    return vision.is_transient(exc) or isinstance(exc, (TimeoutError, ConnectionError))


def _fail_or_retry(p: dict, exc: Exception, attempts: int, retry_status: str, fail_status: str) -> None:
    transient = _is_transient(exc)
    if transient and attempts < config.MAX_ATTEMPTS:
        delay = config.RETRY_BASE_SECONDS * 2 ** (attempts - 1)
        db.update("products", p["id"], status=retry_status, error=f"Poging {attempts} mislukt, opnieuw over {delay}s: {exc}",
                  next_attempt_at=time.time() + delay)
    else:
        db.update("products", p["id"], status=fail_status, error=str(exc)[:2000], next_attempt_at=None)
        stage = "publicatie naar Shopify" if fail_status == "publish_failed" else "verwerking"
        alerts.notify("fout", f"Product #{p['id']}: {stage} mislukt",
                      f"Na {attempts} poging(en): {exc}", product_id=p["id"], key=f"fail-{p['id']}-{fail_status}")
    msg = str(exc).lower()
    if "credit" in msg or "quota" in msg or "insufficient" in msg:
        alerts.notify("kritiek", "AI-tegoed lijkt op of geblokkeerd",
                      f"De AI-dienst meldt een tegoed-/quotumprobleem: {exc}. Controleer je n8n Gateway credits.",
                      key="credits", cooldown=3 * 3600)


def process(p: dict) -> None:
    pid, attempt = p["id"], p["attempts"]
    images = db.query("SELECT * FROM images WHERE product_id=? ORDER BY position", (pid,))
    out_dir = config.OUTPUT_DIR / str(pid)
    out_dir.mkdir(exist_ok=True)

    for im in images:
        if im["processed_path"]:
            continue  # al gedaan in eerdere poging
        with Step(pid, f"beeld_{im['position']}", attempt) as step:
            res = imaging.process_photo(Path(im["original_path"]), out_dir, p["style"] or "studio")
            db.update("images", im["id"], processed_path=res["processed_path"], mask_path=res["mask_path"],
                      integrity_json=json.dumps(res["integrity"]))
            compl = res["integrity"].get("completeness", {})
            if compl.get("agreement_iou", 1.0) < UNRELIABLE_IOU:
                # Uitsnede duidelijk onbetrouwbaar (bijv. hand in beeld, tas half buiten beeld):
                # standaard de originele foto gebruiken; de gebruiker kan corrigeren met het penseel.
                db.update("images", im["id"], use_processed=0)
                step.extra["message"] = (f"Uitsnede onbetrouwbaar (modellen {compl['agreement_iou']:.0%} eens) — "
                                         "originele foto wordt gebruikt. Corrigeer met het penseel of maak een nieuwe foto.")

    if p["analysis_json"]:
        pass  # analyse al gelukt bij eerdere poging
    elif not config.vision_enabled():
        db.insert("events", product_id=pid, step="ai_analyse", status="skipped", attempt=attempt,
                  started_at=time.time(), ended_at=time.time(), duration_ms=0,
                  message="AI uitgeschakeld (geen API-sleutel): vul teksten handmatig in.")
    else:
        confirmed = {**json.loads(p.get("dims_json") or "{}"), **json.loads(p.get("seller_json") or "{}")}
        photos = [Path(i["original_path"]) for i in images]
        with Step(pid, "ai_analyse", attempt) as step:
            data, usage = vision.analyze(photos, confirmed)
            step.extra.update(model=usage["model"], input_tokens=usage["input_tokens"],
                              output_tokens=usage["output_tokens"], cost_usd=usage["cost_usd"])
        if config.FACTCHECK and config.VISION_PROVIDER == "n8n":
            with Step(pid, "factcheck", attempt) as step:
                fc, usage = vision.factcheck(data, photos, confirmed)
                step.extra.update(model=usage["model"], input_tokens=usage["input_tokens"],
                                  output_tokens=usage["output_tokens"], cost_usd=usage["cost_usd"],
                                  message=f"{fc['oordeel']}: {len(fc['problemen'])} aanpassing(en)")
            data["factcheck"] = {"oordeel": fc["oordeel"], "problemen": fc["problemen"],
                                 "origineel": {k: data[k] for k in ("titel", "beschrijving_html", "seo_titel", "seo_beschrijving")}}
            for k in ("titel", "beschrijving_html", "seo_titel", "seo_beschrijving"):
                if fc.get(k):
                    data[k] = fc[k]
        db.update("products", pid, analysis_json=json.dumps(data, ensure_ascii=False),
                  title=data["titel"], description_html=data["beschrijving_html"],
                  product_type=data["producttype"], category=data["categorie"],
                  tags=json.dumps(list(dict.fromkeys(data["tags"] + [f"staat: {data['staat']['beoordeling']}"])),
                                  ensure_ascii=False), seo_title=data["seo_titel"],
                  seo_description=data["seo_beschrijving"], handle=data["handle"])

    db.update("products", pid, status="review", error=None, analyzed_at=time.time(), next_attempt_at=None)


def publish(p: dict) -> None:
    pid, attempt = p["id"], p["publish_attempts"]
    full = db.product_full(pid)
    if not full["title"]:
        raise ValueError("Titel is verplicht voor publicatie.")
    for im in full["images"]:
        if im["use_processed"] and im["integrity"] and not im["integrity"]["passed"]:
            raise ValueError(f"Afbeelding {im['position']} heeft geen geldige integriteitscontrole.")
    # Productfoto's eerst, sfeerbeelden daarna (nooit als hoofdfoto).
    ordered = sorted(full["images"], key=lambda im: (im.get("kind") == "sfeer", im["position"]))
    paths = [Path(im["processed_path"] if im["use_processed"] and im["processed_path"] else im["original_path"])
             for im in ordered]
    full["_alts"] = [(f"Sfeerbeeld (AI-omgeving, tas onbewerkt) – {full['title']}" if im.get("kind") == "sfeer"
                      else full["title"]) for im in ordered]
    with Step(pid, "shopify_publicatie", attempt) as step:
        if config.PUBLISH_VIA == "n8n":
            if config.PUBLIC_BASE_URL:
                urls = [f"{config.PUBLIC_BASE_URL}/files/{p_.relative_to(config.DATA_DIR)}" for p_ in paths]
            else:
                # Backend niet publiek: beelden rechtstreeks in Shopify's staged storage zetten;
                # n8n maakt daarna het concept aan met die resourceUrls.
                urls = shopify.upload_images(paths)
            res = shopify.publish_via_n8n(full, urls)
        else:
            res = shopify.create_draft_product(full, paths)
        step.extra["message"] = f"Concept aangemaakt: {res.get('id')}"
    db.update("products", pid, status="published", error=None, published_at=time.time(),
              shopify_product_id=res.get("id"), shopify_admin_url=res.get("admin_url"), next_attempt_at=None)


def _loop() -> None:
    while not _stop.is_set():
        job = db.claim(("publish_queued", "publish_retry"), "publishing", "publish_attempts")
        if job:
            try:
                publish(job)
            except Exception as e:  # noqa: BLE001
                log.warning("publish %s: %s", job["id"], traceback.format_exc())
                _fail_or_retry(job, e, job["publish_attempts"], "publish_retry", "publish_failed")
            continue
        job = db.claim(("queued", "retry_wait"), "processing", "attempts")
        if job:
            try:
                process(job)
            except Exception as e:  # noqa: BLE001
                log.warning("process %s: %s", job["id"], traceback.format_exc())
                _fail_or_retry(job, e, job["attempts"], "retry_wait", "failed")
            continue
        _stop.wait(1.0)


_last = {"flush": 0.0, "health": 0.0, "stuck": 0.0}


def check_stuck() -> int:
    """Producten die te lang 'bezig' zijn opnieuw inplannen en melden."""
    limit = time.time() - config.STUCK_MINUTES * 60
    rows = db.query("SELECT id, status FROM products WHERE status IN ('processing','publishing') AND updated_at < ?", (limit,))
    for r in rows:
        new = "queued" if r["status"] == "processing" else "publish_queued"
        db.update("products", r["id"], status=new, next_attempt_at=None)
        alerts.notify("waarschuwing", f"Product #{r['id']} hing vast",
                      f"Stond langer dan {config.STUCK_MINUTES} min op '{r['status']}'; automatisch opnieuw ingepland.",
                      product_id=r["id"], key=f"stuck-{r['id']}")
    return len(rows)


def health_check() -> list[str]:
    """Controleert Shopify, n8n en schijfruimte. Problemen worden gemeld (max. 1× per 6 uur per soort)."""
    import shutil
    import httpx
    problems = []
    if config.SHOPIFY_CLIENT_ID:
        try:
            shopify.graphql("{ shop { name } }")
        except Exception as e:  # noqa: BLE001
            problems.append(("shopify", f"Shopify-koppeling werkt niet: {e}"))
    n8n = config.env("N8N_URL", "https://supplierresponse.app.n8n.cloud").rstrip("/")
    try:
        if httpx.get(f"{n8n}/healthz", timeout=20).status_code != 200:
            problems.append(("n8n", "n8n reageert niet goed op de gezondheidscheck."))
    except httpx.HTTPError as e:
        problems.append(("n8n", f"n8n onbereikbaar: {e}"))
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    if free_gb < 1:
        problems.append(("disk", f"Nog maar {free_gb:.2f} GB schijfruimte vrij."))
    for key, msg in problems:
        alerts.notify("kritiek", "Gezondheidscheck: probleem gevonden", msg, key=f"health-{key}")
    return [m for _, m in problems]


def daily_summary(force: bool = False) -> bool:
    """Dagoverzicht van de afgelopen 24 uur, één keer per dag rond DAILY_SUMMARY_HOUR (Europe/Amsterdam)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if config.DAILY_SUMMARY_HOUR < 0 and not force:
        return False
    now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    marker = config.DATA_DIR / "laatste_dagoverzicht.txt"
    today = now.date().isoformat()
    if not force and (now.hour < config.DAILY_SUMMARY_HOUR or (marker.exists() and marker.read_text() == today)):
        return False
    since = time.time() - 86400
    q = lambda sql: db.one(sql, (since,))["n"]
    stats = {
        "Nieuw geüpload": q("SELECT COUNT(*) n FROM products WHERE created_at >= ?"),
        "Als concept in Shopify gezet": q("SELECT COUNT(*) n FROM products WHERE published_at >= ?"),
        "Mislukt": q("SELECT COUNT(*) n FROM products WHERE status IN ('failed','publish_failed') AND updated_at >= ?"),
        "AI-tokens gebruikt": q("SELECT COALESCE(SUM(input_tokens)+SUM(output_tokens),0) n FROM events WHERE started_at >= ?"),
    }
    waiting = db.one("SELECT COUNT(*) n FROM products WHERE status='review'")["n"]
    failed = db.query("SELECT id, error FROM products WHERE status IN ('failed','publish_failed') ORDER BY id DESC LIMIT 10")
    rows = "".join(f"<tr><td style='padding:4px 12px 4px 0'>{k}</td><td><b>{v}</b></td></tr>" for k, v in stats.items())
    items = [{"title": f"Product #{f['id']} mislukt", "message": (f["error"] or "")[:300], "link": alerts.link(f["id"])}
             for f in failed]
    ok = alerts.send_now("fout" if failed else "info", f"Dagoverzicht {now.strftime('%d-%m-%Y')}",
                         items, intro=f"{waiting} product(en) wachten op jouw controle.",
                         html_extra=f"<table style='margin-top:12px'>{rows}</table>")
    if ok:
        marker.write_text(today)
    return ok


def _monitor() -> None:
    while not _stop.is_set():
        now = time.time()
        try:
            if now - _last["stuck"] > 300:
                _last["stuck"] = now
                check_stuck()
            if now - _last["health"] > 3600:
                _last["health"] = now
                health_check()
                daily_summary()
            if now - _last["flush"] > 60:
                _last["flush"] = now
                alerts.flush()
        except Exception:  # noqa: BLE001
            log.warning("monitor: %s", traceback.format_exc())
        _stop.wait(10)


def recover_interrupted() -> None:
    """Na een herstart: producten die midden in verwerking zaten opnieuw inplannen."""
    with db.tx() as c:
        c.execute("UPDATE products SET status='queued' WHERE status='processing'")
        c.execute("UPDATE products SET status='publish_queued' WHERE status='publishing'")


def start() -> list[threading.Thread]:
    _stop.clear()
    recover_interrupted()
    threads = [threading.Thread(target=_loop, name=f"worker-{i}", daemon=True) for i in range(config.WORKERS)]
    if config.MONITOR:
        threads.append(threading.Thread(target=_monitor, name="monitor", daemon=True))
    for t in threads:
        t.start()
    return threads


def stop() -> None:
    _stop.set()
