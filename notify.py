"""
notify.py — e-mail via Gmail SMTP.

De mail toont ALLE ruwe scores, inclusief de vier nieuwe rug-pull-vectoren,
plus de risicomanagement-checklist uit risk_config.yaml en een X-zoeklink
voor de handmatige sentiment-check. De bot beslist niets over kopen.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Optional

import config
from models import Evaluation, Outcome

log = logging.getLogger(__name__)

OUTCOME_LABEL = {
    Outcome.PASS: ("PASS", "#1a7f37"),
    Outcome.FAIL: ("FAIL", "#b42318"),
    Outcome.DATA_UNAVAILABLE: ("GEEN DATA", "#b54708"),
    Outcome.SKIPPED: ("OVERGESLAGEN", "#667085"),
}


class NotifyError(RuntimeError):
    pass


def ensure_configured() -> None:
    missing = []
    if not config.GMAIL_ADDRESS:
        missing.append("GMAIL_ADDRESS")
    if not config.GMAIL_APP_PASSWORD:
        missing.append("GMAIL_APP_PASSWORD")
    if not config.ALERT_RECIPIENT:
        missing.append("ALERT_RECIPIENT")
    if missing:
        raise NotifyError(
            "E-mail niet geconfigureerd, ontbrekende secrets: " + ", ".join(missing)
        )


def load_risk_config() -> dict[str, Any]:
    try:
        import yaml

        with config.RISK_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — risk_config mag nooit de mail blokkeren
        log.warning("risk_config.yaml niet leesbaar: %s", exc)
        return {}


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "ja" if value else "nee"
    if isinstance(value, float):
        # Bedragen leesbaar houden (120.000,00 i.p.v. 1.2e+05), kleine
        # getallen met genoeg significante cijfers.
        if abs(value) >= 1000:
            return f"{value:,.2f}"
        if abs(value) >= 0.01 or value == 0:
            return f"{value:,.4f}".rstrip("0").rstrip(".")
        return f"{value:.4g}"
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items() if v is not None)
    return str(value)


def _rows_html(evaluation: Evaluation, hard: bool) -> str:
    rows = []
    for result in evaluation.results:
        if result.hard != hard:
            continue
        label, color = OUTCOME_LABEL[result.outcome]
        detail = html.escape(result.detail or "")
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>"
            f"<code>{html.escape(result.name)}</code></td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:{color};"
            f"font-weight:600'>{label}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>"
            f"{html.escape(_fmt(result.raw_value))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#667085'>"
            f"{html.escape(result.threshold)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#667085;"
            f"font-size:12px'>{detail}</td>"
            f"</tr>"
        )
    if not rows:
        return "<tr><td colspan='5'>geen</td></tr>"
    return "".join(rows)


def _checklist_html(risk: dict[str, Any]) -> str:
    items = []
    for key in ("pre_buy_checklist", "hard_rules"):
        entries = risk.get(key) or []
        if not entries:
            continue
        items.append(f"<p style='margin:12px 0 4px;font-weight:600'>{html.escape(key)}</p><ul>")
        for entry in entries:
            items.append(f"<li>{html.escape(str(entry))}</li>")
        items.append("</ul>")

    per_trade = risk.get("per_trade") or {}
    portfolio = risk.get("portfolio") or {}
    if per_trade or portfolio:
        items.append("<p style='margin:12px 0 4px;font-weight:600'>Positieregels</p><ul>")
        for key, value in {**portfolio, **per_trade}.items():
            items.append(f"<li><code>{html.escape(str(key))}</code>: {html.escape(_fmt(value))}</li>")
        items.append("</ul>")
    return "".join(items)


def build_email_html(evaluation: Evaluation, risk: Optional[dict[str, Any]] = None) -> str:
    risk = risk if risk is not None else load_risk_config()
    pair = evaluation.pair
    narrative = evaluation.narrative

    mc = evaluation.by_name("marketcap_eur")
    liq = evaluation.by_name("liquidity_eur")

    header_bits = [
        f"Marketcap: €{_fmt(mc.raw_value) if mc else '—'}",
        f"Liquiditeit: €{_fmt(liq.raw_value) if liq else '—'}",
        f"Zachte score: {_fmt(evaluation.soft_score)}/100",
    ]
    if pair and pair.age_minutes is not None:
        header_bits.append(f"Pair-leeftijd: {pair.age_minutes/60:.1f}u")

    narrative_html = ""
    if narrative and narrative.available:
        narrative_html = (
            f"<div style='background:#f8f9fc;padding:12px;border-radius:8px;margin:16px 0'>"
            f"<strong>Narratief-check (zacht signaal, geen advies)</strong><br>"
            f"Score: {_fmt(narrative.score)}/100 — {html.escape(narrative.verdict)}<br>"
            f"<span style='color:#667085;font-size:13px'>"
            f"{html.escape(narrative.reasoning)}</span></div>"
        )

    table_style = "border-collapse:collapse;width:100%;font-size:14px;margin-bottom:18px"

    return f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
color:#101828;max-width:820px">
<h2 style="margin-bottom:4px">{html.escape(evaluation.symbol or 'onbekend')} —
{html.escape(evaluation.name or '')}</h2>
<p style="color:#667085;margin-top:0"><code>{html.escape(evaluation.token_address)}</code></p>
<p>{' &nbsp;|&nbsp; '.join(html.escape(b) for b in header_bits)}</p>

<p>
<a href="{html.escape(evaluation.dexscreener_url)}">DexScreener</a> &nbsp;|&nbsp;
<a href="{html.escape(evaluation.rugcheck_url)}">Rugcheck</a> &nbsp;|&nbsp;
<a href="{html.escape(evaluation.x_search_url)}"><strong>X-sentiment zelf checken →</strong></a>
</p>

{narrative_html}

<h3>Harde filters (alle vier de rug-vectoren moeten PASS zijn)</h3>
<table style="{table_style}">
<tr style="text-align:left;background:#f2f4f7">
<th style="padding:6px 10px">Filter</th><th style="padding:6px 10px">Uitkomst</th>
<th style="padding:6px 10px">Ruwe waarde</th><th style="padding:6px 10px">Drempel</th>
<th style="padding:6px 10px">Toelichting</th></tr>
{_rows_html(evaluation, hard=True)}
</table>

<h3>Zachte signalen</h3>
<table style="{table_style}">
<tr style="text-align:left;background:#f2f4f7">
<th style="padding:6px 10px">Signaal</th><th style="padding:6px 10px">Uitkomst</th>
<th style="padding:6px 10px">Ruwe waarde</th><th style="padding:6px 10px">Drempel</th>
<th style="padding:6px 10px">Toelichting</th></tr>
{_rows_html(evaluation, hard=False)}
</table>

<h3>Risicomanagement (referentie — de bot voert hier niets van uit)</h3>
{_checklist_html(risk)}

<hr style="margin-top:24px;border:none;border-top:1px solid #eaecf0">
<p style="color:#667085;font-size:12px">
Dit is een filter- en logging-systeem, geen trading-bot. Er wordt nooit
automatisch gekocht of verkocht en er is geen wallet gekoppeld. De laatste
sentiment-check en de koopbeslissing liggen bij jou. Niets hierin is
financieel advies.
</p>
</body></html>"""


def build_email_text(evaluation: Evaluation) -> str:
    lines = [
        f"{evaluation.symbol or 'onbekend'} — {evaluation.name or ''}",
        evaluation.token_address,
        f"Zachte score: {_fmt(evaluation.soft_score)}/100",
        "",
        f"DexScreener: {evaluation.dexscreener_url}",
        f"Rugcheck:    {evaluation.rugcheck_url}",
        f"X-sentiment: {evaluation.x_search_url}",
        "",
        "HARDE FILTERS",
    ]
    for result in evaluation.results:
        if result.hard:
            lines.append(
                f"  {result.name}: {result.outcome.value} "
                f"(waarde={_fmt(result.raw_value)}, drempel={result.threshold})"
            )
    lines.append("")
    lines.append("ZACHTE SIGNALEN")
    for result in evaluation.results:
        if not result.hard:
            lines.append(
                f"  {result.name}: {result.outcome.value} "
                f"(waarde={_fmt(result.raw_value)}, drempel={result.threshold})"
            )
    lines += [
        "",
        "Dit systeem koopt en verkoopt niets. Doe zelf de sentiment-check op X.",
        "Geen financieel advies.",
    ]
    return "\n".join(lines)


def build_message(evaluation: Evaluation, risk: Optional[dict[str, Any]] = None) -> EmailMessage:
    msg = EmailMessage()
    subject_symbol = evaluation.symbol or evaluation.token_address[:8]
    msg["Subject"] = (
        f"{config.EMAIL_SUBJECT_PREFIX} {subject_symbol} "
        f"— score {_fmt(evaluation.soft_score)}/100"
    )
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.ALERT_RECIPIENT
    msg.set_content(build_email_text(evaluation))
    msg.add_alternative(build_email_html(evaluation, risk), subtype="html")
    return msg


def send_alert(evaluation: Evaluation, risk: Optional[dict[str, Any]] = None) -> bool:
    """Verstuurt één alert. Geeft False terug bij een fout (run gaat door)."""
    try:
        ensure_configured()
        message = build_message(evaluation, risk)
        context = ssl.create_default_context()
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.starttls(context=context)
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(message)
        log.info("Alert verstuurd voor %s", evaluation.token_address)
        return True
    except Exception as exc:  # noqa: BLE001 — mailfout mag de run niet slopen
        log.error("Mail versturen mislukt voor %s: %s", evaluation.token_address, exc)
        return False


def send_run_summary(subject: str, body: str) -> bool:
    """Losse mail voor run-niveau meldingen (bv. veel rate-limit-hits)."""
    try:
        ensure_configured()
        msg = EmailMessage()
        msg["Subject"] = f"{config.EMAIL_SUBJECT_PREFIX} {subject}"
        msg["From"] = config.GMAIL_ADDRESS
        msg["To"] = config.ALERT_RECIPIENT
        msg.set_content(body)
        context = ssl.create_default_context()
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.starttls(context=context)
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Samenvattingsmail mislukt: %s", exc)
        return False
