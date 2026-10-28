"""Zet alle credentials en workflows van de AI Product Studio in n8n (herhaalbaar: bijwerken i.p.v. dubbel aanmaken).

Gebruik (waarden uit .env of de omgeving, worden nooit geprint):
  python scripts/deploy_n8n.py

Credentials (op naam gezocht; alleen aangemaakt als ze ontbreken):
  - "AI Studio – Shopify client credentials"  (httpCustomAuth)
  - "AI Studio – webhook secret"              (httpHeaderAuth, X-Studio-Secret)
  - "Klaviyo API-sleutel (invullen)"          (httpHeaderAuth, placeholder tot Klaviyo gekoppeld is)
  - bestaande Microsoft Outlook-koppeling      (wordt gebruikt, niet aangemaakt)

Workflows (op naam bijgewerkt of aangemaakt):
  - Publiceer concept naar Shopify, Productanalyse, Sfeerscène genereren
  - Beheerdersmelding (e-mail), Foutbewaking n8n-workflows
  - Nieuwsbrief nieuwe tassen (Klaviyo) – verstuurt pas iets als KLAVIYO_LIST_ID gezet is
Alle studio-workflows krijgen de foutbewaking als "error workflow".
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
ADMIN_EMAIL = config.env("ADMIN_EMAIL", "Thomas.Lamers@hotmail.com")


def api(method: str, path: str, **kw):
    r = httpx.request(method, f"{N8N_URL}/api/v1{path}", headers={"X-N8N-API-KEY": API_KEY}, timeout=30, **kw)
    if r.status_code >= 300:
        sys.exit(f"n8n {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def list_all(path: str) -> list[dict]:
    out, cursor = [], None
    while True:
        page = api("GET", path, params={"limit": 100, **({"cursor": cursor} if cursor else {})})
        out += page.get("data", [])
        cursor = page.get("nextCursor")
        if not cursor:
            return out


def credential(existing: list[dict], name: str, ctype: str, data_fn) -> str:
    for c in existing:
        if c["name"] == name and c["type"] == ctype:
            return c["id"]
    cid = api("POST", "/credentials", json={"name": name, "type": ctype, "data": data_fn()})["id"]
    print(f"Credential aangemaakt: {name}")
    return cid


def upsert_workflow(existing: list[dict], file: str, subs: dict, active: bool, error_wf: str | None = None) -> dict:
    text = (WF_DIR / file).read_text()
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", v)
    wf = json.loads(text)
    if error_wf:
        wf["settings"]["errorWorkflow"] = error_wf
    body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
    match = next((w for w in existing if w["name"] == wf["name"]), None)
    if match:
        if match.get("active"):
            api("POST", f"/workflows/{match['id']}/deactivate")
        res = api("PUT", f"/workflows/{match['id']}", json=body)
        verb = "bijgewerkt"
    else:
        res = api("POST", "/workflows", json=body)
        verb = "aangemaakt"
    if active:
        api("POST", f"/workflows/{res['id']}/activate")
    print(f"Workflow {verb}{' en actief' if active else ' (uit)'}: {wf['name']} ({res['id']})")
    return res


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
    creds = list_all("/credentials")
    have_webhook = any(c["name"] == "AI Studio – webhook secret" for c in creds)
    if have_webhook and not config.N8N_WEBHOOK_SECRET:
        sys.exit("Webhook-credential bestaat al in n8n, maar N8N_WEBHOOK_SECRET ontbreekt in .env.")
    webhook_secret = config.N8N_WEBHOOK_SECRET or secrets.token_urlsafe(32)
    outlook = next((c for c in creds if c["type"] == "microsoftOutlookOAuth2Api"), None)

    subs = {
        "SHOP": config.SHOPIFY_SHOP, "STORE": config.SHOPIFY_SHOP.split(".")[0],
        "API_VERSION": config.SHOPIFY_API_VERSION, "PUBLIC_BASE_URL": config.PUBLIC_BASE_URL,
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "KLAVIYO_LIST_ID": config.env("KLAVIYO_LIST_ID", "VUL_IN_KLAVIYO_LIJST_ID"),
        "CRED_SHOPIFY": credential(creds, "AI Studio – Shopify client credentials", "httpCustomAuth", lambda: {"json": json.dumps(
            {"body": {"client_id": config.SHOPIFY_CLIENT_ID, "client_secret": config.SHOPIFY_CLIENT_SECRET}})}),
        "CRED_WEBHOOK": credential(creds, "AI Studio – webhook secret", "httpHeaderAuth",
                                   lambda: {"name": "X-Studio-Secret", "value": webhook_secret}),
        "CRED_KLAVIYO": credential(creds, "Klaviyo API-sleutel (invullen)", "httpHeaderAuth",
                                   lambda: {"name": "Authorization", "value": "Klaviyo-API-Key VUL_HIER_JE_PRIVATE_KEY_IN"}),
    }
    workflows = list_all("/workflows")
    error_wf = None
    if outlook:
        subs["CRED_OUTLOOK"] = outlook["id"]
        error_wf = upsert_workflow(workflows, "error_watch.json", subs, active=True)["id"]
        upsert_workflow(workflows, "admin_alert.json", subs, active=True, error_wf=error_wf)
        upsert_workflow(workflows, "newsletter_klaviyo.json", subs, active=True, error_wf=error_wf)
    else:
        print("Geen Microsoft Outlook-koppeling gevonden: meldingen en nieuwsbrief overgeslagen.")
    for file in ("publish_to_shopify.json", "analyze_vision.json", "scene_generate.json"):
        upsert_workflow(workflows, file, subs, active=True, error_wf=error_wf)

    hook = f"{N8N_URL}/webhook/ai-product-studio"
    set_env({"N8N_PUBLISH_WEBHOOK_URL": f"{hook}/publish", "N8N_ANALYZE_WEBHOOK_URL": f"{hook}/analyze",
             "N8N_ALERT_WEBHOOK_URL": f"{hook}/alert" if outlook else "", "VISION_PROVIDER": "n8n",
             "N8N_WEBHOOK_SECRET": webhook_secret})
    print(".env bijgewerkt met de webhook-URL's en het webhook-secret.")


if __name__ == "__main__":
    main()
