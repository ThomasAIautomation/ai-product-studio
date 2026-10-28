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

### Live test 2: AI-analyse via n8n Gateway credits

- Analyse loopt via de n8n-workflow "AI Product Studio – Productanalyse (vision, Gateway credits)" (`VISION_PROVIDER=n8n`). Model: `gpt-6-luna` met de door n8n beheerde OpenAI-koppeling. Er is geen eigen API-sleutel nodig en het verbruik gaat van de n8n Gateway credits af.
- De studio voegt alle foto's van een product samen tot één genummerd overzichtsbeeld ("Foto 1..n") en stuurt de prompt mee. Zo blijven de regels in `app/vision.py` de enige bron. Het antwoord wordt gecontroleerd op alle verplichte velden; een ongeldig antwoord telt als tijdelijke fout en wordt opnieuw geprobeerd.
- Resultaat met een fictieve testtas: kleur, vorm en hengsel met hoge zekerheid en bewijs. Kras en vlek gemeld met locatie, maar expliciet "niet vast te stellen of dit beschadiging is". Materiaal, merk en echtheid onder "Niet vast te stellen". Een waarschuwing dat de foto bewerkt oogt (klopt: getekende testfoto). Ongeveer 7.100 tokens en 16 s per analyse.
- Goedgekeurd → **product 15921852711252 als DRAFT** in Shopify, met de AI-teksten, tags (inclusief `staat: onbekend`) en prijs.
- Gevonden en opgelost: bij deze foto kwam een stukje achtergrond mee in de uitsnede. Het systeem gaf een waarschuwing, maar goedkeuren kon toch. Nu blokkeert goedkeuren bij een uitsnede-waarschuwing, totdat je de uitsnede corrigeert (penseel), de originele foto kiest of expliciet bevestigt. Het al aangemaakte testconcept bevat dat stukje nog wel.

### Live test 3: echte tasfoto's (28-10-2026)

Drie echte foto's (lage resolutie, ±430×570) in één batch, stijl `studio`. Zie `docs/echte-tassen-vergelijking.png` (origineel, uitsnede, resultaat).

| Foto | Uitsnede | AI-analyse | Uitkomst |
|---|---|---|---|
| Tote met beschadiging | Modellen 99,2% eens, slijtage en rafelige hoek exact behouden | Staat "matig", vlekken en beschadiging rechtsonder met locatie; merk niet als feit | **Concept 15922683248980 in Shopify** |
| Schoudertas aan standaard | Standaard (paal) meegenomen → waarschuwing | Correct, staat "onbekend" door lage detailweergave | Corrigeren met penseel |
| Bostontas in de hand | Hand + hengsel overgebleven, tas weg (modellen 48% eens) | Correct, inclusief slijtage aan een hoek | Automatisch terug naar originele foto |

Nieuw: onder 80% overeenstemming tussen de modellen gebruikt het systeem automatisch de originele foto.

**Fotorichtlijnen:** product helemaal in beeld, geen hand of standaard, neutrale ondergrond, minimaal 1000 px aan de korte zijde, één foto per zijde, plus close-ups van gebreken.

### Sfeerbeelden (28-10-2026)

**Aanpak:** de AI maakt een *lege* scène (n8n-workflow "Sfeerscène genereren", `gpt-image-1` op Gateway credits). De tas zit nooit in het generatieve model. Daarna:
1. Het vision-model wijst de plaatsing aan (vrije plek op het tafelblad of de hand) en een referentiemaat in de scène: tafeldiameter 60 cm of schouderbreedte 42 cm.
2. Met de tasafmetingen (dashboard: breedte × hoogte × diepte, cm) volgt de schaal. De **scène** wordt geschaald naar de tas; de tas blijft 1:1.
3. Originele tas-pixels erin, plus contactschaduw, pixelcontrole en lossless opslag. Past de tas op ware grootte niet in het beeld, dan wordt het beeld geweigerd en niet afgesneden.
4. Geen sfeerbeeld als de uitsnede onzeker is: dan eerst corrigeren met het penseel.
5. In Shopify staan sfeerbeelden altijd ná de productfoto's, met alt-tekst "Sfeerbeeld (AI-omgeving, tas onbewerkt)".

**Beperkingen, getest:**
- Gateway credits ondersteunen geen beeldbewerking met masker ("Gateway credits don't currently support this operation"). Een model dat de tas écht vasthoudt (vingers om het hengsel) is daardoor niet mogelijk. Daarvoor is een eigen sleutel nodig (FLUX Fill/Kontext via fal.ai, of OpenAI met eigen tegoed). De preset "Model" blijft **experimenteel**: de tas hangt naast de hand.
- Belichting en perspectief van de bronfoto blijven zichtbaar, omdat de tas niet wordt herbelicht. Een plat van bovenaf gefotografeerde tas oogt in een scène op tafelhoogte ingeplakt. Fotografeer voor sfeerbeelden rechtop, recht van voren en bij zacht daglicht.
- Resolutie: de scène wordt naar de tas geschaald. Bij lage-resolutie bronfoto's wordt het sfeerbeeld dus ook klein (hier 971×647).

Resultaat: `docs/sfeer-parijs-tote.png` (concept 15922834407764, 2 afbeeldingen) en `docs/sfeer-eerste-tests.png`.

## Openstaand

1. Optioneel: Claude als alternatief analysemodel (`VISION_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`).
2. Aanbevolen: het Shopify client secret en de n8n API-sleutel roteren, want beide zijn in de chat gedeeld.
