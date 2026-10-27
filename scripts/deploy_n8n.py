"""Zet de credentials en workflows automatisch in n8n via de n8n Public API.

Gebruik (waarden komen uit .env of de omgeving, worden nooit geprint):
  N8N_URL=https://supplierresponse.app.n8n.cloud  N8N_API_KEY=...  python scripts/deploy_n8n.py

Maakt aan:
  - credential "AI Studio – Shopify client credentials"  (httpCustomAuth; client_id/secret in de body)
  - credential "AI Studio – webhook secret"              (httpHeaderAuth; X-Studio-Secret)
  - credential "AI Studio – dashboard login"             (httpBasicAuth; alleen als DASHBOARD_PASSWORD gezet is)
  - workflow "Publiceer concept naar Shopify" (geactiveerd)
  - workflow "Mislukte jobs opnieuw inplannen" (alleen als PUBLIC_BASE_URL gezet is)
Schrijft daarna N8N_PUBLISH_WEBHOOK_URL en N8N_WEBHOOK_SECRET naar .env.
"""
import json
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

N8N_URL = config.env("N8N_URL", "https://supplierresponse.app.n8n.cloud").rstrip("/")
API_KEY = config.env("N8N_API_KEY")
WF_DIR = config.ROOT / "n8n"


def api(method: str, path: str, **kw):
    r = httpx.request(method, f"{N8N_URL}/api/v1{path}", headers={"X-N8N-API-KEY": API_KEY}, timeout=30, **kw)
    if r.status_code >= 300:
        sys.exit(f"n8n {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def credential(name: str, ctype: str, data: dict) -> str:
    return api("POST", "/credentials", json={"name": name, "type": ctype, "data": data})["id"]


def workflow(file: str, subs: dict) -> dict:
    text = (WF_DIR / file).read_text()
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", v)
    wf = json.loads(text)
    created = api("POST", "/workflows", json=wf)
    api("POST", f"/workflows/{created['id']}/activate")
    return created


def set_env(updates: dict) -> None:
    env = config.ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    keys = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            keys.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in keys]
    env.write_text("\n".join(lines) + "\n")


def main() -> None:
    if not API_KEY:
        sys.exit("N8N_API_KEY ontbreekt (n8n → Settings → n8n API → Create API key).")
    if not (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET):
        sys.exit("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET ontbreken in .env.")
    webhook_secret = config.N8N_WEBHOOK_SECRET or secrets.token_urlsafe(32)
    subs = {
        "SHOP": config.SHOPIFY_SHOP, "STORE": config.SHOPIFY_SHOP.split(".")[0],
        "API_VERSION": config.SHOPIFY_API_VERSION, "PUBLIC_BASE_URL": config.PUBLIC_BASE_URL,
        "CRED_SHOPIFY": credential("AI Studio – Shopify client credentials", "httpCustomAuth", {"json": json.dumps(
            {"body": {"grant_type": "client_credentials", "client_id": config.SHOPIFY_CLIENT_ID,
                      "client_secret": config.SHOPIFY_CLIENT_SECRET}})}),
        "CRED_WEBHOOK": credential("AI Studio – webhook secret", "httpHeaderAuth",
                                   {"name": "X-Studio-Secret", "value": webhook_secret}),
    }
    pub = workflow("publish_to_shopify.json", subs)
    print(f"Workflow aangemaakt en actief: {pub['name']} ({pub['id']})")
    if config.PUBLIC_BASE_URL and config.DASHBOARD_PASSWORD:
        subs["CRED_DASHBOARD"] = credential("AI Studio – dashboard login", "httpBasicAuth",
                                            {"user": "studio", "password": config.DASHBOARD_PASSWORD})
        retry = workflow("retry_failed_jobs.json", subs)
        print(f"Workflow aangemaakt en actief: {retry['name']} ({retry['id']})")
    else:
        print("Retry-workflow overgeslagen: PUBLIC_BASE_URL en DASHBOARD_PASSWORD zijn daarvoor nodig.")
    set_env({"N8N_PUBLISH_WEBHOOK_URL": f"{N8N_URL}/webhook/ai-product-studio/publish",
             "N8N_WEBHOOK_SECRET": webhook_secret})
    print(".env bijgewerkt met de webhook-URL en het webhook-secret.")


if __name__ == "__main__":
    main()
