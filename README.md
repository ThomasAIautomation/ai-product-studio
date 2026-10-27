# AI Product Studio — AI Bag Studio (demo)

Upload productfoto's van tassen. AI analyseert ze en schrijft Nederlandse teksten. De foto's krijgen een professionele achtergrond zonder dat de tas zelf verandert. Jij controleert het resultaat en zet het daarna als **concept (DRAFT)** in Shopify.

## Architectuur

```
 Browser (dashboard)                      Backend (FastAPI, Python)                 Externe diensten
 ┌───────────────────┐  upload/review   ┌──────────────────────────────┐
 │ Uploaden          │ ───────────────► │ REST API  /api/*             │
 │ Producten/Review  │ ◄─────────────── │ SQLite (data/studio.db)      │
 │ Monitor           │   polling 3 s    │ Wachtrij + workers (retries) │
 └───────────────────┘                  │  ├ Beeld: rembg IS-Net + U²-Net ── lokaal, gratis
                                        │  │  → compositie, pixelcontrole, lossless PNG
                                        │  ├ AI: Claude vision (structured JSON) ──► Anthropic API
                                        │  └ Publicatie (na goedkeuring):
                                        │     PUBLISH_VIA=direct ─────────────────► Shopify Admin GraphQL
                                        │     PUBLISH_VIA=n8n ──► n8n-webhook ────► Shopify Admin GraphQL
                                        └──────────────────────────────┘
```

| Onderdeel | Keuze | Waarom |
|---|---|---|
| Dashboard | Eén HTML-pagina met vanilla JS, geserveerd door de backend | Geen build-stap, gratis |
| Backend | FastAPI (Python 3.11+) | Python is nodig voor de lokale beeldmodellen |
| Database | SQLite (WAL) | Gratis, geen server nodig, later te vervangen door Postgres |
| Achtergrond verwijderen | rembg (`isnet-general-use`) + `u2net` als controlemodel | Draait lokaal: geen kosten, geen upload van foto's |
| Vision/teksten | Claude (`claude-opus-5-5`, instelbaar) met JSON-schema-output | Gestructureerd en controleerbaar, betaald per gebruik |
| Shopify | Admin GraphQL: `stagedUploadsCreate` → `productCreate(status: DRAFT, media)` → `productVariantsBulkUpdate` | Actuele API, afbeeldingen gaan mee, altijd concept |
| Shopify-auth | Client-credentials-grant van de Dev Dashboard-app (token van 24 uur, automatisch vernieuwd) | Geen langlevend token opgeslagen |
| n8n | Workflow "Publiceer concept naar Shopify" + "Mislukte jobs opnieuw inplannen" | Secrets in n8n-credentials, niet in de nodes |

## Fotobewerking: de tas blijft exact gelijk

Generatieve beeld-AI tekent een product altijd opnieuw. Krassen, vlekken en slijtage kunnen daardoor verdwijnen of veranderen. Daarom tekent geen enkel AI-model in dit systeem de tas opnieuw:

1. **Segmentatie** (IS-Net) bepaalt welke pixels bij de tas horen.
2. **Volledigheidscontrole**: een tweede, onafhankelijk model (U²-Net) vergelijkt de uitsnede met die van het eerste model. Ontbreekt er mogelijk een stuk, zoals een hengsel of riem, dan krijgt het product een rode waarschuwing in het reviewscherm.
3. **Beschadigingen behouden**: kleine "gaten" in de uitsnede zijn vaak precies de kras of vlek. Die worden met de originele pixels opgevuld. Grote openingen, zoals de ruimte binnen een hengsel, blijven open.
4. **Compositie**: de originele pixels worden op een nieuwe achtergrond gezet, op ware grootte (schaal 1:1) en zonder kleurcorrectie, verscherping of ruisfilter. Bij een sfeer- of modelfoto wordt de achtergrond naar de tas geschaald, nooit andersom.
5. **Pixelcontrole**: na de compositie wordt elke tas-pixel vergeleken met het origineel. Wijkt er één pixel af, dan wordt de afbeelding afgekeurd.
6. **Lossless opslag** (PNG, of lossless WebP boven 20 MB): geen JPEG-compressie op de tas.

Stijlen: `studio` (zachte achtergrond met contactschaduw), `wit` (marketplace) en `scene` (je eigen sfeer- of modelfoto; klik in het dashboard aan waar het hengsel moet komen, bijvoorbeeld in de hand van het model).

**Beperking (eerlijk):** een AI-gegenereerd model dat de tas vasthoudt, met vingers óver het hengsel, kan niet pixel-exact. Vingers die het hengsel bedekken veranderen per definitie die pixels. Wat wel exact kan: een modelfoto of AI-gegenereerde scène *achter* de tas, met het hengsel ter hoogte van de hand. Een generatieve scène-achtergrond (bijv. via een betaalde beeld-API) is als uitbreiding mogelijk, maar staat uit tot je daar toestemming voor geeft.

## AI-teksten: alleen wat bewezen is

Het vision-model geeft voor elk kenmerk een **zekerheid** (hoog/middel/laag) en het **bewijs** (welke foto, welk detail). De regels in de prompt (`app/vision.py`):
- Materiaal ("leer") alleen als een label het bevestigt. Anders een visuele omschrijving ("leerlook") en het materiaal komt onder *Niet vast te stellen*.
- Een merk alleen als het logo leesbaar is, en dan altijd met een waarschuwing dat de echtheid niet is gecontroleerd.
- Afmetingen, herkomst, leeftijd en echtheid → *Niet vast te stellen*.
- Zichtbare gebreken moeten in de beschrijving staan.

## Statussen, fouten en herhaalpogingen

`queued → processing → review → publish_queued → publishing → published`

- Tijdelijke fouten (429, 5xx, netwerk, Shopify THROTTLED) worden automatisch opnieuw geprobeerd met exponentiële backoff (5 s, 10 s, 20 s …), tot `MAX_ATTEMPTS`.
- Definitieve fouten (ontbrekende configuratie, validatie, `userErrors`) gaan direct naar `failed` / `publish_failed` met een leesbare melding.
- Een herhaalpoging slaat stappen over die al gelukt zijn, zodat er geen dubbele AI-kosten ontstaan.
- Na een herstart worden onderbroken jobs opnieuw ingepland.
- Elke stap wordt gelogd met duur, poging, tokens en kosten. Het Monitor-scherm toont de totalen.

## Installatie en gebruik

```bash
cp .env.example .env        # vul ANTHROPIC_API_KEY, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET in
./run.sh                    # → http://localhost:8000
.venv/bin/python scripts/check_connections.py   # test de koppelingen (kosteloos)
```

De eerste beeldverwerking downloadt de segmentatiemodellen (±350 MB, eenmalig).

**Via n8n publiceren** (optioneel; de backend moet dan publiek bereikbaar zijn, zodat Shopify de afbeeldingen kan ophalen):
```bash
N8N_API_KEY=... .venv/bin/python scripts/deploy_n8n.py   # maakt credentials + workflows aan en activeert ze
# zet daarna PUBLISH_VIA=n8n en PUBLIC_BASE_URL=https://… in .env
```

## Kosten

| Onderdeel | Kosten |
|---|---|
| Achtergrond verwijderen + compositie | €0 (lokaal) |
| Shopify Development Store | €0 |
| SQLite, dashboard, backend lokaal | €0 |
| n8n | Binnen je bestaande n8n Cloud-abonnement (1 executie per publicatie) |
| Claude vision, per product (3 foto's), schatting | `claude-opus-5-5`: ±$0,05–0,12 · `claude-haiku-5-5`: ±$0,002–0,005 |

De werkelijke kosten worden per aanroep geregistreerd en staan in het Monitor-scherm.

## Tests

`.venv/bin/python -m pytest -q tests` geeft 6 geslaagde tests:
- `test_compose_keeps_every_product_pixel_identical`: elke tas-pixel byte-identiek in stijl `studio` en `wit`
- `test_refine_fills_damage_hole_but_keeps_handle_opening`: kras blijft behouden, de opening binnen het hengsel blijft open, losse snippers worden verwijderd
- `test_scene_scales_background_not_product`: bij een scène schaalt alleen de achtergrond
- `test_save_lossless_roundtrip`: het opgeslagen bestand is identiek aan het geheugenbeeld
- `test_full_pipeline_keeps_handle_and_damage`: echte segmentatie op een testfoto; hengsel en kras zitten in de uitsnede
- `test_batch_review_edit_and_publish_flow`: batch-upload → review → bewerken → publiceren. Zonder credentials volgt een definitieve fout. Een gesimuleerde tijdelijke Shopify 503 leidt tot een automatische herhaalpoging en daarna succes, en de payload heeft `status: DRAFT`.

Daarnaast is de volledige UI-flow in Chromium (Playwright) doorlopen: uploaden van 3 foto's, batchverwerking, review, bewerken, goedkeuren en Monitor.

Getest met fictieve, gegenereerde testfoto's (`scripts/make_test_photo.py`). Echte productfoto's met een echte Claude-sleutel en een echte Shopify-store zijn nog niet getest; zie "Openstaand".

## Live test (27-10-2026)

- Shopify-verbinding: verbonden met "AI Bag Studio" (development store), Admin API `2026-10`, token via client credentials.
- n8n: workflow "AI Product Studio – Publiceer concept naar Shopify" via de API aangemaakt en geactiveerd, met de credentials "AI Studio – Shopify client credentials" en "AI Studio – webhook secret". De bestaande workflow "Shopify DRAFT gateway" is niet aangeraakt.
- End-to-end: fictieve testtas → beeldbewerking (pixelcontrole geslaagd) → teksten handmatig ingevuld (nog geen Anthropic-sleutel) → goedgekeurd → backend zet de foto in Shopify's staged storage → n8n `productCreate` + prijs → **product 15921813815636 als DRAFT** met afbeelding (READY), prijs €49,95, tags en SEO. Doorlooptijd van upload tot Shopify: ±15 s.
- In n8n-modus zonder publieke backend uploadt de backend de foto's zelf naar Shopify (staged upload) en geeft hij alleen de `resourceUrl` door aan n8n.

## Openstaand

1. **Anthropic API-sleutel** (`ANTHROPIC_API_KEY`) voor de AI-analyse met Haiku 5.5; akkoord op Haiku is gegeven.
2. Aanbevolen: het Shopify client secret en de n8n API-sleutel roteren, want beide zijn in de chat gedeeld.
