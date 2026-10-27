"""Controleert de koppelingen zonder kosten te maken (geen AI-generatie, geen producten aanmaken)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, shopify  # noqa: E402

ok = True
if config.ANTHROPIC_API_KEY:
    import anthropic
    try:
        m = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY).models.retrieve(config.VISION_MODEL)
        print(f"✔ Claude API bereikbaar, model {m.id} beschikbaar")
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"✖ Claude API: {type(e).__name__}: {e}")
else:
    print("– Claude API: geen ANTHROPIC_API_KEY (AI-analyse staat uit)")

try:
    shop = shopify.graphql("{ shop { name myshopifyDomain plan { partnerDevelopment } } }")["shop"]
    print(f"✔ Shopify: verbonden met {shop['name']} ({shop['myshopifyDomain']}), dev store: {shop['plan']['partnerDevelopment']}")
except Exception as e:  # noqa: BLE001
    ok = False; print(f"✖ Shopify: {e}")
sys.exit(0 if ok else 1)
