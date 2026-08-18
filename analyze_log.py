"""
analyze_log.py — drempel-tuning achteraf, zonder opnieuw te scannen.

Dit is waar het loggen van ruwe waardes voor bedoeld is (plan §5). Je kunt
hier vragen beantwoorden als "wat als de bot-score-grens 40 was geweest in
plaats van 60?" en zien wat dat met je hitrate had gedaan.

Draai:
    python analyze_log.py                          # overzicht
    python analyze_log.py --what-if bot_score=40   # simuleer een drempel
    python analyze_log.py --performance            # 24u/72u/7d-resultaten
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from typing import Any, Optional

import csv_log
import filters


def _num(value: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _raw(row: dict[str, str], name: str) -> Any:
    value = row.get(f"{name}__raw", "")
    if value == "":
        return None
    if value in ("true", "false"):
        return value == "true"
    number = _num(value)
    if number is not None:
        return number
    if value.startswith("{") or value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def overview(rows: list[dict[str, str]]) -> None:
    print(f"Regels in log: {len(rows)}")
    if not rows:
        return
    alerted = sum(1 for r in rows if r.get("alerted") == "true")
    hard_pass = sum(1 for r in rows if r.get("hard_pass") == "true")
    print(f"Harde filters gehaald: {hard_pass} ({hard_pass/len(rows)*100:.1f}%)")
    print(f"Gealerteerd:           {alerted} ({alerted/len(rows)*100:.1f}%)")

    print("\nWaarom vielen coins af (per filter):")
    for name in filters.FILTER_NAMES:
        counts = Counter(r.get(f"{name}__outcome", "") for r in rows)
        fails = counts.get("fail", 0)
        unavailable = counts.get("data_unavailable", 0)
        if fails or unavailable:
            print(
                f"  {name:32s} fail={fails:5d}  geen-data={unavailable:5d}"
                f"  pass={counts.get('pass', 0):5d}"
            )

    print("\nVerdeling van numerieke ruwe waardes:")
    for name in filters.SOFT_FILTER_NAMES + ["marketcap_eur", "liquidity_eur", "liq_mc_ratio"]:
        values = [v for v in (_raw(r, name) for r in rows) if isinstance(v, (int, float))]
        if len(values) < 3:
            continue
        values.sort()
        print(
            f"  {name:26s} n={len(values):5d}  min={values[0]:12.4g}"
            f"  mediaan={statistics.median(values):12.4g}  max={values[-1]:12.4g}"
        )


def what_if(rows: list[dict[str, str]], expressions: list[str]) -> None:
    """Simuleert alternatieve drempels op de al gelogde ruwe waardes."""
    parsed = []
    for expr in expressions:
        if "=" not in expr:
            print(f"Sla '{expr}' over — verwacht formaat filter=waarde")
            continue
        name, raw_value = expr.split("=", 1)
        parsed.append((name.strip(), float(raw_value)))

    if not parsed:
        return

    # Filters waar een LAGERE waarde beter is.
    lower_is_better = {"bot_score", "holder_growth_per_min"}

    print(f"\nWhat-if op {len(rows)} gelogde regels:")
    for name, threshold in parsed:
        values = [(_raw(r, name), r) for r in rows]
        usable = [(v, r) for v, r in values if isinstance(v, (int, float))]
        if not usable:
            print(f"  {name}: geen numerieke waardes in het log")
            continue

        if name in lower_is_better:
            keep = [r for v, r in usable if v <= threshold]
        else:
            keep = [r for v, r in usable if v >= threshold]

        was_alerted = sum(1 for r in keep if r.get("alerted") == "true")
        print(
            f"  {name} drempel {threshold:g}: {len(keep)}/{len(usable)} regels zouden "
            f"passeren (waarvan {was_alerted} destijds ook echt gealerteerd zijn)"
        )


def performance(rows: list[dict[str, str]]) -> None:
    """Waren de afwijzingen terecht? Dit is waar followup.py voor draait."""
    print("\nResultaten na 24u / 72u / 7d (mediaan marketcap-verandering):")
    for group_name, predicate in (
        ("gealerteerd", lambda r: r.get("alerted") == "true"),
        ("afgewezen", lambda r: r.get("alerted") != "true"),
    ):
        subset = [r for r in rows if predicate(r)]
        line = [f"  {group_name:12s} n={len(subset):5d}"]
        for interval in ("24h", "72h", "7d"):
            changes = []
            for row in subset:
                start = _num(row.get("market_cap_eur", ""))
                later = _num(row.get(f"mc_eur_{interval}", ""))
                if start and later is not None and start > 0:
                    changes.append((later - start) / start * 100.0)
            if changes:
                line.append(f"{interval}: {statistics.median(changes):+7.1f}% (n={len(changes)})")
            else:
                line.append(f"{interval}: geen data")
        print("  ".join(line))

    print(
        "\n  Let op: 'afgewezen' hoort gemiddeld slechter te presteren dan\n"
        "  'gealerteerd'. Is dat niet zo, dan filter je op de verkeerde dingen."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyseer scan_log.csv")
    parser.add_argument("--what-if", action="append", default=[], metavar="FILTER=WAARDE")
    parser.add_argument("--performance", action="store_true")
    args = parser.parse_args()

    rows = csv_log.read_rows()
    overview(rows)
    if args.what_if:
        what_if(rows, args.what_if)
    if args.performance:
        performance(rows)


if __name__ == "__main__":
    main()
