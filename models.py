"""
models.py — gedeelde datastructuren.

Belangrijkste idee: elke filter levert een FilterResult met de RUWE waarde
erin, niet alleen pass/fail. Dat is wat drempel-tuning achteraf mogelijk
maakt (plan §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Outcome(str, Enum):
    """Uitkomst van één filter."""

    PASS = "pass"
    FAIL = "fail"
    #: Data kon niet betrouwbaar opgehaald worden. Bij een harde filter
    #: betekent dit fail-closed (afwijzen), maar het wordt APART gelogd van
    #: een echte fail zodat je later kunt zien of je een API-probleem had.
    DATA_UNAVAILABLE = "data_unavailable"
    #: Filter is bewust uitgezet in de config.
    SKIPPED = "skipped"

    @property
    def is_blocking(self) -> bool:
        return self in (Outcome.FAIL, Outcome.DATA_UNAVAILABLE)


@dataclass
class FilterResult:
    """Resultaat van één individuele filter."""

    name: str
    outcome: Outcome
    hard: bool
    #: De daadwerkelijk gemeten waarde (bv. bot_score 37, top10_pct 22.4).
    raw_value: Any = None
    #: De drempel waartegen getoetst is, als string voor de log.
    threshold: str = ""
    #: Vrije toelichting, incl. de reden bij DATA_UNAVAILABLE.
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome.value,
            "hard": self.hard,
            "raw_value": self.raw_value,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass
class PairData:
    """Genormaliseerde DexScreener-pairgegevens (alle bedragen in USD)."""

    token_address: str
    pair_address: str = ""
    dex_id: str = ""
    symbol: str = ""
    name: str = ""
    price_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    volume_h1_usd: Optional[float] = None
    volume_h6_usd: Optional[float] = None
    volume_h24_usd: Optional[float] = None
    price_change_h1: Optional[float] = None
    price_change_h24: Optional[float] = None
    buys_h1: Optional[int] = None
    sells_h1: Optional[int] = None
    buys_h24: Optional[int] = None
    sells_h24: Optional[int] = None
    pair_created_at_ms: Optional[int] = None
    url: str = ""
    websites: list[str] = field(default_factory=list)
    socials: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def age_minutes(self) -> Optional[float]:
        if not self.pair_created_at_ms:
            return None
        import time

        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60_000.0)

    def twitter_handle(self) -> Optional[str]:
        """Haalt de X/Twitter-handle uit de socials, als die er is."""
        for social in self.socials:
            url = str(social.get("url", ""))
            stype = str(social.get("type", "")).lower()
            if stype in {"twitter", "x"} or "twitter.com" in url or "x.com" in url:
                handle = url.rstrip("/").split("/")[-1]
                handle = handle.split("?")[0]
                if handle and handle.lower() not in {"x.com", "twitter.com"}:
                    return handle
        return None


@dataclass
class RugcheckReport:
    """Genormaliseerd rugcheck-resultaat. None = onbekend (fail-closed)."""

    mint: str
    available: bool = False
    mint_authority_renounced: Optional[bool] = None
    freeze_authority_renounced: Optional[bool] = None
    lp_locked_pct: Optional[float] = None
    lp_locked_or_burned: Optional[bool] = None
    honeypot_ok: Optional[bool] = None
    rugged: Optional[bool] = None
    score: Optional[int] = None
    score_normalised: Optional[int] = None
    top_holders_pct: Optional[float] = None
    largest_holder_pct: Optional[float] = None
    total_holders: Optional[int] = None
    #: Terugvaloptie als DexScreener geen liquiditeit levert (bugfix 4.2).
    total_market_liquidity_usd: Optional[float] = None
    creator: Optional[str] = None
    risks: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""  # "rugcheck" | "rpc-fallback" | "mixed"
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeployerReputation:
    """Historie van de deployer-wallet."""

    wallet: Optional[str] = None
    available: bool = False
    previous_deploys: Optional[int] = None
    dead_deploys: Optional[int] = None
    dead_ratio: Optional[float] = None
    error: str = ""
    inspected_mints: list[str] = field(default_factory=list)


@dataclass
class NarrativeCheck:
    """Uitkomst van de optionele Claude-narratief-check."""

    available: bool = False
    score: Optional[float] = None  # 0-100, hoger = sterker narratief
    verdict: str = ""
    reasoning: str = ""
    error: str = ""


@dataclass
class Evaluation:
    """Volledige beoordeling van één token."""

    token_address: str
    symbol: str = ""
    name: str = ""
    pair: Optional[PairData] = None
    rugcheck: Optional[RugcheckReport] = None
    deployer: Optional[DeployerReputation] = None
    narrative: Optional[NarrativeCheck] = None
    results: list[FilterResult] = field(default_factory=list)
    soft_score: Optional[float] = None
    alerted: bool = False
    alert_suppressed_reason: str = ""
    #: Per drempelset of die gealarmeerd zou hebben (plan §7.4).
    shadow_sets: dict[str, bool] = field(default_factory=dict)
    #: Verandering sinds de vorige waarneming van deze munt (plan §7.2).
    deltas: dict[str, Any] = field(default_factory=dict)

    # ---------------- afgeleide eigenschappen ---------------- #

    def by_name(self, name: str) -> Optional[FilterResult]:
        for r in self.results:
            if r.name == name:
                return r
        return None

    @property
    def hard_results(self) -> list[FilterResult]:
        return [r for r in self.results if r.hard]

    @property
    def soft_results(self) -> list[FilterResult]:
        return [r for r in self.results if not r.hard]

    @property
    def hard_pass(self) -> bool:
        """Alle harde filters PASS (of bewust SKIPPED)."""
        return all(not r.outcome.is_blocking for r in self.hard_results)

    @property
    def blocking_reasons(self) -> list[str]:
        out = []
        for r in self.hard_results:
            if r.outcome is Outcome.FAIL:
                out.append(f"{r.name}=FAIL({r.raw_value})")
            elif r.outcome is Outcome.DATA_UNAVAILABLE:
                out.append(f"{r.name}=DATA_UNAVAILABLE({r.detail or 'geen data'})")
        return out

    @property
    def x_search_url(self) -> str:
        """Zoeklink voor de handmatige sentiment-check (plan §4)."""
        from urllib.parse import quote_plus

        query = self.token_address
        if self.symbol:
            query = f"${self.symbol} OR {self.token_address}"
        return f"https://x.com/search?q={quote_plus(query)}&f=live"

    @property
    def dexscreener_url(self) -> str:
        if self.pair and self.pair.url:
            return self.pair.url
        return f"https://dexscreener.com/solana/{self.token_address}"

    @property
    def rugcheck_url(self) -> str:
        return f"https://rugcheck.xyz/tokens/{self.token_address}"
