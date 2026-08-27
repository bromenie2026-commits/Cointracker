# Solana Memecoin Alert Bot

Een **filter- en meetsysteem**, geen trading-bot. Het scant nieuwe
Solana-pairs, elimineert kandidaten op harde en zachte filters, logt **alles**
(ook afwijzingen, met ruwe metric-waardes) en mailt alleen wat overleeft.

> **Dit systeem kan niet handelen.** Geen wallet, geen private key, geen
> order-code. `config.TRADING_ENABLED` staat hard op `False`, `main.py` breekt
> af als die vlag anders staat, en er is een test die de hele codebase scant op
> transactie- en sleutelcode.

**Versie 2 (21-08-2026)** — herzien op basis van 440 gemeten coins met
uitkomstdata. Wat er veranderde en waarom staat in `AANPASSINGEN.md`.

---

## Snelstart

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                   # 173 tests, geen netwerk nodig

cp .env.example .env && export $(grep -v '^#' .env | xargs)
python main.py --dry-run --limit 5 -v       # eerste echte run, geen mail
python analyze_log.py                       # wat is er gelogd
python rapport.py --print                   # het weekrapport, zonder mailen
```

Begin altijd met `--dry-run`. Die schrijft wel het logboek, maar verstuurt geen
mail en slaat geen dedup-state op.

---

## Wat het meet

### Harde filters — markt

| Filter | Drempel |
|---|---|
| `marketcap_eur` | venster van de actieve set (zie hieronder) |
| `liquidity_eur` | ≥ €10.000, met rugcheck als terugvaloptie |
| `liq_mc_ratio` | 0,05 – 1,50 |
| `volume_spike` | universele bovengrens tegen wash-trading |

### Harde filters — rug-vectoren

`mint_authority_renounced`, `freeze_authority_renounced`,
`lp_locked_or_burned` (≥ 90%), `honeypot_check`.

**Geen enkele coin die hierop faalt kan ooit een mail triggeren**, ongeacht de
rest. Afgedwongen in `filters.set_would_alert()`, met eigen tests.

Kan een harde check niet betrouwbaar opgehaald worden, dan wordt de coin
**afgewezen** (`data_unavailable`, apart gelabeld van een echte `fail`). Liever
een gemiste kans dan een gemiste rug.

### Zachte signalen

Herzien op basis van de meting. Winnaars bleken munten met echt geld erin die
nog niet in een maalstroom zitten:

| Signaal | Gewicht | Winnaars vs rest (p) |
|---|---|---|
| `vol_mc_ratio` — volume ÷ marketcap, lager is beter | 35 | 2,07 vs 7,38 (0,001) |
| `avg_trade_eur` — gemiddelde tradegrootte, hoger is beter | 20 | €49,68 vs €36,71 (0,009) |
| `tx_per_min` — transacties per minuut, lager is beter | 20 | 16,6 vs 36,9 (0,004) |
| `holder_concentration` — top-10 en grootste houder | 15 | 54,4% vs 71,8% (0,020) |
| `deployer_reputation` — eerdere deploys die naar nul gingen | 10 | (0,010) |
| `holder_growth_per_min`, `social_account_age_days`, `narrative_score` | 0 | geen signaal of geen data |

De oude `bot_score` is verwijderd: die faalde 0 keer in 773 gevallen en
correleerde niet met het resultaat.

---

## Schaduw-configuraties

Vier drempelsets lopen **tegelijk** mee. De bot mailt volgens `ACTIVE_SET`
(default B); van de andere drie wordt alleen gelogd of ze zouden hebben
gealarmeerd.

| | Marketcap | vol/mc | Extra |
|---|---|---|---|
| **A** — controlegroep | €35k – €5M | ≤ 25 | — |
| **B** — voorstel | €15k – €150k | ≤ 5 | avg trade ≥ €35, tx/min ≤ 30 |
| **C** — klein en streng | €10k – €75k | ≤ 3 | idem |
| **D** — tail-hunter | €15k – €5M | ≤ 5 | idem |

De sets verschillen **alleen** in de marktdrempels. Rug-vectoren,
liquiditeitsbodem en zachte score gelden voor alle sets gelijk — zo test je één
ding tegelijk.

Waarom dit bestaat: je kunt drempels niet eerlijk beoordelen op data die je al
gezien hebt. Deze sets zijn vooraf vastgelegd en worden getoetst op munten die
niemand heeft gezien. Wisselen doe je met de repository-variable `ACTIVE_SET`,
zonder code te wijzigen.

---

## Het logboek

`logs/scan_log.csv` krijgt één regel per gescande coin, doorgelaten én
afgewezen, met per filter twee kolommen: `<filter>__outcome` en
`<filter>__raw` — de **daadwerkelijk gemeten waarde**. Dus niet
`bot_score_pass: true`, maar `vol_mc_ratio__raw: 1.8`.

Verder per regel: alle marktwaardes in EUR, `hard_pass`, `soft_score`,
`alerted`, `alert_suppressed_reason`, `blocking_reasons`,
`data_unavailable_filters`, `shadow_A_alert` t/m `shadow_D_alert`, en de
follow-up-kolommen.

Het schema migreert vanzelf: nieuwe kolommen worden bij de eerstvolgende run
toegevoegd, oude regels blijven leesbaar met lege waarden.

### Follow-up

`followup.py` draait elk uur en vult meetpunten op **1, 4, 12, 24 en 72 uur en
7 dagen** terug in dezelfde regel, voor **alle** gelogde coins — ook de
afgewezen, zodat je kunt zien of je afwijzingen terecht waren.

Daarnaast houdt hij `max_price_seen` / `max_gain_pct` bij: de hoogste stand die
we bij een meetmoment zagen. Zonder dat kun je niet onderscheiden tussen "er
gebeurde niets" en "er gebeurde iets en je was te laat". Het is een benadering
— pieken tussen twee metingen zien we niet.

Een mislukte API-call wordt **niet** als "markt weg" geboekt; die regel blijft
leeg en wordt de volgende ronde opnieuw geprobeerd.

### Volglijst

De follow-up meet op 1, 4, 12 en 24 uur. Dat is te grof: uit de meting van
24-08 bleek dat **87% van alle momenten boven +30% in het eerste uur na het
alert lag**. Met scans van ~90 minuten en checkpoints van een uur zie je die
piek niet — je ziet alleen wat er daarna van over is.

`watchlist.py` houdt daarom een klein lijstje bij van *alleen* de munten
waarover je een alert kreeg, en meet die **elke tien minuten** gedurende twaalf
uur. Eén API-call per munt. Elke meting gaat als losse regel naar
`logs/watchlist.csv`, met de tijd sinds het alert en het rendement op dat
moment. Dat is de fijnmazige koersgeschiedenis die je nodig hebt om te
beoordelen of een verkoopregel als "sluit bij +30%" écht werkt.

Mailen bij +30/+50/+100% kán (`WATCHLIST_NOTIFY_ENABLED`), maar staat
**standaard uit**. Eerst meten. Een mail die zegt "hij staat op +30%" is pas
iets waard als de data laat zien dat eruit stappen op dat punt beter is dan
blijven zitten — en dat weten we nu niet.

Een mislukte API-call schrijft niets weg, net als bij de follow-up: een storing
is geen -100%.

### Ruw archief

`raw_store.py` schrijft de volledige API-antwoorden gzipped weg onder `raw/`,
gekoppeld via `row_id`. Zo kun je over een maand een hypothese toetsen op de
data van vandaag.

Dit gaat **als GitHub Actions-artifact** naar buiten (14 dagen), niet de
git-geschiedenis in — git vergeet nooit iets en de repo zou onbeperkt groeien.

---

## Positie-monitor

Zet in `posities.yaml` welke munten je écht gekocht hebt en tegen welke prijs
of marketcap. Bij elke run vergelijkt `monitor.py` die met de ladder uit je
eigen `risk_config.yaml` en mailt zodra een niveau geraakt wordt:

> *TEST staat op +110%. Jouw regel zegt: verkoop 50% van de positie. Daarmee
> haal je je inleg eruit.*

Hij handelt niet en kan niet handelen. Hij herinnert je aan wat je zelf
opschreef toen je nog rustig keek. Elk niveau wordt één keer gemeld.

---

## Weekrapport

`rapport.py` draait wekelijks via `rapport.yml` en mailt: welke schaduw-set
voorloopt, de papieren handel per set, piek versus eindstand, waar munten
afvielen, en of de bot zelf nog gezond is.

Dit bestaat omdat elke handmatige stap — CSV downloaden, terminal openen — een
moment is waarop het project kan stranden. Het grootste risico is niet dat de
filters niet werken, maar dat je stopt met kijken.

---

## Analyse op je eigen machine

```bash
python analyze_log.py                          # overzicht + afvalredenen
python analyze_log.py --what-if vol_mc_ratio=3 # simuleer een andere drempel
python analyze_log.py --performance            # waren de afwijzingen terecht?
python analyze_log.py --paper-trade            # had ik er geld mee verdiend?
python rapport.py --print --days 30            # het weekrapport, lokaal
```

---

## GitHub Actions

| Workflow | Wanneer | Wat |
|---|---|---|
| `loop.yml` | elk uur een start, draait ~5 uur | **alles**: volglijst /10 min, scan /20 min, follow-up /60 min |
| `rapport.yml` | maandag 06:00 UTC | weekrapport mailen |
| `tests.yml` | bij elke push | pytest |
| `scan.yml` | alleen handmatig | één losse scan |
| `followup.yml` | alleen handmatig | één losse follow-up-ronde |
| `watchlist.yml` | alleen handmatig | één losse volglijst-meting |

### Waarom één lus in plaats van drie schema's

Twee dingen bleken gemeten niet te werken (analyse 27-08):

**GitHub houdt zich niet aan het schema.** Gevraagd: elke 20 minuten een scan.
Werkelijk gemeten over 116 scans: **mediaan 97 minuten**. De volglijst stond op
elke 10 minuten en draaide eens per 101. En op 26-08 om 14:29 UTC stopte GitHub
zonder melding **18 uur** met alle geplande taken tegelijk. Op zo'n wekker kun
je geen meting bouwen.

**Drie taken, één bestand.** De scan schreef om 14:29 het logboek weg; de
follow-up was om 14:29 met een oudere versie begonnen, kon zijn eigen versie
niet meer kwijt en viel om met `exit code 1`. Twee schrijvers op één bestand
gaat een keer mis, hoe je de cron ook uit elkaar legt.

`loop.py` lost beide op: één taak die vijf uur draait en zijn eigen klok
bijhoudt. Binnen die lus loopt alles achter elkaar, dus er is per definitie
maar één schrijver. Na elke ronde wordt gecommit, dus je kunt hooguit één ronde
kwijtraken. De workflow start elk uur opnieuw; draait er al een lus, dan wacht
de nieuwe en neemt hij het over zodra de vorige klaar is — daardoor is er
vrijwel altijd één actief, ook als GitHub starts overslaat.

Loopt het pushen drie keer op rij mis (een echt conflict met wat er op GitHub
staat), dan zet de lus een kopie van het logboek in `raw/noodkopie` — dat gaat
als artifact naar buiten — en begint opnieuw vanaf de versie op GitHub. Liever
één ronde kwijt dan een bot die voor altijd vastzit.

De frequenties staan in `config.py` en zijn met repository-variables te
wijzigen: `LOOP_MINUTES`, `LOOP_SCAN_MINUTES`, `LOOP_WATCHLIST_MINUTES`,
`LOOP_FOLLOWUP_MINUTES`.

GitHub schakelt geplande workflows uit na 60 dagen zonder repo-activiteit. De
commits van de bot houden hem meestal wakker; controleer af en toe.

---

## Secrets en variables

| Secret | Verplicht | Waarvoor |
|---|---|---|
| `GMAIL_ADDRESS` | ja | afzender |
| `GMAIL_APP_PASSWORD` | ja | Gmail **app-wachtwoord** (16 tekens) |
| `ALERT_RECIPIENT` | ja | ontvanger |
| `ANTHROPIC_API_KEY` | nee | narratief-check (staat standaard uit) |
| `SOLANA_RPC_URL` | aanbevolen | eigen RPC; de publieke is zwaar gerate-limit |
| `RUGCHECK_API_KEY` | nee | hogere rugcheck-limieten |
| `X_BEARER_TOKEN` | nee | **kost geld**; zonder blijft `social_account_age` leeg |

| Variable | Waarvoor |
|---|---|
| `ACTIVE_SET` | welke drempelset mag mailen (A/B/C/D, default B) |
| `CLAUDE_META_CHECK_ENABLED` | `false` als je geen Anthropic-key hebt |
| `USD_PER_EUR` | wisselkoers, default 1,09 |

---

## Testen

```bash
pytest -q               # 173 tests, alle API-calls gemockt
pytest -q -k filters    # alleen de filterlogica
```

De suite dekt onder andere: retry/backoff en dat de HTTP-laag nooit een
exception doorlaat; dat ontbrekende rug-data tot een geblokkeerde alert leidt;
dat een harde fail alle vier de schaduw-sets blokkeert; dat de liquiditeitspool
niet als houder meetelt; dat een API-fout niet als totaalverlies wordt geboekt;
dat de cooldown een tweede mail tegenhoudt maar het loggen niet; en dat er
nergens transactie- of sleutelcode in de codebase staat.

---

## Bekende beperkingen

1. **De kandidatenbron is een promotie-feed, geen launch-feed.** DexScreener
   heeft geen publiek "nieuwe pairs"-endpoint. Je ziet een doorsnede, niet elke
   launch. Een echte launch-feed via een eigen node-provider is de grootste
   openstaande verbetering.
2. **`max_price_seen` is een benadering.** We meten op zes momenten, niet
   continu.
3. **De publieke Solana RPC is traag en gerate-limit.** De deployer-check is
   daarom begrensd en wordt overgeslagen voor coins die al afvielen.
4. **rugcheck's schema kan wijzigen.** Bij twijfel wordt de uitkomst
   `data_unavailable` en dus fail-closed: je mist kansen, je loopt geen rug op.
   Check bij een plotselinge daling in alerts eerst die kolom.
5. **De EUR/USD-koers is statisch** (`USD_PER_EUR`).

---

## Disclaimer

Gereedschap om te filteren, te loggen en te meten. Geen financieel advies. Het
systeem koopt en verkoopt niets. Memecoins zijn een categorie waarin de meeste
tokens naar nul gaan; ga ervan uit dat je elke inleg volledig kunt verliezen.
`risk_config.yaml` staat er niet voor de sier.
