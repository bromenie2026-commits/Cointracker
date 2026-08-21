"""
config.py — één centrale plek voor ALLE drempelwaardes en instellingen.

Regel: nergens anders in de codebase staat een magisch getal. Wil je tunen,
dan doe je dat hier (of via environment variables / GitHub Secrets).

Alles is override-baar via env vars zodat je in een GitHub Action kunt tunen
zonder code te wijzigen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Helpers voor env-overrides
# --------------------------------------------------------------------------- #


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


# --------------------------------------------------------------------------- #
# Paden
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"

SCAN_LOG_PATH = Path(_env_str("SCAN_LOG_PATH", str(LOG_DIR / "scan_log.csv")))
DEDUP_STATE_PATH = Path(_env_str("DEDUP_STATE_PATH", str(STATE_DIR / "dedup.json")))
HOLDER_HISTORY_PATH = Path(
    _env_str("HOLDER_HISTORY_PATH", str(STATE_DIR / "holder_history.json"))
)
RISK_CONFIG_PATH = Path(_env_str("RISK_CONFIG_PATH", str(BASE_DIR / "risk_config.yaml")))

# --------------------------------------------------------------------------- #
# HTTP / rate limiting
# --------------------------------------------------------------------------- #

HTTP_TIMEOUT_SECONDS = _env_float("HTTP_TIMEOUT_SECONDS", 15.0)
HTTP_MAX_ATTEMPTS = _env_int("HTTP_MAX_ATTEMPTS", 3)  # max 3 pogingen (plan §2)
HTTP_BACKOFF_BASE_SECONDS = _env_float("HTTP_BACKOFF_BASE_SECONDS", 1.5)
HTTP_BACKOFF_MAX_SECONDS = _env_float("HTTP_BACKOFF_MAX_SECONDS", 20.0)
HTTP_USER_AGENT = _env_str(
    "HTTP_USER_AGENT", "memecoin-alert-bot/2.0 (read-only screener; no trading)"
)

# DexScreener documenteert 60 req/min op de meeste endpoints.
DEXSCREENER_MIN_INTERVAL_SECONDS = _env_float("DEXSCREENER_MIN_INTERVAL_SECONDS", 1.1)
RUGCHECK_MIN_INTERVAL_SECONDS = _env_float("RUGCHECK_MIN_INTERVAL_SECONDS", 0.6)
SOLANA_RPC_MIN_INTERVAL_SECONDS = _env_float("SOLANA_RPC_MIN_INTERVAL_SECONDS", 0.25)

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

DEXSCREENER_BASE = _env_str("DEXSCREENER_BASE", "https://api.dexscreener.com")
RUGCHECK_BASE = _env_str("RUGCHECK_BASE", "https://api.rugcheck.xyz")
RUGCHECK_API_KEY = os.getenv("RUGCHECK_API_KEY", "")  # optioneel; publiek werkt ook
SOLANA_RPC_URL = _env_str("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

CHAIN_ID = "solana"

# --------------------------------------------------------------------------- #
# Scan-omvang
# --------------------------------------------------------------------------- #

# Hoeveel kandidaten maximaal per run volledig doorgemeten worden. Houdt de
# run binnen de rate limits van een gratis GitHub Action.
MAX_CANDIDATES_PER_RUN = _env_int("MAX_CANDIDATES_PER_RUN", 40)

# Zoektermen voor de DexScreener-search als aanvullende bron van verse pairs.
DEXSCREENER_SEARCH_QUERIES = [
    q.strip()
    for q in _env_str("DEXSCREENER_SEARCH_QUERIES", "SOL,WSOL,USDC").split(",")
    if q.strip()
]

# Pair mag maximaal zo oud zijn om als "nieuw" te tellen.
MAX_PAIR_AGE_HOURS = _env_float("MAX_PAIR_AGE_HOURS", 72.0)
# ... en minimaal zo oud (te verse pairs hebben geen bruikbare metrics).
MIN_PAIR_AGE_MINUTES = _env_float("MIN_PAIR_AGE_MINUTES", 15.0)

# --------------------------------------------------------------------------- #
# Valuta
# --------------------------------------------------------------------------- #

# Alle drempels in dit bestand staan in EUR (zoals het plan). DexScreener
# levert USD. Deze koers zet om. Override via env als de koers ver afwijkt.
USD_PER_EUR = _env_float("USD_PER_EUR", 1.09)

# --------------------------------------------------------------------------- #
# HARDE FILTERS — bestaand (plan §3.1)
# --------------------------------------------------------------------------- #

MIN_MARKETCAP_EUR = _env_float("MIN_MARKETCAP_EUR", 35_000.0)
MAX_MARKETCAP_EUR = _env_float("MAX_MARKETCAP_EUR", 5_000_000.0)

# Liquiditeit/marketcap-ratio moet binnen een gezonde bandbreedte liggen.
# Te laag = niet uit te stappen. Te hoog = vaak nep/net-geseede pool.
MIN_LIQ_MC_RATIO = _env_float("MIN_LIQ_MC_RATIO", 0.05)
MAX_LIQ_MC_RATIO = _env_float("MAX_LIQ_MC_RATIO", 1.50)

# Absolute liquiditeitsbodem — onder dit bedrag is elke ratio betekenisloos.
MIN_LIQUIDITY_EUR = _env_float("MIN_LIQUIDITY_EUR", 10_000.0)

# Volume-spike: 24u-volume gedeeld door marketcap. Extreem hoog is een
# klassiek wash-trading-signaal.
MAX_VOL24_MC_RATIO = _env_float("MAX_VOL24_MC_RATIO", 25.0)
# 1u-volume gedeeld door liquiditeit — vangt korte, gefabriceerde spikes.
MAX_VOL1H_LIQ_RATIO = _env_float("MAX_VOL1H_LIQ_RATIO", 15.0)
# Minimale echte activiteit; anders is er niets te meten.
MIN_VOL24_EUR = _env_float("MIN_VOL24_EUR", 5_000.0)

# --------------------------------------------------------------------------- #
# HARDE FILTERS — rug-pull-vectoren (plan §3.2)
# Dit zijn booleans: geen drempel, alleen "moet waar zijn".
# Bij onbetrouwbare/ontbrekende data => DATA_UNAVAILABLE => fail-closed.
# --------------------------------------------------------------------------- #

REQUIRE_MINT_AUTHORITY_RENOUNCED = _env_bool("REQUIRE_MINT_AUTHORITY_RENOUNCED", True)
REQUIRE_FREEZE_AUTHORITY_RENOUNCED = _env_bool(
    "REQUIRE_FREEZE_AUTHORITY_RENOUNCED", True
)
REQUIRE_LP_LOCKED_OR_BURNED = _env_bool("REQUIRE_LP_LOCKED_OR_BURNED", True)
REQUIRE_HONEYPOT_PASS = _env_bool("REQUIRE_HONEYPOT_PASS", True)

# Welk percentage LP moet vergrendeld/geburnd zijn om als "veilig" te tellen.
MIN_LP_LOCKED_PCT = _env_float("MIN_LP_LOCKED_PCT", 90.0)

# Fail-closed schakelaar. Op False zetten is expliciet je eigen risico:
# dan worden coins met onbetrouwbare data doorgelaten. Default: True.
FAIL_CLOSED_ON_MISSING_DATA = _env_bool("FAIL_CLOSED_ON_MISSING_DATA", True)

# --------------------------------------------------------------------------- #
# ZACHTE FILTERS (plan §3.3) — drempels + gewichten
# --------------------------------------------------------------------------- #

# --- Handelsdrukte (vervangt de oude bot_score, plan §7.1) --------------- #
# De oude bot-score faalde 0 keer in 773 gevallen en correleerde niet met het
# resultaat (rho=-0,058, p=0,22). Vervangen door drie signalen die dat wél
# deden. Winnaars zijn munten met echt geld erin die nog niet in een
# maalstroom zitten: minder transacties, grótere transacties, en volume dat
# ~2x de marketcap is in plaats van ~7x.

# Volume 24u gedeeld door marketcap. Lager = rustiger = beter.
# Winnaars mediaan 2,07 / rest 7,38 (p=0,001).
MAX_VOL_MC_SOFT = _env_float("MAX_VOL_MC_SOFT", 5.0)

# Gemiddelde tradegrootte in EUR. Hoger = groter geld = beter.
# Winnaars mediaan EUR 49,68 / rest EUR 36,71 (p=0,009).
MIN_AVG_TRADE_EUR = _env_float("MIN_AVG_TRADE_EUR", 35.0)

# Transacties per minuut sinds de launch. Lager = beter.
# Winnaars mediaan 16,6 / rest 36,9 (p=0,004).
MAX_TX_PER_MIN = _env_float("MAX_TX_PER_MIN", 30.0)

# Minimaal aantal transacties voordat deze signalen betekenis hebben.
MIN_TX_FOR_ACTIVITY = _env_int("MIN_TX_FOR_ACTIVITY", 20)

# Holder-concentratie: aandeel van de top-10 wallets in de supply (%).
MAX_TOP10_HOLDER_PCT = _env_float("MAX_TOP10_HOLDER_PCT", 35.0)
# Grootste enkele holder (excl. LP/burn-adressen) in %.
MAX_SINGLE_HOLDER_PCT = _env_float("MAX_SINGLE_HOLDER_PCT", 12.0)

# Holder-groei-snelheid: nieuwe holders per minuut in de eerste uren.
# Onnatuurlijk hoog = airdrop-farming / sybil.
MAX_HOLDER_GROWTH_PER_MIN = _env_float("MAX_HOLDER_GROWTH_PER_MIN", 40.0)

# Deployer-reputatie: aandeel eerdere deploys van dezelfde wallet dat naar
# (bijna) nul ging. 0.0 - 1.0.
MAX_DEPLOYER_DEAD_RATIO = _env_float("MAX_DEPLOYER_DEAD_RATIO", 0.50)
# Een deployer met heel veel eerdere launches is per definitie verdacht.
MAX_DEPLOYER_PREVIOUS_DEPLOYS = _env_int("MAX_DEPLOYER_PREVIOUS_DEPLOYS", 5)
# Onder deze marketcap (EUR) telt een eerdere deploy als "naar nul gegaan".
DEPLOYER_DEAD_MC_EUR = _env_float("DEPLOYER_DEAD_MC_EUR", 5_000.0)
# Hoeveel transacties van de deployer-wallet we maximaal doorspitten.
DEPLOYER_MAX_SIGNATURES = _env_int("DEPLOYER_MAX_SIGNATURES", 200)
DEPLOYER_MAX_TX_FETCH = _env_int("DEPLOYER_MAX_TX_FETCH", 40)

# Social account age in dagen (leeftijd van het gekoppelde X-account).
# Vereist een X/Twitter API bearer token; zonder token blijft deze check
# "unknown" en telt hij als neutraal in de zachte score.
MIN_SOCIAL_ACCOUNT_AGE_DAYS = _env_float("MIN_SOCIAL_ACCOUNT_AGE_DAYS", 14.0)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
X_API_BASE = _env_str("X_API_BASE", "https://api.twitter.com")
X_MIN_INTERVAL_SECONDS = _env_float("X_MIN_INTERVAL_SECONDS", 1.0)

# Gewichten voor de gecombineerde zachte score (0-100, hoger = beter).
# Filters die "unknown" teruggeven krijgen de neutrale waarde uit
# SOFT_UNKNOWN_SCORE en hun gewicht telt gewoon mee, zodat te veel
# onbekenden vanzelf onder de drempel zakken.
# Gewichten herzien op 21-08-2026 na analyse van 440 coins met uitkomstdata.
# De oude gewichten waren op gevoel bedacht en de resulterende score
# correleerde NEGATIEF met het rendement (rho=-0,428, p=0,003). Deze set komt
# uit de gemeten discriminerende kracht per signaal.
SOFT_WEIGHTS = {
    "vol_mc_ratio": _env_float("W_VOL_MC_RATIO", 35.0),
    "avg_trade_eur": _env_float("W_AVG_TRADE_EUR", 20.0),
    "tx_per_min": _env_float("W_TX_PER_MIN", 20.0),
    "holder_concentration": _env_float("W_HOLDER_CONCENTRATION", 15.0),
    "deployer_reputation": _env_float("W_DEPLOYER_REPUTATION", 10.0),
    # Op 0: gemeten geen signaal (holder_growth p=0,300) of nooit data
    # (social_account_age 773/773 leeg). Blijven wél gelogd voor later.
    "holder_growth": _env_float("W_HOLDER_GROWTH", 0.0),
    "social_account_age": _env_float("W_SOCIAL_ACCOUNT_AGE", 0.0),
    "narrative": _env_float("W_NARRATIVE", 0.0),
}

SOFT_UNKNOWN_SCORE = _env_float("SOFT_UNKNOWN_SCORE", 40.0)

# Minimale gecombineerde zachte score om een mail te mogen triggeren.
MIN_SOFT_SCORE_TO_ALERT = _env_float("MIN_SOFT_SCORE_TO_ALERT", 55.0)

# --------------------------------------------------------------------------- #
# SCHADUW-CONFIGURATIES (plan §7.4)
#
# Vier drempelsets die TEGELIJK meelopen. De bot mailt volgens ACTIVE_SET,
# maar logt per coin of de andere sets óók zouden hebben gealarmeerd.
#
# Waarom: je kunt drempels niet eerlijk beoordelen op data die je al gezien
# hebt. Deze sets zijn vooraf opgeschreven en worden getoetst op munten die
# niemand van ons ooit heeft gezien.
#
# De sets verschillen ALLEEN in de marktdrempels. De vier rug-vectoren, de
# liquiditeitsbodem en de zachte score gelden voor alle sets gelijk — zo test
# je één ding tegelijk.
# --------------------------------------------------------------------------- #

SHADOW_SETS: dict[str, dict[str, float | None]] = {
    # De huidige instellingen. Controlegroep: zonder deze weet je niet of een
    # verandering iets deed of dat de markt gewoon anders was.
    "A": {
        "min_marketcap_eur": 35_000.0,
        "max_marketcap_eur": 5_000_000.0,
        "max_vol_mc": 25.0,
        "min_avg_trade_eur": None,
        "max_tx_per_min": None,
    },
    # Het voorstel op basis van de meting van 21-08-2026.
    "B": {
        "min_marketcap_eur": 15_000.0,
        "max_marketcap_eur": 150_000.0,
        "max_vol_mc": 5.0,
        "min_avg_trade_eur": 35.0,
        "max_tx_per_min": 30.0,
    },
    # Kleiner en strenger.
    "C": {
        "min_marketcap_eur": 10_000.0,
        "max_marketcap_eur": 75_000.0,
        "max_vol_mc": 3.0,
        "min_avg_trade_eur": 35.0,
        "max_tx_per_min": 30.0,
    },
    # Tail-hunter: plafond hoog om de zeldzame 26x'en te vangen.
    "D": {
        "min_marketcap_eur": 15_000.0,
        "max_marketcap_eur": 5_000_000.0,
        "max_vol_mc": 5.0,
        "min_avg_trade_eur": 35.0,
        "max_tx_per_min": 30.0,
    },
}

#: Welke set daadwerkelijk mag mailen. De rest loopt alleen mee in het log.
ACTIVE_SET = _env_str("ACTIVE_SET", "B")


def active_set() -> dict[str, Any]:
    return dict(SHADOW_SETS.get(ACTIVE_SET) or SHADOW_SETS["B"])


def _apply_active_set() -> None:
    """Laat de actieve set de losse drempels overschrijven.

    Een expliciete environment variable wint altijd: dan wil je bewust iets
    anders dan de set voorschrijft.
    """
    s = active_set()
    globals()["MIN_MARKETCAP_EUR"] = _env_float("MIN_MARKETCAP_EUR", s["min_marketcap_eur"])
    globals()["MAX_MARKETCAP_EUR"] = _env_float("MAX_MARKETCAP_EUR", s["max_marketcap_eur"])
    globals()["MAX_VOL_MC_SOFT"] = _env_float("MAX_VOL_MC_SOFT", s["max_vol_mc"])
    if s["min_avg_trade_eur"] is not None:
        globals()["MIN_AVG_TRADE_EUR"] = _env_float("MIN_AVG_TRADE_EUR", s["min_avg_trade_eur"])
    if s["max_tx_per_min"] is not None:
        globals()["MAX_TX_PER_MIN"] = _env_float("MAX_TX_PER_MIN", s["max_tx_per_min"])


_apply_active_set()

# --------------------------------------------------------------------------- #
# Dedup / cooldown (plan §3.4)
# --------------------------------------------------------------------------- #

DEDUP_COOLDOWN_HOURS = _env_float("DEDUP_COOLDOWN_HOURS", 6.0)
# Hoe lang we een adres in de dedup-state bewaren voor we het opruimen.
DEDUP_RETENTION_DAYS = _env_float("DEDUP_RETENTION_DAYS", 30.0)

# --------------------------------------------------------------------------- #
# Claude narratief-check (plan §4)
# --------------------------------------------------------------------------- #

# Standaard AAN. Ontbreekt de key, dan stopt de run met een duidelijke fout
# in plaats van stilletjes door te draaien zonder deze laag.
CLAUDE_META_CHECK_ENABLED = _env_bool("CLAUDE_META_CHECK_ENABLED", True)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = _env_str("CLAUDE_MODEL", "claude-sonnet-4-5")
CLAUDE_MAX_TOKENS = _env_int("CLAUDE_MAX_TOKENS", 700)
CLAUDE_TIMEOUT_SECONDS = _env_float("CLAUDE_TIMEOUT_SECONDS", 40.0)
# De narratief-check is een ZACHT signaal. Hij mag nooit alleen een coin
# afwijzen of goedkeuren; hij weegt mee in de zachte score.
CLAUDE_MIN_NARRATIVE_SCORE = _env_float("CLAUDE_MIN_NARRATIVE_SCORE", 35.0)

# --------------------------------------------------------------------------- #
# E-mail (plan §7)
# --------------------------------------------------------------------------- #

SMTP_HOST = _env_str("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env_int("SMTP_PORT", 587)
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_RECIPIENT = _env_str("ALERT_RECIPIENT", os.getenv("GMAIL_ADDRESS", ""))
EMAIL_SUBJECT_PREFIX = _env_str("EMAIL_SUBJECT_PREFIX", "[Memecoin Alert]")
# Maximaal aantal alerts per run — beschermt tegen mailbom bij een bug.
MAX_ALERTS_PER_RUN = _env_int("MAX_ALERTS_PER_RUN", 5)

# --------------------------------------------------------------------------- #
# Follow-up (plan §5.1)
# --------------------------------------------------------------------------- #

# De levensloop van een memecoin is vaak korter dan 24 uur. Met alleen een
# meetpunt op 24u zie je een munt die tussendoor +300% deed als "-85%".
FOLLOWUP_INTERVALS_HOURS = {
    "1h": _env_float("FOLLOWUP_1H", 1.0),
    "4h": _env_float("FOLLOWUP_4H", 4.0),
    "12h": _env_float("FOLLOWUP_12H", 12.0),
    "24h": _env_float("FOLLOWUP_24H", 24.0),
    "72h": _env_float("FOLLOWUP_72H", 72.0),
    "7d": _env_float("FOLLOWUP_7D", 168.0),
}
# Tolerantie: een regel is "toe aan" een interval zodra hij ouder is dan het
# interval. Er is geen bovengrens — een gemiste run wordt later ingehaald.
FOLLOWUP_MAX_ROWS_PER_RUN = _env_int("FOLLOWUP_MAX_ROWS_PER_RUN", 200)

# --------------------------------------------------------------------------- #
# Ruw archief (plan §7.5)
# --------------------------------------------------------------------------- #

# De volledige API-antwoorden wegschrijven zodat je later hypotheses kunt
# toetsen op data van vandaag. Gaat als Actions-artifact naar buiten, NIET de
# git-geschiedenis in — anders groeit je repo onbeperkt.
RAW_ARCHIVE_ENABLED = _env_bool("RAW_ARCHIVE_ENABLED", True)
RAW_ARCHIVE_DIR = Path(_env_str("RAW_ARCHIVE_DIR", str(BASE_DIR / "raw")))
RAW_ARCHIVE_RETENTION_DAYS = _env_float("RAW_ARCHIVE_RETENTION_DAYS", 30.0)

# --------------------------------------------------------------------------- #
# Positie-monitor (plan §8.3)
# --------------------------------------------------------------------------- #

POSITIONS_PATH = Path(_env_str("POSITIONS_PATH", str(BASE_DIR / "posities.yaml")))
POSITION_STATE_PATH = Path(_env_str("POSITION_STATE_PATH", str(STATE_DIR / "posities.json")))
POSITION_MONITOR_ENABLED = _env_bool("POSITION_MONITOR_ENABLED", True)

# --------------------------------------------------------------------------- #
# Gezondheidsalarm (plan §8.4)
# --------------------------------------------------------------------------- #

HEALTH_ALARM_ENABLED = _env_bool("HEALTH_ALARM_ENABLED", True)
# Boven dit aantal alerts in één run gaat er een waarschuwing uit.
HEALTH_MAX_ALERTS_PER_RUN = _env_int("HEALTH_MAX_ALERTS_PER_RUN", 15)
# Boven dit aandeel "geen data" bij harde filters is er iets stuk.
HEALTH_MAX_UNAVAILABLE_RATIO = _env_float("HEALTH_MAX_UNAVAILABLE_RATIO", 0.60)
# Zoveel rate-limit-hits in één run is een teken dat de frequentie omlaag moet.
HEALTH_MAX_RATE_LIMIT_HITS = _env_int("HEALTH_MAX_RATE_LIMIT_HITS", 10)
# Niet vaker dan eens per zoveel uur een alarmmail, anders spam je jezelf.
HEALTH_ALARM_COOLDOWN_HOURS = _env_float("HEALTH_ALARM_COOLDOWN_HOURS", 6.0)
HEALTH_STATE_PATH = Path(_env_str("HEALTH_STATE_PATH", str(STATE_DIR / "health.json")))

# --------------------------------------------------------------------------- #
# Veiligheidsslot
# --------------------------------------------------------------------------- #

# Dit systeem mag NOOIT handelen. Deze constante bestaat zodat er een
# expliciete, testbare assertie is: er is nergens code die kan kopen of
# verkopen, en er wordt nergens een private key gelezen.
TRADING_ENABLED = False  # NIET WIJZIGEN — er is geen trading-code aanwezig.
