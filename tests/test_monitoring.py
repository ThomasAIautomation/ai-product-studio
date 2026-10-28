import io
import time

import httpx
from fastapi.testclient import TestClient

from app import alerts, config, lint, main, vision, worker
from make_test_photo import make


def _photo(seed):
    import os, tempfile
    path = os.path.join(tempfile.mkdtemp(), "t.jpg")
    make(path, seed=seed)
    return open(path, "rb").read()


def _wait(client, pid, statuses, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        p = client.get(f"/api/products/{pid}").json()
        if p["status"] in statuses:
            return p
        time.sleep(0.5)
    raise AssertionError(p["status"])


def test_lint_flags_brand_missing_defects_and_claims():
    p = {"title": "Tas met dubbel-G-patroon", "description_html": "<p>Mooie tas van echt leer.</p>",
         "analysis": {"staat": {"zichtbare_gebreken": [{"omschrijving": "vlek"}]}}}
    msgs = " ".join(i["message"] for i in lint.check(p))
    assert "dubbel-g" in msgs and "noemt geen gebreken" in msgs and "echt leer" in msgs
    p["seller"] = {"merk": "Gucci", "materiaal": "canvas"}
    p["description_html"] = "<p>Staat: lichte vlek.</p>"
    assert not [i for i in lint.check(p) if i["level"] == "fout"]


def test_alerts_are_bundled_deduplicated_and_retried(monkeypatch):
    sent = []

    class R:
        def raise_for_status(self):
            if len(sent) == 1:
                raise httpx.HTTPError("tijdelijk")

    monkeypatch.setattr(config, "N8N_ALERT_WEBHOOK_URL", "http://n8n.test/alert")
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **kw: sent.append(kw["json"]) or R())
    alerts._buffer.clear(); alerts._sent.clear()
    assert alerts.notify("fout", "A", "x", key="k1")
    assert not alerts.notify("fout", "A", "x", key="k1")          # dubbel binnen cooldown
    assert alerts.notify("kritiek", "B", "y")
    assert alerts.flush() == 0 and len(alerts._buffer) == 2      # eerste poging mislukt -> blijft staan
    assert alerts.flush() == 2 and not alerts._buffer            # tweede poging lukt, gebundeld
    assert sent[-1]["level"] == "kritiek" and sent[-1]["subject"] == "2 meldingen"


def test_final_failure_triggers_alert_and_stuck_is_requeued(monkeypatch):
    from app import db
    got = []
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: got.append(a) or True)
    pid = db.insert("products", status="processing", created_at=time.time())
    worker._fail_or_retry({"id": pid}, ValueError("kapot"), 3, "retry_wait", "failed")
    assert db.one("SELECT status FROM products WHERE id=?", (pid,))["status"] == "failed"
    assert got and "mislukt" in got[0][1]
    pid2 = db.insert("products", status="processing", created_at=time.time())
    db.update("products", pid2)
    with db.tx() as c:
        c.execute("UPDATE products SET updated_at=? WHERE id=?", (time.time() - 3600, pid2))
    assert worker.check_stuck() >= 1
    assert db.one("SELECT status FROM products WHERE id=?", (pid2,))["status"] == "queued"


def test_seller_info_reaches_ai_and_factcheck_is_applied(monkeypatch):
    seen = {}

    def fake_analyze(photos, confirmed=None):
        seen["confirmed"] = confirmed
        return ({"waargenomen_kenmerken": [], "staat": {"beoordeling": "goed", "zichtbare_gebreken": [
            {"omschrijving": "vlek", "locatie": "voor", "foto": 1}]}, "niet_vast_te_stellen": [], "titel": "Tas echt leer",
            "korte_omschrijving": "", "beschrijving_html": "<p>Tas.</p>", "producttype": "Handtas", "categorie": "Tassen",
            "tags": ["tas"], "seo_titel": "Tas", "seo_beschrijving": "Tas", "handle": "tas", "waarschuwingen": []},
            {"model": "fake", "input_tokens": 10, "output_tokens": 5, "cost_usd": None})

    def fake_factcheck(data, photos, confirmed=None):
        return ({"oordeel": "aangepast", "problemen": [{"tekst": "echt leer", "reden": "niet bevestigd"}],
                 "titel": "Bruine handtas", "beschrijving_html": "<p>Tas. Staat: vlek voorop.</p>",
                 "seo_titel": "Bruine handtas", "seo_beschrijving": "Bruine handtas met vlek."},
                {"model": "fake", "input_tokens": 8, "output_tokens": 4, "cost_usd": None})

    monkeypatch.setattr(config, "VISION_PROVIDER", "n8n")
    monkeypatch.setattr(config, "N8N_ANALYZE_WEBHOOK_URL", "http://n8n.test/analyze")
    monkeypatch.setattr(config, "FACTCHECK", True)
    monkeypatch.setattr(vision, "analyze", fake_analyze)
    monkeypatch.setattr(vision, "factcheck", fake_factcheck)
    with TestClient(main.app) as client:
        r = client.post("/api/batches", files=[("files", ("t.jpg", io.BytesIO(_photo(9)), "image/jpeg"))],
                        data={"grouping": "per_foto", "style": "wit"})
        pid = r.json()["product_ids"][0]
        _wait(client, pid, {"review", "failed"})
        client.patch(f"/api/products/{pid}", json={"seller": {"merk": "Testmerk", "materiaal": "canvas"},
                                                    "dims": {"breedte": 38, "hoogte": 28}})
        client.post(f"/api/products/{pid}/retry?reanalyze=true")
        p = _wait(client, pid, {"review", "failed"})
        assert seen["confirmed"]["merk"] == "Testmerk" and seen["confirmed"]["breedte"] == 38
        assert p["title"] == "Bruine handtas" and p["analysis"]["factcheck"]["problemen"][0]["tekst"] == "echt leer"
        assert "staat: goed" in p["tags"]
        assert any(e["step"] == "factcheck" for e in p["events"])


def test_approve_blocked_by_text_check_until_confirmed():
    with TestClient(main.app) as client:
        r = client.post("/api/batches", files=[("files", ("t.jpg", io.BytesIO(_photo(10)), "image/jpeg"))],
                        data={"grouping": "per_foto", "style": "wit"})
        pid = r.json()["product_ids"][0]
        p = _wait(client, pid, {"review", "failed"})
        for im in p["images"]:
            client.patch(f"/api/products/{pid}", json={"image_choices": {str(im["id"]): False}})
        client.patch(f"/api/products/{pid}", json={"title": "Tas met LV-monogram en dubbel-G"})
        r = client.post(f"/api/products/{pid}/approve")
        assert r.status_code == 409 and any("Merk" in w for w in r.json()["detail"]["warnings"])
        assert client.post(f"/api/products/{pid}/approve?confirm_warnings=true").status_code == 200
