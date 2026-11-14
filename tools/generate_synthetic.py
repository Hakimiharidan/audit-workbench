"""Generate the synthetic sample datasets and the list of planted issues.

    python tools/generate_synthetic.py --seed 42 --out sample_data

Everything here is invented: names come from syllable lists, phone and ID
numbers use an obviously fake format (000-FAKE-1234, FAKE-123456), and no value
is taken from any real dataset. The same seed always gives the same data.

Outputs:
  retail.xlsx           customers, staff, items, receipts, receipt lines, payouts
                        (commissions), purchase orders, PO lines
  gl.csv                a small general ledger, balanced per journal
  holidays_my_2025.yaml illustrative public-holiday list used by JE-01
  planted_issues.json   every planted anomaly as the exception_id the packs emit
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl

GENERATOR_VERSION = 1
CENT = Decimal("0.01")
FIXED_TIME = dt.datetime(2026, 1, 1, 0, 0, 0)

RETAIL_START = dt.date(2024, 7, 1)
RETAIL_END = dt.date(2025, 6, 30)
N_CUSTOMERS = 300
N_SALES_BASE = 1480
N_POS = 25
SST = Decimal("1.06")         # the hidden commission rule works on the amount before a 6% tax

# Illustrative sample only; see the YAML header written below.
HOLIDAYS_MY_2025 = [
    ("2025-01-29", "Chinese New Year"),
    ("2025-01-30", "Chinese New Year (second day)"),
    ("2025-03-31", "Hari Raya Aidilfitri"),
    ("2025-04-01", "Hari Raya Aidilfitri (second day)"),
    ("2025-05-01", "Labour Day"),
    ("2025-05-12", "Wesak Day"),
    ("2025-06-02", "Birthday of the Yang di-Pertuan Agong"),
    ("2025-06-07", "Hari Raya Haji"),
    ("2025-06-27", "Awal Muharram"),
    ("2025-08-31", "National Day"),
    ("2025-09-05", "Prophet Muhammad's Birthday"),
    ("2025-09-16", "Malaysia Day"),
    ("2025-10-20", "Deepavali"),
    ("2025-12-25", "Christmas Day"),
]

SYL = ["ka", "ri", "mo", "ta", "se", "lin", "dar", "vo", "ne", "ul", "pa", "zen", "ro", "mi", "sa", "tu"]
ITEM_WORDS = ["Nova", "Orbit", "Pulse", "Vega", "Lumen", "Arc", "Quill", "Zephyr", "Halo", "Ember"]
ACCOUNTS = ["1000 Cash", "1100 Receivables", "1200 Inventory", "2000 Payables", "2100 Accruals",
            "4000 Revenue", "5000 Cost of sales", "6100 Rent", "6200 Utilities", "6300 Salaries"]


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fake_name(rng: random.Random) -> str:
    def word():
        return "".join(rng.choice(SYL) for _ in range(rng.randint(2, 3))).capitalize()
    return f"{word()} {word()}"


def fake_phone(rng):
    return f"000-FAKE-{rng.randint(0, 9999):04d}"


def rand_date(rng, start: dt.date, end: dt.date) -> dt.date:
    return start + dt.timedelta(days=rng.randint(0, (end - start).days))


def as_dt(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day)


# --------------------------------------------------------------------------- retail

def build_retail(rng: random.Random):
    customers = [{"ClientCode": f"CL{i:04d}", "ClientName": fake_name(rng), "Mobile": fake_phone(rng),
                  "BirthDate": as_dt(rand_date(rng, dt.date(1960, 1, 1), dt.date(2004, 12, 31)))
                  if rng.random() > 0.1 else "N/A"} for i in range(1, N_CUSTOMERS + 1)]

    roles = ["Manager", "Cashier", "Cashier", "Sales Associate", "Cashier", "Sales Associate",
             "Cashier", "Sales Associate", "Cashier", "Sales Associate"]
    staff = []
    for i, role in enumerate(roles, start=1):
        staff.append({
            "StaffCode": f"ST{i:03d}", "StaffName": fake_name(rng), "Role": role,
            "Sex": rng.choice(["F", "M"]),
            "BirthDate": as_dt(rand_date(rng, dt.date(1975, 1, 1), dt.date(2002, 12, 31))),
            "Mobile": fake_phone(rng),
            "Joined": as_dt(rand_date(rng, dt.date(2019, 1, 1), dt.date(2023, 12, 31))),
            "Left": "N/A", "EmploymentStatus": "Active",
            "CommRate": float(rng.choice(["0.01", "0.015", "0.02", "0.025", "0.03"])) if role != "Manager" else 0.0,
            "NationalIdFake": f"FAKE-{rng.randint(0, 999999):06d}",
        })
    leaver = staff[3]           # ST004: leaves, then 4 receipts appear after the leave date (planted)
    leaver_date = dt.date(2025, 2, 14)
    leaver["Left"], leaver["EmploymentStatus"] = as_dt(leaver_date), "Resigned"
    active_leaver = staff[6]    # ST007: leave date recorded but still Active (planted)
    active_leaver_date = dt.date(2025, 5, 30)
    active_leaver["Left"] = as_dt(active_leaver_date)
    clean_leaver = staff[8]     # ST009: a correct leaver with no later activity (must not be flagged)
    clean_leaver_date = dt.date(2024, 12, 31)
    clean_leaver["Left"], clean_leaver["EmploymentStatus"] = as_dt(clean_leaver_date), "Resigned"
    leave = {s["StaffCode"]: (s["Left"].date() if isinstance(s["Left"], dt.datetime) else None) for s in staff}
    joined = {s["StaffCode"]: s["Joined"].date() for s in staff}
    rate = {s["StaffCode"]: Decimal(str(s["CommRate"])) for s in staff}
    sellers = [s["StaffCode"] for s in staff if s["Role"] != "Manager"]

    items = [{"ItemCode": f"IT{i:03d}", "ItemName": f"{rng.choice(ITEM_WORDS)} {rng.choice(['S', 'X', 'Pro', 'Lite'])}{i}",
              "UnitPrice": rng.choice(range(400, 4001, 50)),
              "Spec": f"{rng.choice([64, 128, 256])}GB / {rng.choice([4, 6, 8])}GB"} for i in range(1, 21)]
    price = {it["ItemCode"]: it["UnitPrice"] for it in items}

    def eligible(d):
        return [s for s in sellers if joined[s] <= d and (leave[s] is None or d <= leave[s])]

    sales = []
    used_keys = set()

    def new_sale(d, seller, client):
        lines = [rng.choice(items)["ItemCode"] for _ in range(rng.randint(1, 3))]
        total = sum(price[x] for x in lines)
        return {"date": d, "client": client, "seller": seller, "lines": lines, "total": total,
                "tender": rng.choice(["Cash", "Card", "E-wallet"]), "tag": None}

    while len(sales) < N_SALES_BASE:
        d = rand_date(rng, RETAIL_START, RETAIL_END)
        choices = eligible(d)
        if not choices:
            continue
        client = rng.choice(customers)["ClientCode"] if rng.random() < 0.2 else "N/A"
        s = new_sale(d, rng.choice(choices), client)
        key = (s["date"], s["client"], s["total"], s["seller"])
        if client != "N/A" and key in used_keys:
            continue                       # keep accidental duplicates out
        used_keys.add(key)
        sales.append(s)

    # planted: 4 receipts by ST004 after the leave date
    for k in range(4):
        s = new_sale(leaver_date + dt.timedelta(days=3 + 9 * k), leaver["StaffCode"], "N/A")
        s["tag"] = "after_leave"
        sales.append(s)
    # planted: 4 duplicate pairs among named customers
    named = [s for s in sales if s["client"] != "N/A" and s["tag"] is None]
    for orig in rng.sample(named, 4):
        orig["tag"] = "dup_orig"
        copy = dict(orig, lines=list(orig["lines"]), tag="dup_copy", tender=orig["tender"])
        copy["orig"] = orig
        sales.append(copy)

    rng.shuffle(sales)
    sales.sort(key=lambda s: s["date"])
    for i, s in enumerate(sales, start=1):
        s["id"] = f"R{i:05d}"

    # planted: 6 receipts removed from the receipts and lines sheets but kept in payouts
    removable = [s for s in sales[1:-1] if s["tag"] is None]
    removed = set(id(s) for s in rng.sample(removable, 6))
    for s in sales:
        s["removed"] = id(s) in removed

    # payouts (commissions): one per receipt
    payouts = []
    candidates = [s for s in sales if not s["removed"] and s["tag"] is None]
    misattributed = set(id(s) for s in rng.sample(candidates, 72))
    for s in sales:
        payee = s["seller"]
        if id(s) in misattributed:
            payee = rng.choice([x for x in sellers if x != s["seller"]])
            s["misattributed"] = True
        amount = q2(Decimal(s["total"]) / SST * rate[s["seller"]])
        payouts.append({"ReceiptNo": s["id"], "StaffCode": payee, "PayoutAmount": float(amount), "_amount": amount})

    # purchase orders
    pos = []
    po_lines = []
    raisers = [s["StaffCode"] for s in staff if s["Role"] in ("Manager", "Sales Associate")]
    approver = staff[0]["StaffCode"]
    sod_index = rng.randrange(N_POS)
    for i in range(1, N_POS + 1):
        lines = [(it, rng.randint(5, 40)) for it in rng.sample([x["ItemCode"] for x in items], rng.randint(8, 18))]
        raised = rng.choice([r for r in raisers if r != approver])
        appr = approver
        if i - 1 == sod_index:
            raised = appr = approver          # planted: raised and approved by the same person
        po = {"PONumber": f"PO{i:03d}", "PODate": as_dt(rand_date(rng, RETAIL_START, RETAIL_END)),
              "TotalUnits": sum(u for _, u in lines), "RaisedBy": raised, "SignedOffBy": appr}
        pos.append(po)
        po_lines += [{"PONumber": po["PONumber"], "ItemCode": it, "Units": u} for it, u in lines]

    # planted list (exception ids exactly as the packs emit them)
    kept = [s for s in sales if not s["removed"]]
    num = lambda sid: int(sid[1:])
    planted = {
        "ITGC-01": sorted([f"after_leave:{s['id']}" for s in kept if s["tag"] == "after_leave"]
                          + [f"leaver_still_active:{active_leaver['StaffCode']}"]),
        "ITGC-02": [f"sod:{pos[sod_index]['PONumber']}"],
        "REV-01": sorted(f"dup:{s['id']}" for s in kept if s["tag"] in ("dup_orig", "dup_copy")),
        "REV-02": sorted([f"gap:{num(s['id'])}" for s in sales if s["removed"]]
                         + [f"orphan:{s['id']}" for s in sales if s["removed"]]),
        "PAY-01": sorted(
            [f"attribution:{s['id']}" for s in kept if s.get("misattributed")]
            + [f"recalc:{p['ReceiptNo']}" for s, p in zip(sales, payouts)
               if not s["removed"] and abs(p["_amount"] - Decimal(s["total"]) * rate[s["seller"]]) > Decimal("1.00")]),
    }

    sheets = {
        "Clients": (["ClientCode", "ClientName", "Mobile", "BirthDate"],
                    [[c["ClientCode"], c["ClientName"], c["Mobile"], c["BirthDate"]] for c in customers]),
        "Staff": (["StaffCode", "StaffName", "Role", "Sex", "BirthDate", "Mobile", "Joined", "Left",
                   "EmploymentStatus", "CommRate", "NationalIdFake"],
                  [[s[k] for k in ["StaffCode", "StaffName", "Role", "Sex", "BirthDate", "Mobile", "Joined", "Left",
                                   "EmploymentStatus", "CommRate", "NationalIdFake"]] for s in staff]),
        "Items": (["ItemCode", "ItemName", "UnitPrice", "Spec"],
                  [[i["ItemCode"], i["ItemName"], i["UnitPrice"], i["Spec"]] for i in items]),
        "Receipts": (["ReceiptNo", "ReceiptDate", "ClientCode", "Tender", "Units", "GrossAmount", "CashierCode"],
                     [[s["id"], as_dt(s["date"]), s["client"], s["tender"], len(s["lines"]), s["total"], s["seller"]]
                      for s in kept]),
        "ReceiptItems": (["ReceiptNo", "ItemCode"], [[s["id"], x] for s in kept for x in s["lines"]]),
        "Payouts": (["ReceiptNo", "StaffCode", "PayoutAmount"],
                    [[p["ReceiptNo"], p["StaffCode"], p["PayoutAmount"]] for p in payouts]),
        "POs": (["PONumber", "PODate", "TotalUnits", "RaisedBy", "SignedOffBy"],
                [[p[k] for k in ["PONumber", "PODate", "TotalUnits", "RaisedBy", "SignedOffBy"]] for p in pos]),
        "POItems": (["PONumber", "ItemCode", "Units"], [[l["PONumber"], l["ItemCode"], l["Units"]] for l in po_lines]),
    }
    return sheets, planted


# --------------------------------------------------------------------------- general ledger

def build_gl(rng: random.Random):
    holidays = {dt.date.fromisoformat(d) for d, _ in HOLIDAYS_MY_2025}
    start, end = dt.date(2025, 1, 1), dt.date(2025, 12, 31)

    def is_weekend(d):
        return d.isoweekday() in (6, 7)

    def normal_day():
        while True:
            d = rand_date(rng, start, end)
            if not is_weekend(d) and d not in holidays:
                return d

    def weekend_day():
        while True:
            d = rand_date(rng, start, end)
            if is_weekend(d) and d not in holidays:
                return d

    def odd_amount():
        while True:
            a = Decimal(rng.randint(5000, 2_500_000)) / 100
            if a % 1000 != 0:
                return a

    def normal_lines():
        while True:
            n = rng.randint(1, 3)
            debits = [odd_amount() for _ in range(n)]
            credit = -sum(debits)
            if credit % 1000 != 0:
                return debits + [credit]

    def round_lines():
        a = Decimal(rng.randint(10, 60) * 1000)
        return [a, -a]

    journals = []
    for _ in range(300):
        journals.append({"date": normal_day(), "amounts": normal_lines(), "tag": None})
    for _ in range(5):
        journals.append({"date": weekend_day(), "amounts": normal_lines(), "tag": "weekend"})
    for d in ["2025-05-01", "2025-09-16", "2025-12-25"]:
        journals.append({"date": dt.date.fromisoformat(d), "amounts": normal_lines(), "tag": "holiday"})
    for _ in range(6):
        journals.append({"date": normal_day(), "amounts": round_lines(), "tag": "round"})
    for _ in range(2):
        journals.append({"date": weekend_day(), "amounts": round_lines(), "tag": "weekend;round"})
    rng.shuffle(journals)
    journals.sort(key=lambda j: j["date"])

    rows, planted = [], []
    for i, j in enumerate(journals, start=1):
        je = f"JV{i:05d}"
        poster = rng.choice(["usr-a01", "usr-b02", "usr-c03", "usr-d04"])
        src = rng.choice(["MANUAL", "AP", "AR", "PAYROLL"])
        for ln, amt in enumerate(j["amounts"], start=1):
            rows.append([je, ln, j["date"].isoformat(), rng.choice(ACCOUNTS), f"{amt:.2f}",
                         f"Synthetic entry {i}", poster, src])
            if j["tag"]:
                planted.append(f"{je}:{ln}")
    header = ["JrnlRef", "Ln", "PostDate", "Acct", "Amt", "Narrative", "Poster", "Src"]
    return header, rows, sorted(planted)


# --------------------------------------------------------------------------- writers

def write_xlsx(path: Path, sheets: dict) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append(r)
    wb.properties.created = FIXED_TIME
    wb.properties.modified = FIXED_TIME
    wb.properties.creator = "generate_synthetic.py"
    wb.save(path)


def write_holidays(path: Path) -> None:
    lines = [
        "# Illustrative sample — not an authoritative calendar; supply your own.",
        "# Malaysian national public holidays for 2025, for the synthetic GL demo only.",
        "# State holidays and replacement days are not included.",
        'label: "Illustrative sample"',
        "dates:",
    ]
    lines += [f'  - {{date: {d}, name: "{n}"}}' for d, n in HOLIDAYS_MY_2025]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(seed: int, out: Path) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    sheets, planted = build_retail(rng)
    header, gl_rows, gl_planted = build_gl(rng)
    planted["JE-01"] = gl_planted

    write_xlsx(out / "retail.xlsx", sheets)
    with open(out / "gl.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(gl_rows)
    write_holidays(out / "holidays_my_2025.yaml")

    result = {
        "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "row_counts": {name: len(rows) for name, (_, rows) in sheets.items()} | {"gl": len(gl_rows)},
        "runs": {k: planted[k] for k in sorted(planted)},
    }
    (out / "planted_issues.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "sample_data")
    args = ap.parse_args(argv)
    result = generate(args.seed, args.out)
    counts = {k: len(v) for k, v in result["runs"].items()}
    print(f"seed {args.seed}: wrote {args.out} — planted per run: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
