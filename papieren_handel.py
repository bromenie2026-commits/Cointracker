import csv, statistics

PAD = "logs/scan_log.csv"
KOSTEN = 5.0
INLEG = 25.0


def getal(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rendementen(rijen, interval):
    uit = []
    for r in rijen:
        instap = getal(r.get("price_usd"))
        uitstap = getal(r.get("price_" + interval))
        if instap and uitstap is not None and instap > 0:
            uit.append((uitstap / instap - 1) * 100 - KOSTEN)
    return uit


with open(PAD, newline="", encoding="utf-8") as f:
    rijen = list(csv.DictReader(f))

gealerteerd = [r for r in rijen if r.get("alerted") == "true"]
afgewezen = [r for r in rijen if r.get("alerted") != "true"]

print("Regels in log:", len(rijen))
print("Gealerteerd:", len(gealerteerd), "| afgewezen:", len(afgewezen))
print("\nPAPIEREN HANDEL - GEEN ECHT GELD")
print("Inleg EUR", INLEG, "per alert,", KOSTEN, "% kosten per trade")

for interval in ("24h", "72h", "7d"):
    a = rendementen(gealerteerd, interval)
    b = rendementen(afgewezen, interval)
    print("\n--- verkocht na " + interval + " ---")
    if not a:
        print("Nog geen follow-up-data. Laat langer draaien.")
        continue
    winst = [x for x in a if x > 0]
    groot = [x for x in a if x >= 30]
    ingelegd = INLEG * len(a)
    over = sum(INLEG * (1 + x / 100) for x in a)
    print("Trades:          ", len(a))
    print("Winstgevend:     ", len(winst), "(%.0f%%)" % (len(winst) / len(a) * 100))
    print("Minimaal +30%:   ", len(groot), "(%.0f%%)" % (len(groot) / len(a) * 100))
    print("Mediaan:          %+.1f%%" % statistics.median(a))
    print("Gemiddeld:        %+.1f%%" % statistics.fmean(a))
    print("Slechtste/beste:  %+.1f%% / %+.1f%%" % (min(a), max(a)))
    print("Ingelegd:         EUR %.2f" % ingelegd)
    print("Overgehouden:     EUR %.2f" % over)
    print("RESULTAAT:        EUR %+.2f" % (over - ingelegd))
    if b:
        print("Controlegroep (afgewezen): mediaan %+.1f%% over %d stuks"
              % (statistics.median(b), len(b)))

print("\nKijk naar de MEDIAAN, niet naar het gemiddelde.")
print("Doet de controlegroep het even goed? Dan filtert het systeem niets zinnigs weg.")
