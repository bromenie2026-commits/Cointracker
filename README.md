# Solana Memecoin Alert Bot

Een **filter- en logging-systeem**, geen trading-bot. Het scant nieuwe
Solana-pairs, elimineert kandidaten op harde en zachte filters, logt **alles**
(ook afwijzingen, met ruwe metric-waardes) en mailt alleen wat overleeft.

De laatste sentiment-check op X en de koopbeslissing blijven bij jou.

> **Dit systeem kan niet handelen.** Er is geen wallet-koppeling, geen private
> key, geen order-code. `config.TRADING_ENABLED` staat hard op `False` en
> `main.py` breekt af als die vlag ooit anders staat. Er is een test die de
> hele codebase scant op transactie- en sleutelcode
> (`tests/test_safety_and_main.py::test_geen_trading_code_aanwezig`).

---

## Inhoud

- [Snelstart](#snelstart)
- [Bestandsoverzicht](#bestandsoverzicht)
- [Hoe de filters werken](#hoe-de-filters-werken)
- [Het logbestand](#het-logbestand)
- [Follow-up](#follow-up)
- [Drempels tunen](#drempels-tunen)
- [GitHub Actions](#github-actions)
- [Secrets](#secrets)
- [Testen](#testen)
- [Bekende beperkingen](#bekende-beperkingen)

---

## Snelstart

```bash
git clone <jouw-repo> && cd memecoin-alert-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 1. Tests draaien (geen netwerk nodig, alles is gemockt)
pytest -q

# 2. Eerste echte run, zonder mail
cp .env.example .env      # vul in wat je hebt
export $(grep -v '^#' .env | xargs)
python main.py --dry-run --limit 5 -v

# 3. Eén bekend contractadres doormeten
python main.py --token <mint-adres> --dry-run -v

# 4. Bekijk wat er gelogd is
python analyze_log.py
```

Begin altijd met `--dry-run`. Die schrijft wel het logbestand, maar verstuurt
geen mail en slaat geen dedup-state op. Zo zie je eerst wat het systeem zou
doen voordat je je inbox eraan blootstelt.

### Zonder Claude-narratief-check draaien

De narratief-check staat **standaard aan**. Ontbreekt `ANTHROPIC_API_KEY`, dan
stopt de run met exit code 2 en een duidelijke melding — bewust luid, zodat je
niet per ongeluk maandenlang zonder die laag draait. Wil je hem uit:

```bash
export CLAUDE_META_CHECK_ENABLED=false
```

---

## Bestandsoverzicht

```
memecoin-alert-bot/
├── main.py                  # orchestratie: scan → filter → log → notify
├── config.py                # ALLE drempels en instellingen, één plek
├── models.py                # gedeelde datastructuren (FilterResult etc.)
├── http_client.py           # retry, exponential backoff, per-host throttling
├── data_sources.py          # DexScreener + Solana RPC + X-account-leeftijd
├── rugcheck.py              # de vier rug-vectoren (rugcheck.xyz + RPC-fallback)
├── deployer_reputation.py   # historie van de deployer-wallet
├── filters.py               # alle filterlogica, retourneert ruwe waardes
├── dedup.py                 # cooldown per contractadres
├── state_store.py           # atomaire JSON-state op schijf
├── csv_log.py               # append-only scan_log.csv met ruwe waardes
├── claude_meta_check.py     # narratief-beoordeling via de Claude API
├── notify.py                # e-mail via Gmail SMTP
├── followup.py              # losse job: 24u/72u/7d terugschrijven
├── analyze_log.py           # drempel-tuning achteraf
├── risk_config.yaml         # risicochecklist (puur referentie, wordt niet uitgevoerd)
├── logs/scan_log.csv        # het hart van het systeem
├── state/                   # dedup + holder-historie
├── tests/                   # 122 tests, alle API-calls gemockt
└── .github/workflows/       # scan.yml, followup.yml, tests.yml
```

Elke module is los testbaar. `filters.py` doet zelf geen netwerkcalls — hij
krijgt data aangeleverd — zodat een API-wijziging nooit de filterlogica breekt.

---

## Hoe de filters werken

### Harde filters — markt

| Filter | Drempel (config) |
|---|---|
| `marketcap_eur` | €35.000 – €5.000.000 |
| `liquidity_eur` | ≥ €10.000 |
| `liq_mc_ratio` | 0,05 – 1,50 |
| `volume_spike` | vol24/mc ≤ 25, vol1h/liq ≤ 15, vol24 ≥ €5.000 |

### Harde filters — rug-vectoren

| Filter | Eis |
|---|---|
| `mint_authority_renounced` | moet renounced zijn |
| `freeze_authority_renounced` | moet renounced zijn |
| `lp_locked_or_burned` | ≥ 90% vergrendeld of geburnd |
| `honeypot_check` | geen verkoop-blokkerende risico's |

**Geen enkele coin die hierop faalt kan ooit een mail triggeren**, ongeacht hoe
goed de rest scoort. Dat is afgedwongen in `filters.should_alert()`: de harde
check komt vóór de zachte score, met een eigen test.

### Fail-closed

Kan een harde check niet betrouwbaar opgehaald worden, dan wordt de coin
**afgewezen**, niet doorgelaten. In het log heet dat `data_unavailable` — apart
gelabeld van een echte `fail`, zodat je later kunt zien of je een API-probleem
had in plaats van een slechte coin. Zet je `FAIL_CLOSED_ON_MISSING_DATA=false`,
dan gaat die bescherming uit; dat is expliciet je eigen risico.

### Zachte signalen

| Signaal | Wat het meet |
|---|---|
| `bot_score` (0–100) | koop/verkoop-onbalans, minuscule gemiddelde trades, churn t.o.v. liquiditeit, onnatuurlijk constant tempo |
| `holder_concentration` | top-10 en grootste holder als % van de supply (burn-adressen eruit gefilterd) |
| `holder_growth_per_min` | onnatuurlijk snelle holder-stijging = airdrop-farming-signaal |
| `deployer_reputation` | eerdere deploys van dezelfde wallet die naar nul gingen |
| `social_account_age_days` | leeftijd van het gekoppelde X-account (puur leeftijd, geen sentiment) |
| `narrative_score` | Claude's oordeel over naam/ticker/socials |

Deze worden gewogen tot één score van 0–100 (`SOFT_WEIGHTS` in `config.py`).
Een signaal dat "unknown" is telt mee als `SOFT_UNKNOWN_SCORE` (default 40),
dus veel onbekenden duwen de score vanzelf onder de alert-drempel.

De zachte score is gradueel: ruim binnen een drempel scoort hoger dan er net
onder. Dat voorkomt dat alles wat de drempel haalt er identiek uitziet.

### Dedup

Voor een coin een mail triggert, kijkt `dedup.py` of dit adres binnen de
cooldown (default 6 uur) al gealerteerd is. Zo ja: alleen loggen. Dat voorkomt
spam van coins die net op de grens van een drempel fluctueren.

---

## Het logbestand

`logs/scan_log.csv` krijgt één regel per gescande coin, **doorgelaten én
afgewezen**, met per filter twee kolommen:

- `<filter>__outcome` — `pass` / `fail` / `data_unavailable` / `skipped`
- `<filter>__raw` — de **daadwerkelijk gemeten waarde**

Dus niet `bot_score_pass: true`, maar `bot_score__raw: 37`. Dat is het hele
punt: zonder de ruwe waarde kun je achteraf geen drempels tunen zonder opnieuw
te scannen. Dicts (zoals `holder_concentration`) worden compacte JSON in de
cel, zodat je ze programmatisch kunt teruglezen.

Verder per regel: alle marktwaardes in EUR, `hard_pass`, `soft_score`,
`alerted`, `alert_suppressed_reason`, `blocking_reasons`,
`data_unavailable_filters`, de rugcheck-bron en de deployer-wallet.

Het schema is stabiel en migreert vanzelf: nieuwe kolommen worden bij de
eerstvolgende rewrite toegevoegd, oude regels blijven leesbaar.

---

## Follow-up

`followup.py` draait elk uur als losse job en:

1. zoekt logregels die 24u, 72u of 7 dagen oud zijn en nog geen follow-up-data
   hebben voor dat interval;
2. vraagt prijs en marketcap opnieuw op bij DexScreener;
3. schrijft het resultaat terug in **dezelfde regel**
   (`price_24h`, `mc_eur_24h`, `followup_24h_at`, enzovoort).

Dit gebeurt voor **alle** gelogde coins, ook de afgewezen — zodat je kunt zien
of je afwijzingen terecht waren. Is er geen pair meer, dan wordt dat als `0`
gelogd met een notitie: dat is het meest informatieve resultaat dat er is.

Een gemiste run wordt vanzelf ingehaald: een regel die drie dagen blijft
liggen krijgt bij de volgende run alsnog `24h`, `72h` én `7d` ingevuld.

---

## Drempels tunen

```bash
python analyze_log.py                            # verdeling + afvalredenen
python analyze_log.py --what-if bot_score=40     # simuleer een andere drempel
python analyze_log.py --what-if holder_growth_per_min=25
python analyze_log.py --performance              # waren de afwijzingen terecht?
```

`--performance` vergelijkt de mediane marketcap-verandering na 24u/72u/7d van
gealerteerde versus afgewezen coins. Presteren je afwijzingen even goed als je
alerts, dan filter je op de verkeerde dingen.

Verzamel eerst een paar honderd regels voordat je aan drempels gaat draaien.
Met twintig regels tune je op ruis.

---

## GitHub Actions

- **`scan.yml`** — elke 15 minuten (`workflow_dispatch` voor handmatig, met
  een dry-run-optie). Committeert `logs/` en `state/` terug naar de repo, want
  zonder die state werken dedup en holder-groei niet tussen runs.
- **`followup.yml`** — elk uur op :07, zodat hij niet met de scan botst.
- **`tests.yml`** — pytest bij elke push.

Beide job's gebruiken `concurrency`-groepen: twee gelijktijdige runs zouden
elkaars logbestand overschrijven. De push doet drie pogingen met
`git pull --rebase`, voor als scan en followup elkaar toch kruisen.

**Let op:** GitHub schakelt scheduled workflows automatisch uit na 60 dagen
zonder repo-activiteit. De commits van de bot zelf houden hem meestal wakker,
maar controleer af en toe of de schedules nog lopen.

`*/15` is een verstandige ondergrens. DexScreener staat 60 requests per minuut
toe en één run doet er tientallen; sneller scannen levert vooral
rate-limit-hits op. Die worden geteld en aan het eind van elke run gelogd
("`dexscreener: 47 calls, 0x rate-limited`") — zie je daar getallen boven nul,
zet de frequentie omlaag.

---

## Secrets

Zet deze als **repository secrets** (Settings → Secrets and variables →
Actions). Nooit in code, nooit in `config.py`.

| Secret | Verplicht | Waarvoor |
|---|---|---|
| `GMAIL_ADDRESS` | ja | afzender |
| `GMAIL_APP_PASSWORD` | ja | Gmail **app-wachtwoord** (16 tekens), niet je gewone wachtwoord |
| `ALERT_RECIPIENT` | ja | ontvanger |
| `ANTHROPIC_API_KEY` | ja* | narratief-check (*tenzij `CLAUDE_META_CHECK_ENABLED=false`) |
| `SOLANA_RPC_URL` | aanbevolen | eigen RPC (Helius/QuickNode); de publieke RPC is zwaar gerate-limit |
| `RUGCHECK_API_KEY` | nee | hogere rugcheck-limieten |
| `X_BEARER_TOKEN` | nee | zonder token blijft `social_account_age_days` "unknown" |

Een Gmail app-wachtwoord maak je aan op je Google-account onder Beveiliging →
2-staps-verificatie → App-wachtwoorden. Dat werkt alleen met 2FA aan.

Tuning-waardes die geen geheim zijn (bijvoorbeeld `USD_PER_EUR`) kun je als
repository *variable* zetten in plaats van als secret.

---

## Testen

```bash
pytest -q            # 122 tests, geen netwerk
pytest -q -k filters # alleen de filterlogica
```

Alle externe calls zijn gemockt. De suite dekt onder andere:

- retry/backoff, rate-limit-telling, en dat de HTTP-laag nooit een exception
  doorlaat;
- dat ontbrekende rug-data leidt tot `data_unavailable` en dus tot een
  geblokkeerde alert;
- dat een harde fail een alert blokkeert ondanks een perfecte zachte score;
- dat afgewezen coins wél gelogd worden, mét ruwe waardes;
- dat de cooldown een tweede mail tegenhoudt maar het loggen niet;
- dat een kapotte coin de run niet stopt;
- dat er nergens transactie- of sleutelcode in de codebase staat.

---

## Bekende beperkingen

Eerlijk over wat dit systeem *niet* kan:

1. **DexScreener heeft geen publiek "nieuwe pairs"-endpoint.** We combineren
   de tokenprofielen-feed met een paar zoekopdrachten en filteren op
   pair-leeftijd. Je ziet dus niet elke launch — je ziet een representatieve
   doorsnede. Wil je volledige dekking, dan heb je een betaalde feed of een
   eigen mempool-listener nodig.

2. **De bot-score is een proxy.** DexScreener geeft geaggregeerde
   transactietellingen, geen individuele wallets. Echte wallet-clustering
   vereist een geïndexeerde datasource (Helius/Bitquery). De score meet
   patronen die met botgedrag correleren, geen bewezen bots.

3. **`social_account_age_days` vereist een X API-token.** Zonder token blijft
   die check "unknown" en telt hij neutraal mee. Dat is bewust: gokken is
   erger dan niet weten.

4. **De publieke Solana RPC is traag en gerate-limit.** De
   deployer-reputatie-check doet de meeste RPC-calls en is daarom begrensd
   (`DEPLOYER_MAX_TX_FETCH`) én wordt overgeslagen voor coins die al op een
   harde filter zijn afgevallen. Met een eigen RPC-endpoint kun je die
   grenzen ruimer zetten.

5. **rugcheck's responseschema kan wijzigen.** `rugcheck.py` probeert
   meerdere veldpaden en valt terug op de RPC voor mint/freeze. Verandert er
   toch iets, dan wordt de uitkomst `data_unavailable` en dus fail-closed —
   je mist kansen, je loopt geen rug op. Controleer bij een plotselinge daling
   in alerts eerst de kolom `data_unavailable_filters` in het log.

6. **De EUR/USD-koers is statisch** (`USD_PER_EUR`, default 1,09). Wijkt de
   koers ver af, pas de variable aan.

---

## Disclaimer

Dit is gereedschap om te filteren en te loggen, geen financieel advies. Het
systeem koopt en verkoopt niets. Memecoins zijn een categorie waarin de meeste
tokens naar nul gaan; ga ervan uit dat je elke inleg volledig kunt verliezen.
De `risk_config.yaml` staat er niet voor de sier.
