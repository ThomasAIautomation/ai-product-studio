"""FastAPI-backend + dashboard voor de AI Product Studio."""
from __future__ import annotations

import base64
import io
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, worker

ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD = 40 * 1024 * 1024
STYLES = {"studio", "wit", "scene"}


@asynccontextmanager
async def lifespan(_app):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="AI Product Studio", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    # Optioneel wachtwoord voor het dashboard. /files blijft open (Shopify/n8n halen daar beelden op;
    # bestandsnamen bevatten een willekeurige UUID).
    if config.DASHBOARD_PASSWORD and not request.url.path.startswith("/files/"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, pw = base64.b64decode(header[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, config.DASHBOARD_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return JSONResponse({"detail": "auth"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="AI Product Studio"'})
    return await call_next(request)


def _rel(path: str | None) -> str | None:
    return f"/files/{Path(path).relative_to(config.DATA_DIR)}" if path else None


def _public(p: dict) -> dict:
    for im in p.get("images", []):
        for k in ("original_path", "processed_path", "mask_path"):
            im[k.replace("_path", "_url")] = _rel(im.pop(k))
    return p


async def _save_upload(f: UploadFile, folder: Path) -> Path:
    ext = Path(f.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Bestandstype {ext or '?'} niet toegestaan.")
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, f"{f.filename} is groter dan 40 MB.")
    path = folder / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    return path


@app.get("/api/config")
def get_config():
    return config.status() | {"styles": sorted(STYLES), "scenes": [p.name for p in config.SCENE_DIR.iterdir()]}


@app.post("/api/batches")
async def create_batch(files: list[UploadFile] = File(...), grouping: str = Form("per_foto"),
                       style: str = Form("studio"), name: str = Form("")):
    """grouping: 'per_foto' = elke foto een eigen product; 'een_product' = alle foto's samen één product."""
    if style not in STYLES:
        raise HTTPException(400, "Onbekende stijl.")
    if not files:
        raise HTTPException(400, "Geen bestanden.")
    batch_id = db.insert("batches", name=name or time.strftime("Batch %d-%m %H:%M"), style=style, created_at=time.time())
    groups = [files] if grouping == "een_product" else [[f] for f in files]
    created = []
    for group in groups:
        pid = db.insert("products", batch_id=batch_id, status="uploading", style=style, created_at=time.time(),
                        vendor="AI Bag Studio (demo)")
        folder = config.UPLOAD_DIR / str(pid)
        folder.mkdir(exist_ok=True)
        for pos, f in enumerate(group, 1):
            path = await _save_upload(f, folder)
            db.insert("images", product_id=pid, position=pos, original_path=str(path))
        db.update("products", pid, status="queued")
        created.append(pid)
    return {"batch_id": batch_id, "product_ids": created}


@app.get("/api/products")
def list_products(status: str | None = None):
    sql = ("SELECT p.id, p.batch_id, p.status, p.title, p.error, p.attempts, p.style, p.created_at, p.updated_at, "
           "p.shopify_admin_url, (SELECT processed_path FROM images i WHERE i.product_id=p.id ORDER BY position LIMIT 1) thumb, "
           "(SELECT original_path FROM images i WHERE i.product_id=p.id ORDER BY position LIMIT 1) orig, "
           "(SELECT COUNT(*) FROM images i WHERE i.product_id=p.id) n_images FROM products p")
    rows = db.query(sql + (" WHERE p.status=?" if status else "") + " ORDER BY p.id DESC", (status,) if status else ())
    for r in rows:
        r["thumb_url"] = _rel(r.pop("thumb")) or _rel(r.pop("orig", None))
        r.pop("orig", None)
    return rows


@app.get("/api/products/{pid}")
def get_product(pid: int):
    p = db.product_full(pid)
    if not p:
        raise HTTPException(404)
    return _public(p)


class ProductEdit(BaseModel):
    title: str | None = None
    description_html: str | None = None
    product_type: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    handle: str | None = None
    vendor: str | None = None
    price: str | None = None
    image_choices: dict[int, bool] | None = None  # image_id -> bewerkte versie gebruiken?


@app.patch("/api/products/{pid}")
def edit_product(pid: int, body: ProductEdit):
    p = db.one("SELECT status FROM products WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    if p["status"] in ("publishing", "published"):
        raise HTTPException(409, "Product is al (bijna) gepubliceerd.")
    values = body.model_dump(exclude_none=True, exclude={"image_choices"})
    if "tags" in values:
        values["tags"] = json.dumps(values["tags"], ensure_ascii=False)
    if values:
        db.update("products", pid, **values)
    for image_id, use in (body.image_choices or {}).items():
        with db.tx() as c:
            c.execute("UPDATE images SET use_processed=? WHERE id=? AND product_id=?", (int(use), image_id, pid))
    return get_product(pid)


@app.post("/api/products/{pid}/approve")
def approve(pid: int, confirm_warnings: bool = False):
    p = db.one("SELECT status, title FROM products WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    flagged = db.query("SELECT position FROM images WHERE product_id=? AND use_processed=1 "
                       "AND json_extract(integrity_json,'$.completeness.review_required')=1", (pid,))
    if flagged and not confirm_warnings:
        raise HTTPException(409, {"code": "uitsnede_waarschuwing", "positions": [f["position"] for f in flagged],
                                  "message": "Uitsnede van foto " + ", ".join(str(f["position"]) for f in flagged)
                                  + " heeft een waarschuwing. Corrigeer de uitsnede, kies de originele foto, of bevestig."})
    if p["status"] not in ("review", "publish_failed"):
        raise HTTPException(409, f"Status '{p['status']}' kan niet gepubliceerd worden.")
    if not p["title"]:
        raise HTTPException(400, "Vul eerst een titel in.")
    db.update("products", pid, status="publish_queued", error=None, next_attempt_at=None, publish_attempts=0)
    return {"ok": True}


@app.post("/api/products/{pid}/retry")
def retry(pid: int, reanalyze: bool = False):
    p = db.one("SELECT status FROM products WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    if p["status"] in ("processing", "publishing", "published"):
        raise HTTPException(409, "Niet mogelijk in deze status.")
    extra = {"analysis_json": None} if reanalyze else {}
    db.update("products", pid, status="queued", error=None, attempts=0, next_attempt_at=None, **extra)
    return {"ok": True}


@app.post("/api/retry-failed")
def retry_failed():
    with db.tx() as c:
        n1 = c.execute("UPDATE products SET status='queued', attempts=0, error=NULL WHERE status='failed'").rowcount
        n2 = c.execute("UPDATE products SET status='publish_queued', publish_attempts=0, error=NULL "
                       "WHERE status='publish_failed'").rowcount
    return {"requeued_processing": n1, "requeued_publishing": n2}


@app.delete("/api/products/{pid}")
def delete_product(pid: int):
    with db.tx() as c:
        c.execute("DELETE FROM events WHERE product_id=?", (pid,))
        c.execute("DELETE FROM images WHERE product_id=?", (pid,))
        c.execute("DELETE FROM products WHERE id=? AND status NOT IN ('processing','publishing')", (pid,))
    return {"ok": True}


@app.post("/api/images/{image_id}/mask")
async def correct_mask(image_id: int, file: UploadFile = File(...)):
    """Handmatig gecorrigeerde uitsnede: de foto wordt opnieuw samengesteld met déze maskers."""
    from PIL import Image as PILImage
    from . import imaging
    im = db.one("SELECT i.*, p.style, p.status FROM images i JOIN products p ON p.id=i.product_id WHERE i.id=?", (image_id,))
    if not im:
        raise HTTPException(404)
    if im["status"] in ("processing", "publishing", "published"):
        raise HTTPException(409, "Niet mogelijk in deze status.")
    mask = PILImage.open(io.BytesIO(await file.read())).convert("L").point(lambda a: 255 if a >= 128 else 0)
    out_dir = config.OUTPUT_DIR / str(im["product_id"])
    try:
        res = imaging.process_photo(Path(im["original_path"]), out_dir, im["style"] or "studio", manual_mask=mask)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.update("images", image_id, processed_path=res["processed_path"], mask_path=res["mask_path"],
              integrity_json=json.dumps(res["integrity"]), use_processed=1)
    db.insert("events", product_id=im["product_id"], step=f"uitsnede_handmatig_{im['position']}", status="ok",
              attempt=1, started_at=time.time(), ended_at=time.time(), duration_ms=0,
              message="Uitsnede handmatig gecorrigeerd")
    db.update("products", im["product_id"])
    return {"ok": True, "integrity": res["integrity"]}


class SceneRequest(BaseModel):
    preset: str
    image_id: int | None = None
    breedte: float | None = None
    hoogte: float | None = None
    diepte: float | None = None


@app.post("/api/products/{pid}/sfeerbeeld")
def make_lifestyle(pid: int, body: SceneRequest):
    """Genereert een lege AI-scène en plaatst de echte tas erin op ware grootte (tas-pixels exact)."""
    from . import scenes
    if body.preset not in scenes.PRESETS:
        raise HTTPException(400, "Onbekende scène.")
    p = db.one("SELECT status, dims_json FROM products WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    if p["status"] in ("processing", "publishing", "published"):
        raise HTTPException(409, "Niet mogelijk in deze status.")
    base = db.one("SELECT * FROM images WHERE product_id=? AND kind='product' AND mask_path IS NOT NULL "
                  + ("AND id=? " if body.image_id else "") + "ORDER BY position LIMIT 1",
                  (pid, body.image_id) if body.image_id else (pid,))
    if not base:
        raise HTTPException(400, "Geen bewerkte productfoto beschikbaar.")
    compl = (json.loads(base["integrity_json"] or "{}")).get("completeness", {})
    if compl.get("review_required") or (base["use_processed"] == 0 and not compl.get("manual")):
        raise HTTPException(409, "De uitsnede van deze foto is onzeker. Corrigeer eerst de uitsnede met het penseel.")
    dims = {k: v for k, v in body.model_dump(include={"breedte", "hoogte", "diepte"}).items() if v}
    if dims:
        db.update("products", pid, dims_json=json.dumps(dims))
    else:
        dims = json.loads(p["dims_json"] or "null")
    t0 = time.time()
    try:
        res = scenes.make_scene_image(Path(base["original_path"]), Path(base["mask_path"]),
                                      config.OUTPUT_DIR / str(pid), body.preset, dims)
    except Exception as e:  # noqa: BLE001
        db.insert("events", product_id=pid, step=f"sfeer_{body.preset}", status="error", attempt=1,
                  started_at=t0, ended_at=time.time(), duration_ms=int((time.time() - t0) * 1000), message=str(e)[:500])
        raise HTTPException(502, f"Sfeerbeeld mislukt: {e}")
    pos = (db.one("SELECT MAX(position) m FROM images WHERE product_id=?", (pid,))["m"] or 0) + 1
    db.insert("images", product_id=pid, position=max(pos, 100), original_path=base["original_path"],
              processed_path=res["path"], use_processed=1, kind="sfeer", integrity_json=json.dumps(res["integrity"]))
    db.insert("events", product_id=pid, step=f"sfeer_{body.preset}", status="ok", attempt=1, started_at=t0,
              ended_at=time.time(), duration_ms=int((time.time() - t0) * 1000), model="gpt-image-1 (n8n Gateway)",
              message=("Afmetingen aangenomen (38×28×12 cm)" if res["integrity"]["dims_assumed"] else "Op ware grootte"))
    db.update("products", pid)
    return {"ok": True, "integrity": res["integrity"]}


@app.delete("/api/images/{image_id}")
def delete_image(image_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM images WHERE id=? AND kind='sfeer'", (image_id,))
    return {"ok": True}


@app.post("/api/scenes")
async def upload_scene(file: UploadFile = File(...), anchor_x: float = Form(0.5), anchor_y: float = Form(0.3)):
    """Sfeer-/modelfoto als achtergrond voor stijl 'scene'. Anker = waar het hengsel komt (bijv. de hand)."""
    for old in config.SCENE_DIR.iterdir():
        old.unlink()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Alleen JPG/PNG/WebP.")
    (config.SCENE_DIR / f"scene__x{anchor_x:.3f}_y{anchor_y:.3f}{ext}").write_bytes(await file.read())
    return {"ok": True}


@app.get("/api/stats")
def stats():
    by_status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM products GROUP BY status")}
    cost = db.one("SELECT COALESCE(SUM(cost_usd),0) total, COALESCE(SUM(input_tokens),0) tin, "
                  "COALESCE(SUM(output_tokens),0) tout, COUNT(model) calls FROM events")
    lead = db.one("SELECT AVG(analyzed_at - created_at) avg_s, MAX(analyzed_at - created_at) max_s, COUNT(*) n "
                  "FROM products WHERE analyzed_at IS NOT NULL")
    to_shop = db.one("SELECT AVG(published_at - created_at) avg_s FROM products WHERE published_at IS NOT NULL")
    steps = db.query("SELECT CASE WHEN step LIKE 'beeld_%' THEN 'beeldbewerking' ELSE step END step, "
                     "COUNT(*) n, ROUND(AVG(duration_ms)) avg_ms, SUM(status='error') errors, "
                     "COALESCE(SUM(cost_usd),0) cost FROM events GROUP BY 1 ORDER BY 1")
    errors = db.query("SELECT e.product_id, e.step, e.attempt, e.message, e.ended_at FROM events e "
                      "WHERE e.status='error' ORDER BY e.id DESC LIMIT 25")
    integrity = db.one("SELECT COUNT(*) n, SUM(json_extract(integrity_json,'$.passed')) passed FROM images "
                       "WHERE integrity_json IS NOT NULL")
    analyzed = max(1, lead["n"] or 0)
    return {
        "by_status": by_status, "total": sum(by_status.values()),
        "cost_usd": round(cost["total"], 4), "cost_per_product_usd": round(cost["total"] / analyzed, 4),
        "tokens": {"input": cost["tin"], "output": cost["tout"], "calls": cost["calls"]},
        "lead_time_s": {"avg_to_review": lead["avg_s"], "max_to_review": lead["max_s"], "avg_to_shopify": to_shop["avg_s"]},
        "steps": steps, "recent_errors": errors,
        "integrity": {"images": integrity["n"], "passed": integrity["passed"] or 0},
    }


@app.get("/files/{path:path}")
def files(path: str):
    full = (config.DATA_DIR / path).resolve()
    allowed = (config.UPLOAD_DIR.resolve(), config.OUTPUT_DIR.resolve(), config.SCENE_DIR.resolve())
    if not any(a in full.parents for a in allowed) or not full.is_file():
        raise HTTPException(404)
    return FileResponse(full)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
