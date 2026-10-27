import os, sys, tempfile
from pathlib import Path
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="studio-test-")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SHOPIFY_CLIENT_ID"] = ""
os.environ["SHOPIFY_CLIENT_SECRET"] = ""
os.environ["WORKERS"] = "1"
os.environ["MAX_ATTEMPTS"] = "3"
os.environ["RETRY_BASE_SECONDS"] = "0.2"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
