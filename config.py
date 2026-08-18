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

# Bot-score 0-100, hoger = meer bot-achtig. Boven deze grens: zacht negatief.
MAX_BOT_SCORE = _env_float("MAX_BOT_SCORE", 60.0)

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
SOFT_WEIGHTS = {
    "bot_score": _env_float("W_BOT_SCORE", 25.0),
    "holder_concentration": _env_float("W_HOLDER_CONCENTRATION", 25.0),
    "holder_growth": _env_float("W_HOLDER_GROWTH", 15.0),
    "deployer_reputation": _env_float("W_DEPLOYER_REPUTATION", 25.0),
    "social_account_age": _env_float("W_SOCIAL_ACCOUNT_AGE", 5.0),
    "narrative": _env_float("W_NARRATIVE", 5.0),
}

SOFT_UNKNOWN_SCORE = _env_float("SOFT_UNKNOWN_SCORE", 40.0)

# Minimale gecombineerde zachte score om een mail te mogen triggeren.
MIN_SOFT_SCORE_TO_ALERT = _env_float("MIN_SOFT_SCORE_TO_ALERT", 55.0)

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

FOLLOWUP_INTERVALS_HOURS = {
    "24h": _env_float("FOLLOWUP_24H", 24.0),
    "72h": _env_float("FOLLOWUP_72H", 72.0),
    "7d": _env_float("FOLLOWUP_7D", 168.0),
}
# Tolerantie: een regel is "toe aan" een interval zodra hij ouder is dan het
# interval. Er is geen bovengrens — een gemiste run wordt later ingehaald.
FOLLOWUP_MAX_ROWS_PER_RUN = _env_int("FOLLOWUP_MAX_ROWS_PER_RUN", 120)

# --------------------------------------------------------------------------- #
# Veiligheidsslot
# --------------------------------------------------------------------------- #

# Dit systeem mag NOOIT handelen. Deze constante bestaat zodat er een
# expliciete, testbare assertie is: er is nergens code die kan kopen of
# verkopen, en er wordt nergens een private key gelezen.
TRADING_ENABLED = False  # NIET WIJZIGEN — er is geen trading-code aanwezig.
