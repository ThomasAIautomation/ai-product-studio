import io
import time

from fastapi.testclient import TestClient

from app import main, shopify
from make_test_photo import make


def _wait(client, pid, statuses, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        p = client.get(f"/api/products/{pid}").json()
        if p["status"] in statuses:
            return p
        time.sleep(0.5)
    raise AssertionError(f"timeout, status={p['status']} error={p['error']}")


def _photo(seed):
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "t.jpg")
    make(path, seed=seed)
    return open(path, "rb").read()


def test_batch_review_edit_and_publish_flow(monkeypatch):
    with TestClient(main.app) as client:
        files = [("files", (f"tas{i}.jpg", io.BytesIO(_photo(i)), "image/jpeg")) for i in (1, 2)]
        r = client.post("/api/batches", files=files, data={"grouping": "per_foto", "style": "wit"})
        assert r.status_code == 200
        pids = r.json()["product_ids"]
        assert len(pids) == 2
        for pid in pids:
            p = _wait(client, pid, {"review", "failed"})
            assert p["status"] == "review", p["error"]
            assert p["images"][0]["integrity"]["passed"]
            assert any(e["step"] == "ai_analyse" and e["status"] == "skipped" for e in p["events"])

        pid = pids[0]
        r = client.patch(f"/api/products/{pid}", json={"title": "Testtas bruin (fictief)", "tags": ["test"], "price": "49.95"})
        assert r.json()["title"] == "Testtas bruin (fictief)"

        # Zonder Shopify-credentials: definitieve (niet-tijdelijke) fout, duidelijke melding.
        assert client.post(f"/api/products/{pid}/approve?confirm_warnings=true").status_code == 200
        p = _wait(client, pid, {"publish_failed", "published"})
        assert p["status"] == "publish_failed" and "SHOPIFY_CLIENT_ID" in p["error"]

        # Tijdelijke fout -> automatische herhaalpoging -> succes (Shopify-aanroep gesimuleerd).
        calls = {"n": 0}

        def flaky(prod, paths):
            calls["n"] += 1
            if calls["n"] == 1:
                raise shopify.ShopifyError("Shopify 503", transient=True)
            assert prod["title"] == "Testtas bruin (fictief)" and all(p.exists() for p in paths)
            assert shopify.product_input(prod)["status"] == "DRAFT"
            return {"id": "gid://shopify/Product/1", "admin_url": "https://admin.shopify.com/store/x/products/1"}

        monkeypatch.setattr(shopify, "create_draft_product", flaky)
        assert client.post(f"/api/products/{pid}/approve?confirm_warnings=true").status_code == 200
        p = _wait(client, pid, {"published", "publish_failed"})
        assert p["status"] == "published" and calls["n"] == 2
        assert [e["status"] for e in p["events"] if e["step"] == "shopify_publicatie"][-2:] == ["error", "ok"]

        s = client.get("/api/stats").json()
        assert s["by_status"]["published"] == 1 and s["integrity"]["passed"] >= 2


def test_manual_mask_correction_recomposes_with_integrity():
    import numpy as np
    from PIL import Image
    with TestClient(main.app) as client:
        r = client.post("/api/batches", files=[("files", ("t.jpg", io.BytesIO(_photo(5)), "image/jpeg"))],
                        data={"grouping": "per_foto", "style": "studio"})
        pid = r.json()["product_ids"][0]
        p = _wait(client, pid, {"review", "failed"})
        img = p["images"][0]
        mask = np.zeros((1200, 1600), np.uint8)
        mask[300:1000, 440:1160] = 255  # handmatig: ruim kader inclusief hengsel
        buf = io.BytesIO(); Image.fromarray(mask).save(buf, "PNG")
        r = client.post(f"/api/images/{img['id']}/mask", files={"file": ("m.png", buf.getvalue(), "image/png")})
        assert r.status_code == 200, r.text
        integ = r.json()["integrity"]
        assert integ["passed"] and integ["completeness"]["manual"] and integ["product_pixels"] == 700 * 720
        assert "quality_issues" in integ


def test_approve_blocked_when_cutout_flagged():
    from app import db
    with TestClient(main.app) as client:
        r = client.post("/api/batches", files=[("files", ("t.jpg", io.BytesIO(_photo(7)), "image/jpeg"))],
                        data={"grouping": "per_foto", "style": "wit"})
        pid = r.json()["product_ids"][0]
        p = _wait(client, pid, {"review", "failed"})
        img = p["images"][0]
        integ = img["integrity"]; integ["completeness"]["review_required"] = True
        db.update("images", img["id"], integrity_json=__import__("json").dumps(integ))
        client.patch(f"/api/products/{pid}", json={"title": "x"})
        r = client.post(f"/api/products/{pid}/approve")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "uitsnede_waarschuwing"
        assert client.post(f"/api/products/{pid}/approve?confirm_warnings=true").status_code == 200
