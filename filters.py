"""
filters.py — alle harde + zachte filterlogica.

Contract van deze module:
* Elke filter retourneert een FilterResult MET de ruwe gemeten waarde.
* Ontbrekende data => Outcome.DATA_UNAVAILABLE, apart gelabeld van een
  echte FAIL, en bij harde filters fail-closed (= blokkeert de alert).
* Deze module doet zelf geen netwerkcalls. Hij krijgt alles aangeleverd.
  Dat maakt hem triviaal te testen met verzonnen inputs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import config
import data_sources
import state_store
from models import (
    DeployerReputation,
    Evaluation,
    FilterResult,
    NarrativeCheck,
    Outcome,
    PairData,
    RugcheckReport,
)

log = logging.getLogger(__name__)


#: Vaste volgorde van alle filters. csv_log.py bouwt hier de kolomkoppen mee
#: op, zodat het logbestand een stabiel schema houdt.
HARD_FILTER_NAMES = [
    "marketcap_eur",
    "liquidity_eur",
    "liq_mc_ratio",
    "volume_spike",
    "mint_authority_renounced",
    "freeze_authority_renounced",
    "lp_locked_or_burned",
    "honeypot_check",
]
SOFT_FILTER_NAMES = [
    "vol_mc_ratio",
    "avg_trade_eur",
    "tx_per_min",
    "holder_concentration",
    "holder_growth_per_min",
    "deployer_reputation",
    "social_account_age_days",
    "narrative_score",
]
FILTER_NAMES = HARD_FILTER_NAMES + SOFT_FILTER_NAMES


def _r(
    name: str,
    outcome: Outcome,
    hard: bool,
    raw_value: Any = None,
    threshold: str = "",
    detail: str = "",
) -> FilterResult:
    return FilterResult(
        name=name,
        outcome=outcome,
        hard=hard,
        raw_value=raw_value,
        threshold=threshold,
        detail=detail,
    )


def _unavailable(name: str, hard: bool, reason: str, threshold: str = "") -> FilterResult:
    """Fail-closed: bij een harde filter blokkeert dit de alert."""
    if hard and not config.FAIL_CLOSED_ON_MISSING_DATA:
        return _r(
            name,
            Outcome.PASS,
            hard,
            None,
            threshold,
            f"data ontbreekt ({reason}) maar fail-closed staat UIT",
        )
    return _r(name, Outcome.DATA_UNAVAILABLE, hard, None, threshold, reason)


# =========================================================================== #
# HARDE FILTERS — markt (plan §3.1)
# =========================================================================== #


def filter_marketcap(pair: Optional[PairData]) -> FilterResult:
    name = "marketcap_eur"
    threshold = f"{config.MIN_MARKETCAP_EUR:,.0f} - {config.MAX_MARKETCAP_EUR:,.0f} EUR"
    if pair is None:
        return _unavailable(name, True, "geen pairdata", threshold)

    mc_usd = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    if mc_usd is None:
        return _unavailable(name, True, "marketCap en fdv beide leeg", threshold)

    mc_eur = data_sources.usd_to_eur(mc_usd) or 0.0
    ok = config.MIN_MARKETCAP_EUR <= mc_eur <= config.MAX_MARKETCAP_EUR
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        True,
        round(mc_eur, 2),
        threshold,
        "" if ok else ("te laag" if mc_eur < config.MIN_MARKETCAP_EUR else "te hoog"),
    )


def liquidity_usd(
    pair: Optional[PairData], report: Optional[RugcheckReport] = None
) -> tuple[Optional[float], str]:
    """Liquiditeit in USD, met rugcheck als terugvaloptie (bugfix 4.2).

    DexScreener liet bij 45% van de gescande coins `liquidity.usd` leeg. Omdat
    liquiditeit een harde, fail-closed filter is, vielen die allemaal af — elf
    van de 24 best presterende coins zaten daarbij. rugcheck levert hetzelfde
    getal als `totalMarketLiquidity`.
    """
    if pair is not None and pair.liquidity_usd is not None:
        return pair.liquidity_usd, "dexscreener"
    if report is not None and report.total_market_liquidity_usd is not None:
        return report.total_market_liquidity_usd, "rugcheck"
    return None, "geen"


def filter_liquidity(
    pair: Optional[PairData], report: Optional[RugcheckReport] = None
) -> FilterResult:
    name = "liquidity_eur"
    threshold = f">= {config.MIN_LIQUIDITY_EUR:,.0f} EUR"
    liq_usd, bron = liquidity_usd(pair, report)
    if liq_usd is None:
        return _unavailable(name, True, "geen liquiditeitsdata (DexScreener noch rugcheck)", threshold)
    liq_eur = data_sources.usd_to_eur(liq_usd) or 0.0
    ok = liq_eur >= config.MIN_LIQUIDITY_EUR
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        True,
        round(liq_eur, 2),
        threshold,
        f"bron: {bron}",
    )


def filter_liq_mc_ratio(
    pair: Optional[PairData], report: Optional[RugcheckReport] = None
) -> FilterResult:
    name = "liq_mc_ratio"
    threshold = f"{config.MIN_LIQ_MC_RATIO} - {config.MAX_LIQ_MC_RATIO}"
    if pair is None:
        return _unavailable(name, True, "geen pairdata", threshold)

    mc = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    liq, _bron = liquidity_usd(pair, report)
    if mc is None or liq is None or mc <= 0:
        return _unavailable(name, True, "marketcap of liquiditeit leeg/0", threshold)

    ratio = liq / mc
    ok = config.MIN_LIQ_MC_RATIO <= ratio <= config.MAX_LIQ_MC_RATIO
    detail = ""
    if not ok:
        detail = "te dun (niet uit te stappen)" if ratio < config.MIN_LIQ_MC_RATIO else "onnatuurlijk hoog"
    return _r(name, Outcome.PASS if ok else Outcome.FAIL, True, round(ratio, 4), threshold, detail)


def filter_volume_spike(
    pair: Optional[PairData], report: Optional[RugcheckReport] = None
) -> FilterResult:
    """Extreme volume t.o.v. marketcap/liquiditeit = wash-trading-signaal."""
    name = "volume_spike"
    threshold = (
        f"vol24/mc <= {config.MAX_VOL24_MC_RATIO}, "
        f"vol1h/liq <= {config.MAX_VOL1H_LIQ_RATIO}, "
        f"vol24 >= {config.MIN_VOL24_EUR:,.0f} EUR"
    )
    if pair is None:
        return _unavailable(name, True, "geen pairdata", threshold)

    mc = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    vol24 = pair.volume_h24_usd
    if vol24 is None or mc is None or mc <= 0:
        return _unavailable(name, True, "volume of marketcap leeg", threshold)

    vol24_mc = vol24 / mc
    liq, _bron = liquidity_usd(pair, report)
    vol1h_liq = None
    if pair.volume_h1_usd is not None and liq:
        vol1h_liq = pair.volume_h1_usd / liq

    vol24_eur = data_sources.usd_to_eur(vol24) or 0.0
    raw = {
        "vol24_mc_ratio": round(vol24_mc, 4),
        "vol1h_liq_ratio": round(vol1h_liq, 4) if vol1h_liq is not None else None,
        "vol24_eur": round(vol24_eur, 2),
    }

    problems = []
    if vol24_mc > config.MAX_VOL24_MC_RATIO:
        problems.append(f"vol24/mc {vol24_mc:.1f} boven max")
    if vol1h_liq is not None and vol1h_liq > config.MAX_VOL1H_LIQ_RATIO:
        problems.append(f"vol1h/liq {vol1h_liq:.1f} boven max")
    if vol24_eur < config.MIN_VOL24_EUR:
        problems.append("te weinig echt volume")

    ok = not problems
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        True,
        raw,
        threshold,
        "; ".join(problems),
    )


# =========================================================================== #
# HARDE FILTERS — rug-pull-vectoren (plan §3.2)
# =========================================================================== #


def _bool_hard_filter(
    name: str,
    value: Optional[bool],
    enabled: bool,
    threshold: str,
    unavailable_reason: str,
    fail_detail: str,
) -> FilterResult:
    if not enabled:
        return _r(name, Outcome.SKIPPED, True, value, threshold, "uitgezet in config")
    if value is None:
        return _unavailable(name, True, unavailable_reason, threshold)
    return _r(
        name,
        Outcome.PASS if value else Outcome.FAIL,
        True,
        value,
        threshold,
        "" if value else fail_detail,
    )


def filter_mint_authority(report: Optional[RugcheckReport]) -> FilterResult:
    value = report.mint_authority_renounced if report else None
    reason = (report.error if report and report.error else "rugcheck en RPC gaven geen mint-authority")
    return _bool_hard_filter(
        "mint_authority_renounced",
        value,
        config.REQUIRE_MINT_AUTHORITY_RENOUNCED,
        "moet renounced zijn",
        reason,
        "mint authority nog actief — dev kan onbeperkt bijdrukken",
    )


def filter_freeze_authority(report: Optional[RugcheckReport]) -> FilterResult:
    value = report.freeze_authority_renounced if report else None
    reason = (
        report.error if report and report.error else "rugcheck en RPC gaven geen freeze-authority"
    )
    return _bool_hard_filter(
        "freeze_authority_renounced",
        value,
        config.REQUIRE_FREEZE_AUTHORITY_RENOUNCED,
        "moet renounced zijn",
        reason,
        "freeze authority nog actief — je positie kan bevroren worden",
    )


def filter_lp_locked(report: Optional[RugcheckReport]) -> FilterResult:
    name = "lp_locked_or_burned"
    threshold = f">= {config.MIN_LP_LOCKED_PCT}% locked/burned"
    if not config.REQUIRE_LP_LOCKED_OR_BURNED:
        return _r(name, Outcome.SKIPPED, True, None, threshold, "uitgezet in config")
    if report is None or report.lp_locked_or_burned is None:
        reason = (report.error if report and report.error else "geen LP-lock-data beschikbaar")
        return _unavailable(name, True, reason, threshold)
    pct = report.lp_locked_pct
    ok = bool(report.lp_locked_or_burned)
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        True,
        round(pct, 2) if pct is not None else None,
        threshold,
        "" if ok else "LP niet (voldoende) vergrendeld of geburnd — dev kan de pool trekken",
    )


def filter_honeypot(report: Optional[RugcheckReport]) -> FilterResult:
    name = "honeypot_check"
    threshold = "verkoop moet mogelijk zijn"
    if not config.REQUIRE_HONEYPOT_PASS:
        return _r(name, Outcome.SKIPPED, True, None, threshold, "uitgezet in config")
    if report is None or report.honeypot_ok is None:
        reason = report.error if report and report.error else "geen honeypot-oordeel beschikbaar"
        return _unavailable(name, True, reason, threshold)

    markers = [
        str(r.get("name"))
        for r in (report.risks or [])
        if isinstance(r, dict)
        and any(m in f"{r.get('name','')} {r.get('description','')}".lower() for m in
                ("honeypot", "transfer", "blacklist", "delegate"))
    ]
    ok = bool(report.honeypot_ok)
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        True,
        ok,
        threshold,
        "" if ok else f"verkoop-blokkerende risico's: {', '.join(markers) or 'rugged'}",
    )


# =========================================================================== #
# ZACHTE FILTERS (plan §3.3)
# =========================================================================== #


def compute_activity(pair: Optional[PairData]) -> dict[str, Any]:
    """Handelsdrukte — vervangt de oude bot_score (plan §7.1).

    De oude score probeerde te raden WIE er handelde. Dat lukte niet: hij
    faalde 0 keer in 773 gevallen en correleerde niet met het resultaat.

    De nuttige vraag blijkt HOE HARD er gehandeld wordt. Drie maten, alle drie
    significant in de meting van 21-08-2026, alle drie direct af te leiden uit
    DexScreener-data. Lager volume/marketcap, minder transacties per minuut en
    een grotere gemiddelde trade horen bij de winnaars.
    """
    out: dict[str, Any] = {
        "vol_mc": None,
        "avg_trade_eur": None,
        "tx_per_min": None,
        "tx24": None,
        "reason": "",
    }
    if pair is None:
        out["reason"] = "geen pairdata"
        return out

    buys = pair.buys_h24 or 0
    sells = pair.sells_h24 or 0
    tx24 = buys + sells
    out["tx24"] = tx24

    mc = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    if pair.volume_h24_usd is not None and mc:
        out["vol_mc"] = round(pair.volume_h24_usd / mc, 4)

    if tx24 < config.MIN_TX_FOR_ACTIVITY:
        out["reason"] = f"te weinig transacties ({tx24} < {config.MIN_TX_FOR_ACTIVITY})"
        return out

    if pair.volume_h24_usd is not None:
        avg_usd = pair.volume_h24_usd / tx24
        out["avg_trade_eur"] = round(data_sources.usd_to_eur(avg_usd) or 0.0, 2)

    age = pair.age_minutes
    if age and age >= 1.0:
        # Bij coins ouder dan een dag meten we tegen 24 uur, niet tegen de
        # volledige leeftijd — de transactietelling gaat immers over 24 uur.
        window = min(age, 1440.0)
        out["tx_per_min"] = round(tx24 / window, 2)

    return out


def filter_vol_mc_ratio(pair: Optional[PairData]) -> FilterResult:
    name = "vol_mc_ratio"
    threshold = f"<= {config.MAX_VOL_MC_SOFT}"
    a = compute_activity(pair)
    value = a["vol_mc"]
    if value is None:
        return _unavailable(name, False, a["reason"] or "volume of marketcap leeg", threshold)
    ok = value <= config.MAX_VOL_MC_SOFT
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        value,
        threshold,
        "" if ok else "volume pompt de marketcap meermaals per dag rond — pomp draait al",
    )


def filter_avg_trade_eur(pair: Optional[PairData]) -> FilterResult:
    name = "avg_trade_eur"
    threshold = f">= {config.MIN_AVG_TRADE_EUR:,.0f} EUR"
    a = compute_activity(pair)
    value = a["avg_trade_eur"]
    if value is None:
        return _unavailable(name, False, a["reason"] or "geen volume/transactiedata", threshold)
    ok = value >= config.MIN_AVG_TRADE_EUR
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        value,
        threshold,
        f"{a['tx24']} transacties in 24u" + ("" if ok else " — allemaal klein geld"),
    )


def filter_tx_per_min(pair: Optional[PairData]) -> FilterResult:
    name = "tx_per_min"
    threshold = f"<= {config.MAX_TX_PER_MIN}"
    a = compute_activity(pair)
    value = a["tx_per_min"]
    if value is None:
        return _unavailable(name, False, a["reason"] or "leeftijd of transacties onbekend", threshold)
    ok = value <= config.MAX_TX_PER_MIN
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        value,
        threshold,
        "" if ok else "maalstroom van kleine trades",
    )


def filter_holder_concentration(report: Optional[RugcheckReport]) -> FilterResult:
    name = "holder_concentration"
    threshold = (
        f"top10 <= {config.MAX_TOP10_HOLDER_PCT}%, grootste <= {config.MAX_SINGLE_HOLDER_PCT}%"
    )
    if report is None or report.top_holders_pct is None:
        reason = report.error if report and report.error else "geen holder-data"
        return _unavailable(name, False, reason, threshold)

    top10 = report.top_holders_pct
    largest = report.largest_holder_pct
    raw = {
        "top10_pct": round(top10, 2),
        "largest_pct": round(largest, 2) if largest is not None else None,
    }
    problems = []
    if top10 > config.MAX_TOP10_HOLDER_PCT:
        problems.append(f"top10 {top10:.1f}% te hoog")
    if largest is not None and largest > config.MAX_SINGLE_HOLDER_PCT:
        problems.append(f"grootste holder {largest:.1f}% te hoog")

    ok = not problems
    return _r(name, Outcome.PASS if ok else Outcome.FAIL, False, raw, threshold, "; ".join(problems))


def compute_holder_growth(
    token_address: str,
    total_holders: Optional[int],
    pair_age_minutes: Optional[float],
    history: dict[str, Any],
    now: Optional[float] = None,
) -> tuple[Optional[float], dict[str, Any]]:
    """Holders per minuut.

    Eerste keer dat we een token zien: holders gedeeld door pair-leeftijd
    (gemiddelde groei sinds launch). Zien we hem opnieuw, dan meten we de
    groei TUSSEN twee observaties — dat is scherper.
    """
    now = now if now is not None else time.time()
    if total_holders is None:
        return None, {"reason": "aantal holders onbekend"}

    previous = history.get(token_address)
    detail: dict[str, Any] = {"holders": total_holders}

    if isinstance(previous, dict):
        prev_holders = previous.get("holders")
        prev_ts = previous.get("ts")
        if isinstance(prev_holders, (int, float)) and isinstance(prev_ts, (int, float)):
            minutes = (now - prev_ts) / 60.0
            if minutes >= 1.0:
                rate = (total_holders - prev_holders) / minutes
                detail.update(
                    {
                        "mode": "delta",
                        "prev_holders": prev_holders,
                        "minutes_between": round(minutes, 1),
                    }
                )
                return max(0.0, rate), detail

    if pair_age_minutes and pair_age_minutes >= 1.0:
        rate = total_holders / pair_age_minutes
        detail.update({"mode": "since_launch", "pair_age_minutes": round(pair_age_minutes, 1)})
        return rate, detail

    return None, {"reason": "geen bruikbare tijdsbasis", **detail}


def filter_holder_growth(
    token_address: str,
    report: Optional[RugcheckReport],
    pair: Optional[PairData],
    history: dict[str, Any],
) -> FilterResult:
    name = "holder_growth_per_min"
    threshold = f"<= {config.MAX_HOLDER_GROWTH_PER_MIN} holders/min"
    total_holders = report.total_holders if report else None
    rate, detail = compute_holder_growth(
        token_address,
        total_holders,
        pair.age_minutes if pair else None,
        history,
    )
    if rate is None:
        return _unavailable(name, False, str(detail.get("reason", "onbekend")), threshold)
    ok = rate <= config.MAX_HOLDER_GROWTH_PER_MIN
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        round(rate, 2),
        threshold,
        ("airdrop-farming-signaal; " if not ok else "")
        + ", ".join(f"{k}={v}" for k, v in detail.items()),
    )


def filter_deployer_reputation(rep: Optional[DeployerReputation]) -> FilterResult:
    name = "deployer_reputation"
    threshold = (
        f"dead_ratio <= {config.MAX_DEPLOYER_DEAD_RATIO}, "
        f"previous_deploys <= {config.MAX_DEPLOYER_PREVIOUS_DEPLOYS}"
    )
    if rep is None or not rep.available:
        reason = rep.error if rep and rep.error else "geen deployer-historie"
        return _unavailable(name, False, reason, threshold)

    raw = {
        "wallet": rep.wallet,
        "previous_deploys": rep.previous_deploys,
        "dead_deploys": rep.dead_deploys,
        "dead_ratio": round(rep.dead_ratio, 3) if rep.dead_ratio is not None else None,
    }
    problems = []
    if rep.dead_ratio is not None and (rep.previous_deploys or 0) > 0:
        if rep.dead_ratio > config.MAX_DEPLOYER_DEAD_RATIO:
            problems.append(f"{rep.dead_deploys}/{rep.previous_deploys} eerdere deploys naar nul")
    if (rep.previous_deploys or 0) > config.MAX_DEPLOYER_PREVIOUS_DEPLOYS:
        problems.append(f"{rep.previous_deploys} eerdere launches (serial deployer)")

    ok = not problems
    return _r(name, Outcome.PASS if ok else Outcome.FAIL, False, raw, threshold, "; ".join(problems))


def filter_social_account_age(social: Optional[dict[str, Any]]) -> FilterResult:
    name = "social_account_age_days"
    threshold = f">= {config.MIN_SOCIAL_ACCOUNT_AGE_DAYS} dagen"
    if not social or not social.get("available"):
        reason = (social or {}).get("error", "geen social-data")
        return _unavailable(name, False, str(reason), threshold)
    age = float(social.get("age_days") or 0.0)
    ok = age >= config.MIN_SOCIAL_ACCOUNT_AGE_DAYS
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        round(age, 1),
        threshold,
        f"@{social.get('handle')}" + ("" if ok else " — vers account"),
    )


def filter_narrative(check: Optional[NarrativeCheck]) -> FilterResult:
    name = "narrative_score"
    threshold = f">= {config.CLAUDE_MIN_NARRATIVE_SCORE}"
    if not config.CLAUDE_META_CHECK_ENABLED:
        return _r(name, Outcome.SKIPPED, False, None, threshold, "narratief-check uitgezet")
    if check is None or not check.available or check.score is None:
        reason = (check.error if check and check.error else "geen narratief-oordeel")
        return _unavailable(name, False, reason, threshold)
    ok = check.score >= config.CLAUDE_MIN_NARRATIVE_SCORE
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        round(check.score, 1),
        threshold,
        (check.verdict or "")[:160],
    )


# =========================================================================== #
# Zachte score
# =========================================================================== #


def _soft_component_score(result: FilterResult) -> float:
    """Vertaalt één zachte filter naar 0-100 (hoger = beter)."""
    if result.outcome is Outcome.DATA_UNAVAILABLE:
        return config.SOFT_UNKNOWN_SCORE
    if result.outcome is Outcome.SKIPPED:
        return config.SOFT_UNKNOWN_SCORE
    if result.outcome is Outcome.FAIL:
        return 0.0

    # PASS: geef een graduele score waar dat zinnig is, zodat 'net binnen de
    # drempel' niet hetzelfde scoort als 'ruim binnen de drempel'.
    name, value = result.name, result.raw_value
    try:
        if name == "vol_mc_ratio" and isinstance(value, (int, float)):
            # 0 -> 100 punten, bij de drempel 40, daarboven aflopend naar 0.
            ratio = float(value) / max(config.MAX_VOL_MC_SOFT, 1e-9)
            return max(0.0, 100.0 - ratio * 60.0)
        if name == "avg_trade_eur" and isinstance(value, (int, float)):
            # EUR 35 -> 50 punten, EUR 100 -> 100 punten.
            return max(0.0, min(100.0, float(value) / max(config.MIN_AVG_TRADE_EUR, 1e-9) * 50.0))
        if name == "tx_per_min" and isinstance(value, (int, float)):
            ratio = float(value) / max(config.MAX_TX_PER_MIN, 1e-9)
            return max(0.0, 100.0 - ratio * 60.0)
        if name == "holder_concentration" and isinstance(value, dict):
            top10 = float(value.get("top10_pct") or 0.0)
            return max(0.0, 100.0 - (top10 / max(config.MAX_TOP10_HOLDER_PCT, 1e-9)) * 60.0)
        if name == "holder_growth_per_min" and isinstance(value, (int, float)):
            ratio = float(value) / max(config.MAX_HOLDER_GROWTH_PER_MIN, 1e-9)
            return max(0.0, 100.0 - ratio * 50.0)
        if name == "deployer_reputation" and isinstance(value, dict):
            dead_ratio = value.get("dead_ratio")
            previous = value.get("previous_deploys") or 0
            if previous == 0:
                return 70.0  # eerste launch: neutraal-positief, niet bewezen
            return max(0.0, 100.0 - float(dead_ratio or 0.0) * 100.0)
        if name == "social_account_age_days" and isinstance(value, (int, float)):
            return min(100.0, 50.0 + float(value) / 2.0)
        if name == "narrative_score" and isinstance(value, (int, float)):
            return float(value)
    except (TypeError, ValueError):
        pass
    return 80.0


SOFT_WEIGHT_KEYS = {
    "vol_mc_ratio": "vol_mc_ratio",
    "avg_trade_eur": "avg_trade_eur",
    "tx_per_min": "tx_per_min",
    "holder_concentration": "holder_concentration",
    "holder_growth_per_min": "holder_growth",
    "deployer_reputation": "deployer_reputation",
    "social_account_age_days": "social_account_age",
    "narrative_score": "narrative",
}


def compute_soft_score(results: list[FilterResult]) -> tuple[float, dict[str, float]]:
    total_weight = 0.0
    weighted = 0.0
    breakdown: dict[str, float] = {}
    for result in results:
        if result.hard:
            continue
        weight_key = SOFT_WEIGHT_KEYS.get(result.name)
        if weight_key is None:
            continue
        weight = float(config.SOFT_WEIGHTS.get(weight_key, 0.0))
        if weight <= 0:
            continue
        component = _soft_component_score(result)
        breakdown[result.name] = round(component, 1)
        weighted += component * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0, breakdown
    return round(weighted / total_weight, 1), breakdown


# =========================================================================== #
# Orchestratie van de filters
# =========================================================================== #


def evaluate(
    token_address: str,
    pair: Optional[PairData],
    report: Optional[RugcheckReport],
    deployer: Optional[DeployerReputation],
    social: Optional[dict[str, Any]] = None,
    narrative: Optional[NarrativeCheck] = None,
    holder_history: Optional[dict[str, Any]] = None,
) -> Evaluation:
    """Draait alle filters en bouwt de complete Evaluation."""
    holder_history = holder_history if holder_history is not None else {}

    evaluation = Evaluation(
        token_address=token_address,
        symbol=(pair.symbol if pair else ""),
        name=(pair.name if pair else ""),
        pair=pair,
        rugcheck=report,
        deployer=deployer,
        narrative=narrative,
    )

    evaluation.results = [
        # harde markt-filters
        filter_marketcap(pair),
        filter_liquidity(pair, report),
        filter_liq_mc_ratio(pair, report),
        filter_volume_spike(pair, report),
        # harde rug-vectoren
        filter_mint_authority(report),
        filter_freeze_authority(report),
        filter_lp_locked(report),
        filter_honeypot(report),
        # zachte signalen — handelsdrukte
        filter_vol_mc_ratio(pair),
        filter_avg_trade_eur(pair),
        filter_tx_per_min(pair),
        # zachte signalen — overig
        filter_holder_concentration(report),
        filter_holder_growth(token_address, report, pair, holder_history),
        filter_deployer_reputation(deployer),
        filter_social_account_age(social),
        filter_narrative(narrative),
    ]

    evaluation.soft_score, _ = compute_soft_score(evaluation.results)
    evaluation.shadow_sets = evaluate_shadow_sets(evaluation)
    # Verandering t.o.v. de vorige keer dat we deze munt zagen. Voorlopig
    # alleen loggen, niet filteren: een nieuw signaal beoordeel je op verse
    # data, niet op data waarop je het bedacht hebt.
    evaluation.deltas = compute_deltas(
        holder_history, token_address, metrics_for_history(evaluation)
    )
    return evaluation


# =========================================================================== #
# Schaduw-configuraties (plan §7.4)
# =========================================================================== #

#: Filters die voor ALLE sets gelijk zijn. Wat hier faalt, faalt overal.
UNIVERSAL_HARD_FILTERS = [
    "liquidity_eur",
    "liq_mc_ratio",
    "volume_spike",
    "mint_authority_renounced",
    "freeze_authority_renounced",
    "lp_locked_or_burned",
    "honeypot_check",
]


def _raw_number(evaluation: Evaluation, name: str) -> Optional[float]:
    result = evaluation.by_name(name)
    if result is None or not isinstance(result.raw_value, (int, float)):
        return None
    return float(result.raw_value)


def universal_blockers(evaluation: Evaluation) -> list[str]:
    """Set-onafhankelijke redenen om nooit te alarmeren."""
    out = []
    for name in UNIVERSAL_HARD_FILTERS:
        result = evaluation.by_name(name)
        if result is not None and result.outcome.is_blocking:
            out.append(f"{name}={result.outcome.value}")
    return out


def set_would_alert(evaluation: Evaluation, set_name: str) -> tuple[bool, str]:
    """Zou deze coin een alert opleveren onder drempelset `set_name`?

    Gebruikt de al gemeten ruwe waarden, zodat elke set exact dezelfde meting
    beoordeelt en het verschil puur in de drempels zit.
    """
    settings = config.SHADOW_SETS.get(set_name)
    if settings is None:
        return False, f"onbekende set {set_name}"

    blockers = universal_blockers(evaluation)
    if blockers:
        return False, "universeel: " + ", ".join(blockers)

    mc = _raw_number(evaluation, "marketcap_eur")
    if mc is None:
        return False, "marketcap onbekend"
    if mc < settings["min_marketcap_eur"]:
        return False, f"marketcap {mc:,.0f} onder {settings['min_marketcap_eur']:,.0f}"
    if mc > settings["max_marketcap_eur"]:
        return False, f"marketcap {mc:,.0f} boven {settings['max_marketcap_eur']:,.0f}"

    vol_mc = _raw_number(evaluation, "vol_mc_ratio")
    if settings["max_vol_mc"] is not None:
        if vol_mc is None:
            return False, "vol/mc onbekend"
        if vol_mc > settings["max_vol_mc"]:
            return False, f"vol/mc {vol_mc} boven {settings['max_vol_mc']}"

    if settings["min_avg_trade_eur"] is not None:
        avg = _raw_number(evaluation, "avg_trade_eur")
        if avg is None:
            return False, "gemiddelde trade onbekend"
        if avg < settings["min_avg_trade_eur"]:
            return False, f"gemiddelde trade {avg} onder {settings['min_avg_trade_eur']}"

    if settings["max_tx_per_min"] is not None:
        tx = _raw_number(evaluation, "tx_per_min")
        if tx is None:
            return False, "transactietempo onbekend"
        if tx > settings["max_tx_per_min"]:
            return False, f"tx/min {tx} boven {settings['max_tx_per_min']}"

    if evaluation.soft_score is None:
        return False, "zachte score niet berekend"
    if evaluation.soft_score < config.MIN_SOFT_SCORE_TO_ALERT:
        return False, f"zachte score {evaluation.soft_score} onder {config.MIN_SOFT_SCORE_TO_ALERT}"

    return True, ""


def evaluate_shadow_sets(evaluation: Evaluation) -> dict[str, bool]:
    """Per set: zou hij gealarmeerd hebben? Wordt in het logboek vastgelegd."""
    out = {}
    for set_name in config.SHADOW_SETS:
        ok, _reason = set_would_alert(evaluation, set_name)
        out[set_name] = ok
    return out


def should_alert(evaluation: Evaluation) -> tuple[bool, str]:
    """Definitief oordeel: mag deze coin een mail triggeren?

    Draait op de ACTIEVE set. De andere sets lopen mee in het logboek maar
    mailen niet. Harde filters gaan altijd voor: geen enkele coin die op een
    rug-vector faalt mag ooit een mail triggeren, ongeacht de rest (plan §3.2).
    """
    return set_would_alert(evaluation, config.ACTIVE_SET)


# =========================================================================== #
# Holder-history bijwerken (voor de volgende run)
# =========================================================================== #


#: Waarden waarvan we de VERANDERING tussen twee scans bijhouden (plan §7.2).
#: Dezelfde munt komt tot 29 keer voorbij; elke vorige waarneming weggooien is
#: de grootste ongebruikte bron in het systeem. Liquiditeit die tussen twee
#: scans zakt is een LP die leegloopt; een tradegrootte die omhoog schiet is
#: groot geld dat instapt. Dat zie je in een momentopname nooit.
DELTA_FIELDS = ("liquidity_eur", "marketcap_eur", "vol_mc_ratio", "avg_trade_eur", "tx_per_min")


def compute_deltas(
    history: dict[str, Any], token_address: str, huidig: dict[str, Optional[float]],
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Procentuele verandering sinds de vorige waarneming van deze munt.

    Eerste keer dat we een munt zien is er niets om mee te vergelijken; dan
    blijft alles leeg. Dat is geen fout, dat is de eerste meting.
    """
    now = now if now is not None else time.time()
    vorige = history.get(token_address)
    if not isinstance(vorige, dict):
        return {}

    vorige_ts = vorige.get("ts")
    if not isinstance(vorige_ts, (int, float)):
        return {}
    minuten = (now - vorige_ts) / 60.0
    if minuten < 1.0:
        return {}

    out: dict[str, Any] = {"minutes_since_prev": round(minuten, 1)}
    for veld in DELTA_FIELDS:
        oud = vorige.get(veld)
        nieuw = huidig.get(veld)
        if isinstance(oud, (int, float)) and isinstance(nieuw, (int, float)) and oud > 0:
            out[f"{veld}_delta_pct"] = round((nieuw / oud - 1.0) * 100.0, 2)
    return out


def record_holder_observation(
    history: dict[str, Any],
    token_address: str,
    total_holders: Optional[int],
    metrics: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, Any]:
    """Bewaart deze waarneming zodat de volgende run het verschil kan zien."""
    entry: dict[str, Any] = {"ts": time.time()}
    if total_holders is not None:
        entry["holders"] = int(total_holders)
    elif isinstance(history.get(token_address), dict):
        vorige_holders = history[token_address].get("holders")
        if vorige_holders is not None:
            entry["holders"] = vorige_holders

    for veld, waarde in (metrics or {}).items():
        if isinstance(waarde, (int, float)):
            entry[veld] = waarde

    if len(entry) == 1:  # alleen een tijdstempel: niets zinnigs te bewaren
        return history
    history[token_address] = entry
    return history


def metrics_for_history(evaluation: Evaluation) -> dict[str, Optional[float]]:
    """De waarden waarvan we de verandering willen volgen."""
    return {veld: _raw_number(evaluation, veld) for veld in DELTA_FIELDS}


def load_holder_history() -> dict[str, Any]:
    data = state_store.load(config.HOLDER_HISTORY_PATH)
    return state_store.prune(data, "ts", config.DEDUP_RETENTION_DAYS * 86400.0)


def save_holder_history(history: dict[str, Any]) -> None:
    state_store.save(config.HOLDER_HISTORY_PATH, history)
