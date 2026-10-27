"""Configuratie uit omgevingsvariabelen (.env). Geheimen worden nooit gelogd of naar de UI gestuurd."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "processed"
SCENE_DIR = DATA_DIR / "scenes"
DB_PATH = DATA_DIR / "studio.db"
for d in (UPLOAD_DIR, OUTPUT_DIR, SCENE_DIR):
    d.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
VISION_MODEL = env("VISION_MODEL", "claude-opus-5-5")
# "n8n" = analyse via n8n-workflow (OpenAI-model op n8n Gateway credits), "anthropic" = direct via Claude API
VISION_PROVIDER = env("VISION_PROVIDER", "anthropic")
N8N_ANALYZE_WEBHOOK_URL = env("N8N_ANALYZE_WEBHOOK_URL")


def vision_enabled() -> bool:
    return bool(N8N_ANALYZE_WEBHOOK_URL) if VISION_PROVIDER == "n8n" else bool(ANTHROPIC_API_KEY)

SHOPIFY_SHOP = env("SHOPIFY_SHOP", "ai-bag-studio.myshopify.com")
SHOPIFY_CLIENT_ID = env("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET")
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2026-10")

PUBLISH_VIA = env("PUBLISH_VIA", "direct")
N8N_PUBLISH_WEBHOOK_URL = env("N8N_PUBLISH_WEBHOOK_URL")
N8N_WEBHOOK_SECRET = env("N8N_WEBHOOK_SECRET")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")

DASHBOARD_PASSWORD = env("DASHBOARD_PASSWORD")
WORKERS = int(env("WORKERS", "2"))
MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(env("RETRY_BASE_SECONDS", "5"))

# USD per 1M tokens (input, output) — voor kostenregistratie in het dashboard.
MODEL_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def status() -> dict:
    """Welke koppelingen geconfigureerd zijn (zonder waarden te tonen)."""
    return {
        "vision": vision_enabled(),
        "vision_model": "gpt-6-luna via n8n" if VISION_PROVIDER == "n8n" else VISION_MODEL,
        "shopify": bool(SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET),
        "shopify_shop": SHOPIFY_SHOP,
        "publish_via": PUBLISH_VIA,
        "n8n": bool(N8N_PUBLISH_WEBHOOK_URL),
    }
