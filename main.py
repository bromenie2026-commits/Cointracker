"""
main.py — orchestratie: scan -> filter -> log -> notify.

Draai:
    python main.py                 # normale run
    python main.py --dry-run       # geen mail, geen state-wijziging
    python main.py --limit 5       # minder kandidaten (handig bij testen)
    python main.py --token <mint>  # één specifiek contractadres doormeten

Dit programma kan niet handelen. Er is geen wallet, geen private key en geen
order-code aanwezig; config.TRADING_ENABLED is hard op False en wordt bij
elke start geverifieerd.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from typing import Optional

import claude_meta_check
import config
import csv_log
import data_sources
import deployer_reputation
import dedup as dedup_module
import filters
import http_client
import monitor
import notify
import raw_store
import rugcheck
import state_store
from models import Evaluation, PairData

log = logging.getLogger("main")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def assert_no_trading() -> None:
    """Expliciete, testbare veiligheidsassertie (plan §9)."""
    if config.TRADING_ENABLED:
        raise SystemExit(
            "TRADING_ENABLED staat op True. Dit systeem bevat geen trading-code; "
            "deze vlag hoort altijd False te zijn. Afgebroken."
        )


def analyze_token(pair: Optional[PairData], token_address: str, holder_history: dict) -> Evaluation:
    """Haalt alle data op voor één token en draait de filters."""
    # Het pool-adres meegeven zodat het niet als 'grootste houder' meetelt
    # (bugfix 4.1 — dit filter wees 20 van de 24 best presterende coins af).
    pair_addresses = [pair.pair_address] if pair and pair.pair_address else None
    report = rugcheck.check_token(token_address, pair_addresses=pair_addresses)

    # Deployer-historie is de duurste call (veel RPC). Alleen doen als de
    # coin de harde rug-vectoren überhaupt haalt — anders is het weggegooide
    # rate limit. De filter geeft dan netjes DATA_UNAVAILABLE.
    preliminary_hard_ok = all(
        f.outcome.value in ("pass", "skipped")
        for f in (
            filters.filter_mint_authority(report),
            filters.filter_freeze_authority(report),
            filters.filter_lp_locked(report),
            filters.filter_honeypot(report),
            filters.filter_marketcap(pair),
        )
    )

    deployer = None
    if preliminary_hard_ok:
        deployer = deployer_reputation.get_reputation(token_address, known_creator=report.creator)
    else:
        from models import DeployerReputation

        deployer = DeployerReputation(
            wallet=report.creator,
            available=False,
            error="niet opgehaald: coin viel al af op een harde filter",
        )

    social = None
    handle = pair.twitter_handle() if pair else None
    if handle:
        social = data_sources.get_social_account_age_days(handle)
    else:
        social = {"available": False, "error": "geen X-account gekoppeld aan de pair"}

    narrative = None
    if preliminary_hard_ok:
        narrative = claude_meta_check.check(pair, token_address)

    evaluation = filters.evaluate(
        token_address=token_address,
        pair=pair,
        report=report,
        deployer=deployer,
        social=social,
        narrative=narrative,
        holder_history=holder_history,
    )
    # Deze waarneming bewaren zodat de VOLGENDE run het verschil kan zien.
    # Dit gebeurt ná evaluate(), want die vergelijkt nog met de vorige stand.
    filters.record_holder_observation(
        holder_history,
        token_address,
        report.total_holders if report else None,
        filters.metrics_for_history(evaluation),
    )
    return evaluation


def run(
    limit: Optional[int] = None,
    dry_run: bool = False,
    single_token: Optional[str] = None,
    no_email: bool = False,
) -> int:
    assert_no_trading()
    http_client.reset_counters()

    # Faalt luid als de narratief-check aanstaat zonder API-key.
    claude_meta_check.ensure_configured()

    if not (dry_run or no_email):
        # Vroeg falen is beter dan pas falen als er iets te melden valt.
        notify.ensure_configured()

    scan_id = uuid.uuid4().hex[:12]
    started = time.time()
    log.info("Scan %s gestart (dry_run=%s)", scan_id, dry_run)

    # ---------------- kandidaten ophalen ---------------- #
    if single_token:
        pairs = data_sources.get_pairs_for_token(single_token)
        best = data_sources.best_pair(pairs)
        candidates = [best] if best else []
        if not candidates:
            log.warning("Geen pair gevonden voor %s — token wordt toch doorgemeten", single_token)
            candidates = [None]
        token_addresses = [single_token]
    else:
        candidates = data_sources.discover_candidates(limit=limit)
        token_addresses = [p.token_address for p in candidates]

    if not candidates:
        log.warning("Geen kandidaten gevonden. Rate limits: %s", http_client.rate_limit_summary())
        return 0

    # ---------------- state ---------------- #
    holder_history = filters.load_holder_history()
    store = dedup_module.DedupStore()
    risk = notify.load_risk_config()

    rows = []
    alerts_sent = 0

    for index, pair in enumerate(candidates, start=1):
        token_address = pair.token_address if pair else token_addresses[index - 1]
        label = f"{index}/{len(candidates)} {pair.symbol if pair else ''} {token_address}"
        log.info("Beoordelen %s", label)

        try:
            evaluation = analyze_token(pair, token_address, holder_history)
        except Exception as exc:  # noqa: BLE001 — één kapotte coin stopt de run niet
            log.exception("Fout bij %s: %s", token_address, exc)
            continue

        ok, reason = filters.should_alert(evaluation)

        if ok:
            allowed, dedup_reason = store.should_alert(token_address)
            if not allowed:
                ok = False
                reason = dedup_reason
            elif alerts_sent >= config.MAX_ALERTS_PER_RUN:
                ok = False
                reason = f"max {config.MAX_ALERTS_PER_RUN} alerts per run bereikt"

        if ok and not (dry_run or no_email):
            sent = notify.send_alert(evaluation, risk)
            if sent:
                evaluation.alerted = True
                store.record_alert(token_address, evaluation.symbol)
                alerts_sent += 1
            else:
                evaluation.alert_suppressed_reason = "mail versturen mislukt"
        elif ok:
            evaluation.alerted = False
            evaluation.alert_suppressed_reason = "dry-run: mail overgeslagen (zou alerten)"
            log.info("ZOU ALERTEN: %s (score %s)", token_address, evaluation.soft_score)
        else:
            evaluation.alert_suppressed_reason = reason

        log.info(
            "  hard_pass=%s soft_score=%s alerted=%s %s",
            evaluation.hard_pass,
            evaluation.soft_score,
            evaluation.alerted,
            evaluation.alert_suppressed_reason,
        )
        row = csv_log.build_row(evaluation, scan_id)
        rows.append(row)

        # Volledige API-antwoorden bewaren, zodat je later hypotheses kunt
        # toetsen op de data van vandaag (plan §7.5). Gaat als artifact naar
        # buiten, niet de git-geschiedenis in.
        raw_store.save(
            scan_id,
            row.get("row_id", ""),
            token_address,
            {
                "dexscreener_pair": (evaluation.pair.raw if evaluation.pair else None),
                "rugcheck": (evaluation.rugcheck.raw if evaluation.rugcheck else None),
            },
        )

    # ---------------- positie-monitor ---------------- #
    # Bewaakt de munten die je écht gekocht hebt tegen je eigen risk_config.
    # Handelt niets af; stuurt alleen een herinnering (plan §8.3).
    if not (dry_run or no_email):
        try:
            for trigger in monitor.check_positions(risk):
                onderwerp, tekst = monitor.format_trigger(trigger)
                notify.send_run_summary(onderwerp, tekst)
                log.info("Positie-melding verstuurd: %s", onderwerp)
        except Exception as exc:  # noqa: BLE001 — mag de run nooit slopen
            log.exception("Positie-monitor faalde: %s", exc)

    # ---------------- wegschrijven ---------------- #
    written = csv_log.append_rows(rows)
    if not dry_run:
        store.save()
        filters.save_holder_history(holder_history)
        raw_store.prune()

    duration = time.time() - started
    log.info(
        "Scan %s klaar in %.1fs — %d gelogd, %d gealerteerd. %s",
        scan_id,
        duration,
        written,
        alerts_sent,
        http_client.rate_limit_summary(),
    )

    total_rate_limits = sum(http_client.RATE_LIMIT_HITS.values())
    if total_rate_limits >= 5:
        log.warning(
            "%d rate-limit-hits deze run — overweeg de scanfrequentie te verlagen.",
            total_rate_limits,
        )

    check_health(rows, alerts_sent, total_rate_limits, send=not (dry_run or no_email))
    return alerts_sent


# --------------------------------------------------------------------------- #
# Gezondheidsalarm (plan §8.4)
# --------------------------------------------------------------------------- #


def health_problems(
    rows: list[dict[str, str]], alerts_sent: int, rate_limit_hits: int
) -> list[str]:
    """Signaleert dat de bot zelf gek doet, niet dat de markt gek doet."""
    problemen: list[str] = []

    if alerts_sent > config.HEALTH_MAX_ALERTS_PER_RUN:
        problemen.append(
            f"{alerts_sent} alerts in één run (grens {config.HEALTH_MAX_ALERTS_PER_RUN}) "
            "— controleer of een filter stuk is voor je hierop handelt."
        )

    if rows:
        harde = [f"{n}__outcome" for n in filters.HARD_FILTER_NAMES]
        totaal = len(rows) * len(harde)
        onbekend = sum(1 for r in rows for k in harde if r.get(k) == "data_unavailable")
        ratio = onbekend / totaal if totaal else 0.0
        if ratio > config.HEALTH_MAX_UNAVAILABLE_RATIO:
            problemen.append(
                f"{ratio*100:.0f}% van de harde filters kreeg geen data "
                f"(grens {config.HEALTH_MAX_UNAVAILABLE_RATIO*100:.0f}%) — waarschijnlijk "
                "is rugcheck of de RPC onbereikbaar. Fail-closed betekent dat je nu "
                "vooral kansen mist, niet dat je risico loopt."
            )

    if rate_limit_hits > config.HEALTH_MAX_RATE_LIMIT_HITS:
        problemen.append(
            f"{rate_limit_hits} rate-limit-hits deze run — zet de scanfrequentie omlaag "
            "of gebruik een eigen RPC-sleutel."
        )

    if not rows:
        problemen.append("Geen enkele coin beoordeeld — de kandidatenbron levert niets op.")

    return problemen


def check_health(
    rows: list[dict[str, str]], alerts_sent: int, rate_limit_hits: int, send: bool = True
) -> list[str]:
    problemen = health_problems(rows, alerts_sent, rate_limit_hits)
    if not problemen:
        return []

    for probleem in problemen:
        log.warning("GEZONDHEID: %s", probleem)

    if not (send and config.HEALTH_ALARM_ENABLED):
        return problemen

    # Niet vaker dan eens per zoveel uur mailen, anders spam je jezelf met
    # hetzelfde probleem.
    state = state_store.load(config.HEALTH_STATE_PATH)
    laatste = state.get("last_alarm_ts")
    if isinstance(laatste, (int, float)) and time.time() - laatste < config.HEALTH_ALARM_COOLDOWN_HOURS * 3600:
        log.info("Gezondheidsalarm onderdrukt (cooldown actief).")
        return problemen

    body = (
        "De bot lijkt zelf iets te mankeren:\n\n"
        + "\n".join(f"- {p}" for p in problemen)
        + "\n\nDit gaat over het meetinstrument, niet over een munt. "
        "Er is geen actie nodig met je posities."
    )
    if notify.send_run_summary("Waarschuwing: controleer de bot", body):
        state["last_alarm_ts"] = time.time()
        state_store.save(config.HEALTH_STATE_PATH, state)
    return problemen


def main() -> None:
    parser = argparse.ArgumentParser(description="Solana memecoin alert bot (scan-only)")
    parser.add_argument("--limit", type=int, default=None, help="max aantal kandidaten")
    parser.add_argument("--dry-run", action="store_true", help="niets mailen, geen state opslaan")
    parser.add_argument("--no-email", action="store_true", help="wel state opslaan, niet mailen")
    parser.add_argument("--token", type=str, default=None, help="één specifiek contractadres")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    try:
        run(
            limit=args.limit,
            dry_run=args.dry_run,
            single_token=args.token,
            no_email=args.no_email,
        )
    except claude_meta_check.ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
    except notify.NotifyError as exc:
        log.error("%s", exc)
        raise SystemExit(3) from exc


if __name__ == "__main__":
    main()
