import os, sys, tempfile
from pathlib import Path
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="studio-test-")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SHOPIFY_CLIENT_ID"] = ""
os.environ["SHOPIFY_CLIENT_SECRET"] = ""
os.environ["WORKERS"] = "1"
# Tests nooit tegen live diensten: alle koppelingen expliciet uit (overschrijft .env).
for k in ("N8N_ANALYZE_WEBHOOK_URL", "N8N_PUBLISH_WEBHOOK_URL", "N8N_WEBHOOK_SECRET", "N8N_API_KEY", "PUBLIC_BASE_URL"):
    os.environ[k] = ""
os.environ["VISION_PROVIDER"] = "anthropic"
os.environ["PUBLISH_VIA"] = "direct"
os.environ["MAX_ATTEMPTS"] = "3"
os.environ["RETRY_BASE_SECONDS"] = "0.2"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
