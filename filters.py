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
    "bot_score",
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


def filter_liquidity(pair: Optional[PairData]) -> FilterResult:
    name = "liquidity_eur"
    threshold = f">= {config.MIN_LIQUIDITY_EUR:,.0f} EUR"
    if pair is None or pair.liquidity_usd is None:
        return _unavailable(name, True, "geen liquiditeitsdata", threshold)
    liq_eur = data_sources.usd_to_eur(pair.liquidity_usd) or 0.0
    ok = liq_eur >= config.MIN_LIQUIDITY_EUR
    return _r(name, Outcome.PASS if ok else Outcome.FAIL, True, round(liq_eur, 2), threshold)


def filter_liq_mc_ratio(pair: Optional[PairData]) -> FilterResult:
    name = "liq_mc_ratio"
    threshold = f"{config.MIN_LIQ_MC_RATIO} - {config.MAX_LIQ_MC_RATIO}"
    if pair is None:
        return _unavailable(name, True, "geen pairdata", threshold)

    mc = pair.market_cap_usd if pair.market_cap_usd is not None else pair.fdv_usd
    liq = pair.liquidity_usd
    if mc is None or liq is None or mc <= 0:
        return _unavailable(name, True, "marketcap of liquiditeit leeg/0", threshold)

    ratio = liq / mc
    ok = config.MIN_LIQ_MC_RATIO <= ratio <= config.MAX_LIQ_MC_RATIO
    detail = ""
    if not ok:
        detail = "te dun (niet uit te stappen)" if ratio < config.MIN_LIQ_MC_RATIO else "onnatuurlijk hoog"
    return _r(name, Outcome.PASS if ok else Outcome.FAIL, True, round(ratio, 4), threshold, detail)


def filter_volume_spike(pair: Optional[PairData]) -> FilterResult:
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
    vol1h_liq = None
    if pair.volume_h1_usd is not None and pair.liquidity_usd:
        vol1h_liq = pair.volume_h1_usd / pair.liquidity_usd

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


def compute_bot_score(pair: Optional[PairData]) -> tuple[Optional[float], dict[str, Any]]:
    """Bot-score 0-100 (hoger = meer bot-achtig).

    Gebouwd op de geaggregeerde transactiedata die DexScreener geeft. Het is
    expliciet een PROXY: we zien geen individuele wallets, dus we meten
    patronen die bij wash-trading en botgedrag horen:

    1. Extreme koop/verkoop-onbalans (bots kopen bij elkaar).
    2. Minuscule gemiddelde tradegrootte bij veel transacties.
    3. Volume dat vele malen de liquiditeit rondpompt.
    4. Onnatuurlijk CONSTANTE transactiesnelheid (1u-tempo == 24u-tempo);
       echte hype piekt en zakt, bots tikken door.
    """
    if pair is None:
        return None, {"reason": "geen pairdata"}

    buys = pair.buys_h24 or 0
    sells = pair.sells_h24 or 0
    total = buys + sells
    if total == 0:
        return None, {"reason": "geen transactiedata"}

    components: dict[str, Any] = {}
    score = 0.0

    # 1. onbalans
    imbalance = abs(buys - sells) / total
    components["imbalance"] = round(imbalance, 3)
    if imbalance > 0.6:
        score += min(30.0, (imbalance - 0.6) / 0.4 * 30.0)

    # 2. gemiddelde tradegrootte
    avg_trade = None
    if pair.volume_h24_usd is not None:
        avg_trade = pair.volume_h24_usd / total
        components["avg_trade_usd"] = round(avg_trade, 2)
        if total > 200 and avg_trade < 25:
            score += min(25.0, (25 - avg_trade) / 25 * 25.0)

    # 3. churn t.o.v. liquiditeit
    churn = None
    if pair.volume_h24_usd is not None and pair.liquidity_usd:
        churn = pair.volume_h24_usd / pair.liquidity_usd
        components["vol24_liq_churn"] = round(churn, 2)
        if churn > 20:
            score += min(25.0, (churn - 20) / 60 * 25.0)

    # 4. regelmaat van het tempo
    regularity = None
    if pair.buys_h1 is not None and pair.sells_h1 is not None:
        tx_h1 = pair.buys_h1 + pair.sells_h1
        if total > 300 and tx_h1 > 0:
            regularity = (tx_h1 * 24.0) / total
            components["rate_regularity"] = round(regularity, 3)
            if 0.85 <= regularity <= 1.15:
                score += 20.0

    components["score"] = round(min(100.0, score), 1)
    return components["score"], components


def filter_bot_score(pair: Optional[PairData]) -> FilterResult:
    name = "bot_score"
    threshold = f"<= {config.MAX_BOT_SCORE}"
    score, components = compute_bot_score(pair)
    if score is None:
        return _unavailable(name, False, str(components.get("reason", "onbekend")), threshold)
    ok = score <= config.MAX_BOT_SCORE
    return _r(
        name,
        Outcome.PASS if ok else Outcome.FAIL,
        False,
        score,
        threshold,
        "componenten: " + ", ".join(f"{k}={v}" for k, v in components.items() if k != "score"),
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
        if name == "bot_score" and isinstance(value, (int, float)):
            return max(0.0, 100.0 - float(value))
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
    "bot_score": "bot_score",
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
        filter_liquidity(pair),
        filter_liq_mc_ratio(pair),
        filter_volume_spike(pair),
        # harde rug-vectoren
        filter_mint_authority(report),
        filter_freeze_authority(report),
        filter_lp_locked(report),
        filter_honeypot(report),
        # zachte signalen
        filter_bot_score(pair),
        filter_holder_concentration(report),
        filter_holder_growth(token_address, report, pair, holder_history),
        filter_deployer_reputation(deployer),
        filter_social_account_age(social),
        filter_narrative(narrative),
    ]

    evaluation.soft_score, _ = compute_soft_score(evaluation.results)
    return evaluation


def should_alert(evaluation: Evaluation) -> tuple[bool, str]:
    """Definitief oordeel: mag deze coin een mail triggeren?

    Volgorde is bewust: harde filters eerst en absoluut. Geen enkele coin
    die op een rug-vector faalt mag ooit een mail triggeren, ongeacht hoe
    goed de rest scoort (plan §3.2).
    """
    if not evaluation.hard_pass:
        return False, "harde filter(s): " + ", ".join(evaluation.blocking_reasons)
    if evaluation.soft_score is None:
        return False, "zachte score niet berekend"
    if evaluation.soft_score < config.MIN_SOFT_SCORE_TO_ALERT:
        return (
            False,
            f"zachte score {evaluation.soft_score} onder drempel {config.MIN_SOFT_SCORE_TO_ALERT}",
        )
    return True, ""


# =========================================================================== #
# Holder-history bijwerken (voor de volgende run)
# =========================================================================== #


def record_holder_observation(
    history: dict[str, Any], token_address: str, total_holders: Optional[int]
) -> dict[str, Any]:
    if total_holders is None:
        return history
    history[token_address] = {"holders": int(total_holders), "ts": time.time()}
    return history


def load_holder_history() -> dict[str, Any]:
    data = state_store.load(config.HOLDER_HISTORY_PATH)
    return state_store.prune(data, "ts", config.DEDUP_RETENTION_DAYS * 86400.0)


def save_holder_history(history: dict[str, Any]) -> None:
    state_store.save(config.HOLDER_HISTORY_PATH, history)
