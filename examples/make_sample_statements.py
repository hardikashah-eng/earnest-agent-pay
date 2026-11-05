"""Generate two fake, overlapping card statement exports for trying the insights feature.

    python examples/make_sample_statements.py

- statements/statement-jul-sep.csv : one signed Amount column, charges negative (Jul 1 - Sep 30)
- statements/statement-sep-oct.csv : separate Debit / Credit columns (Sep 1 - Oct 31)

September appears in both, so importing both shows deduplication.
All data is made up.
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

rng = random.Random(42)
START, END = date(2026, 7, 1), date(2026, 10, 31)
OUT = Path(__file__).parent / "statements"
tx: list[tuple[date, str, float]] = []  # (date, description, +spend / -credit)


def days():
    d = START
    while d <= END:
        yield d
        d += timedelta(days=1)


for d in days():
    if d.weekday() == 5:  # Saturday groceries
        tx.append((d, "WHOLEFDS MKT #10234 AUSTIN TX", round(rng.uniform(95, 145), 2)))
    if d.weekday() == 2 and d.isocalendar().week % 2 == 0:
        tx.append((d, "ALDI 72031 AUSTIN TX", round(rng.uniform(38, 55), 2)))
    if d.weekday() == 0 and d.isocalendar().week % 2 == 1:
        tx.append((d, "SHELL OIL 57444 AUSTIN TX", round(rng.uniform(38, 52), 2)))
    if d.weekday() in (1, 3) and rng.random() < 0.5:
        tx.append((d, "STARBUCKS STORE 08812", round(rng.uniform(5.5, 8.5), 2)))
    if d.day == 3:
        tx.append((d, "SPOTIFY USA", 11.99))
    if d.day == 5:
        tx.append((d, "NETFLIX.COM 866-579-7172 CA", 15.49 if d.month <= 8 else 17.99))
    if d.day == 12:
        tx.append((d, "HULU 877-8244858 CA", 7.99))
    if d.day == 15:
        tx.append((d, "DISNEY PLUS 888-905-7888 CA", 9.99))
    if d.day == 20:
        tx.append((d, "HBO MAX HELP.MAX.COM NY", 16.99))
    if d.day == 17:
        tx.append((d, "PLANET FITNESS #0412", 24.99))
    if d.day == 25:
        tx.append((d, "PAYMENT THANK YOU", -1400.00))

# Delivery: picking up in the fall
for month, count in ((7, 3), (8, 4), (9, 5), (10, 9)):
    for _ in range(count):
        d = date(2026, month, rng.randint(1, 28))
        place = rng.choice(["DOORDASH*THAI KITCHEN", "DOORDASH*BURGER BARN", "UBER *EATS PENDING"])
        tx.append((d, place, round(rng.uniform(26, 48), 2)))

# Amazon: normal, then an October spike
for month in (7, 8, 9, 10):
    for _ in range(3):
        tx.append((date(2026, month, rng.randint(1, 28)), "AMZN Mktp US*2K4L19", round(rng.uniform(18, 60), 2)))
tx.append((date(2026, 10, 11), "AMZN Mktp US*7Q2M88", 389.00))
tx.append((date(2026, 10, 19), "AMAZON.COM*RT5KX2", 149.99))
tx.append((date(2026, 10, 22), "AMAZON.COM*RT5KX2 REFUND", -24.99))

# Fees
tx.append((date(2026, 8, 9), "FOREIGN TRANSACTION FEE", 2.10))
tx.append((date(2026, 10, 7), "FOREIGN TRANSACTION FEE", 4.12))
tx.append((date(2026, 10, 28), "LATE FEE", 39.00))

tx.sort()
OUT.mkdir(exist_ok=True)

with open(OUT / "statement-jul-sep.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Transaction Date", "Post Date", "Description", "Amount"])
    for d, desc, amt in tx:
        if d <= date(2026, 9, 30):
            w.writerow([d.strftime("%m/%d/%Y"), (d + timedelta(days=1)).strftime("%m/%d/%Y"), desc, f"{-amt:.2f}"])

with open(OUT / "statement-sep-oct.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Debit", "Credit"])
    for d, desc, amt in tx:
        if d >= date(2026, 9, 1):
            w.writerow([d.isoformat(), desc, f"{amt:.2f}" if amt > 0 else "", f"{-amt:.2f}" if amt < 0 else ""])

print(f"wrote {OUT / 'statement-jul-sep.csv'} and {OUT / 'statement-sep-oct.csv'}")
