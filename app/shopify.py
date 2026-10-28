"""Shopify Admin GraphQL: token via client credentials, staged uploads, productCreate als DRAFT."""
from __future__ import annotations

import mimetypes
import threading
import time
from pathlib import Path

import httpx

from . import config


class ShopifyError(RuntimeError):
    def __init__(self, msg: str, transient: bool = False):
        super().__init__(msg)
        self.transient = transient


_token: dict = {}
_token_lock = threading.Lock()


def _token_value() -> str:
    """Kortlevend Admin API-token (client credentials grant, Dev Dashboard-app op eigen store)."""
    if not (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET):
        raise ShopifyError("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET ontbreken.")
    with _token_lock:
        if _token.get("value") and _token["expires"] > time.time() + 300:
            return _token["value"]
        r = httpx.post(
            f"https://{config.SHOPIFY_SHOP}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": config.SHOPIFY_CLIENT_ID,
                  "client_secret": config.SHOPIFY_CLIENT_SECRET},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ShopifyError(f"Token aanvragen mislukt ({r.status_code}).", transient=r.status_code >= 500)
        body = r.json()
        _token.update(value=body["access_token"], expires=time.time() + int(body.get("expires_in", 86399)))
        return _token["value"]


def graphql(query: str, variables: dict | None = None) -> dict:
    url = f"https://{config.SHOPIFY_SHOP}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    try:
        r = httpx.post(url, json={"query": query, "variables": variables or {}},
                       headers={"X-Shopify-Access-Token": _token_value()}, timeout=60)
    except httpx.TransportError as e:
        raise ShopifyError(f"Netwerkfout naar Shopify: {e}", transient=True) from e
    if r.status_code == 401:
        _token.clear()
        raise ShopifyError("Shopify 401 — token verlopen of ingetrokken.", transient=True)
    if r.status_code == 429 or r.status_code >= 500:
        raise ShopifyError(f"Shopify {r.status_code}", transient=True)
    body = r.json()
    if body.get("errors"):
        throttled = any(e.get("extensions", {}).get("code") == "THROTTLED" for e in body["errors"])
        raise ShopifyError(f"GraphQL-fout: {body['errors']}", transient=throttled)
    return body["data"]


STAGED = """
mutation staged($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

PRODUCT_CREATE = """
mutation create($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product { id handle status variants(first: 1) { nodes { id } } }
    userErrors { field message }
  }
}"""

VARIANT_PRICE = """
mutation price($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors { field message }
  }
}"""


def upload_images(paths: list[Path]) -> list[str]:
    inputs = []
    for p in paths:
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        inputs.append({"resource": "IMAGE", "filename": p.name, "mimeType": mime,
                       "httpMethod": "POST", "fileSize": str(p.stat().st_size)})
    data = graphql(STAGED, {"input": inputs})["stagedUploadsCreate"]
    if data["userErrors"]:
        raise ShopifyError(f"stagedUploadsCreate: {data['userErrors']}")
    urls = []
    for p, target, inp in zip(paths, data["stagedTargets"], inputs):
        form = {prm["name"]: prm["value"] for prm in target["parameters"]}
        with p.open("rb") as fh:
            r = httpx.post(target["url"], data=form, files={"file": (p.name, fh, inp["mimeType"])}, timeout=120)
        if r.status_code >= 300:
            raise ShopifyError(f"Upload {p.name} mislukt ({r.status_code})", transient=r.status_code >= 500)
        urls.append(target["resourceUrl"])
    return urls


def product_input(p: dict) -> dict:
    return {
        "title": p["title"],
        "descriptionHtml": p.get("description_html") or "",
        "productType": p.get("product_type") or "",
        "vendor": p.get("vendor") or "AI Bag Studio (demo)",
        "tags": list(p.get("tags") or []) + ["ai-product-studio", "testproduct"],
        "status": "DRAFT",  # altijd concept
        "handle": p.get("handle") or None,
        "seo": {"title": p.get("seo_title") or p["title"], "description": p.get("seo_description") or ""},
    }


def create_draft_product(p: dict, image_paths: list[Path]) -> dict:
    sources = upload_images(image_paths) if image_paths else []
    alts = p.get("_alts") or [p["title"]] * len(sources)
    media = [{"originalSource": s, "mediaContentType": "IMAGE", "alt": a} for s, a in zip(sources, alts)]
    data = graphql(PRODUCT_CREATE, {"product": product_input(p), "media": media})["productCreate"]
    if data["userErrors"]:
        raise ShopifyError(f"productCreate: {data['userErrors']}")
    prod = data["product"]
    if p.get("price") and prod["variants"]["nodes"]:
        res = graphql(VARIANT_PRICE, {"productId": prod["id"], "variants": [
            {"id": prod["variants"]["nodes"][0]["id"], "price": str(p["price"])}]})
        if res["productVariantsBulkUpdate"]["userErrors"]:
            raise ShopifyError(f"Prijs zetten: {res['productVariantsBulkUpdate']['userErrors']}")
    numeric = prod["id"].rsplit("/", 1)[-1]
    store = config.SHOPIFY_SHOP.split(".")[0]
    return {"id": prod["id"], "status": prod["status"],
            "admin_url": f"https://admin.shopify.com/store/{store}/products/{numeric}"}


def publish_via_n8n(p: dict, image_urls: list[str]) -> dict:
    """Stuurt het goedgekeurde product naar de n8n-workflow 'Publiceer naar Shopify (concept)'."""
    if not config.N8N_PUBLISH_WEBHOOK_URL:
        raise ShopifyError("N8N_PUBLISH_WEBHOOK_URL ontbreekt.")
    payload = {"product": product_input(p), "price": p.get("price") or None,
               "images": [{"url": u, "alt": a} for u, a in zip(image_urls, p.get("_alts") or [p["title"]] * len(image_urls))]}
    try:
        r = httpx.post(config.N8N_PUBLISH_WEBHOOK_URL, json=payload, timeout=120,
                       headers={"X-Studio-Secret": config.N8N_WEBHOOK_SECRET})
    except httpx.TransportError as e:
        raise ShopifyError(f"n8n onbereikbaar: {e}", transient=True) from e
    if r.status_code >= 300:
        raise ShopifyError(f"n8n antwoordde {r.status_code}: {r.text[:300]}", transient=r.status_code >= 500)
    return r.json()
