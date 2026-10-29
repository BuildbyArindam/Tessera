"""Seeded synthetic SME dataset generator.

    python -m tessera_evalkit.generate --seed 7

Writes data/synthetic/*.parquet (+ manifest.json) and data/fx/ (see fx.py).
Fully deterministic given (seed, start, months, FX source): every component
draws from its own named stream (rng.py), so output is stable run to run.

Design notes (what is modelled, so it can be defended in a demo):

* 12 businesses: 4 solo freelancers, 4 five-person agencies, 4 product exporters.
* Each customer has a fixed type that drives its own log-normal days-past-due:
  prompt / typical / chronic_late / seasonal_late (slow in its own stretch of
  months). Latent type and true pay date go ONLY to ground_truth.parquet.
* Invoice volume = Poisson(customer rate x seasonality x growth). Seasonality:
  quarter-end months x1.3 (and weighted to month-end days), December x0.6.
  ~2% of invoices are one-off large (x4-10).
* ~2% of invoices end badly: never paid, disputed-unpaid, or disputed then
  settled for 40-80%.
* Outflows: INR payroll on a fixed day, SGD/AUD supplier invoices, recurring
  USD/EUR SaaS card spend.
* "As of" is the end of the window: payments after it are not recorded and
  those invoices appear as open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__
from .calendars import (
    add_months, clamp_day, month_starts, roll_back, roll_forward, window_end,
)
from .fx import FxTable, load_fx
from .records import (
    BusinessRec, CounterpartyRec, CustomerRec, GroundTruthRec, InvoiceRec,
    OutflowRec, PaymentRec, Rec, cents_to_decimal,
)
from .rng import rng_for

log = logging.getLogger("tessera_evalkit.generate")

N_PER_ARCHETYPE = 4
P_NEVER_PAID, P_DISPUTE_UNPAID, P_DISPUTE_SETTLED = 0.010, 0.006, 0.004  # ~2% total
P_ONE_OFF = 0.02
QUARTER_END_MONTHS = (3, 6, 9, 12)

ARCH: dict[str, dict] = {
    "solo_freelancer": dict(
        label="Freelance", n_cust=(4, 8), rate=(0.7, 1.2), median=(600, 2000),
        cur_w={"USD": 0.60, "EUR": 0.25, "GBP": 0.15}, n_emp=(1, 1), salary=(35_000, 55_000),
        n_sup=(1, 2), sup_med=(300, 900), sup_rate=0.7, n_saas=(3, 5), saas=(10, 60),
        growth=(0.05, 0.35), payroll_days=(1, 7, 25, 28),
    ),
    "small_agency": dict(
        label="Studio", n_cust=(10, 18), rate=(0.6, 1.1), median=(1200, 3200),
        cur_w={"USD": 0.40, "EUR": 0.35, "GBP": 0.25}, n_emp=(5, 5), salary=(85_000, 140_000),
        n_sup=(2, 4), sup_med=(1500, 4000), sup_rate=1.0, n_saas=(4, 7), saas=(20, 180),
        growth=(0.10, 0.50), payroll_days=(1, 7, 25, 28),
    ),
    "product_exporter": dict(
        label="Exports", n_cust=(12, 25), rate=(0.6, 1.3), median=(2500, 7000),
        cur_w={"USD": 0.45, "EUR": 0.35, "GBP": 0.20}, n_emp=(9, 14), salary=(30_000, 70_000),
        n_sup=(3, 5), sup_med=(6000, 20000), sup_rate=1.3, n_saas=(3, 6), saas=(15, 120),
        growth=(0.05, 0.40), payroll_days=(1, 7, 25, 28),
    ),
}

# (median days past due, log-sigma, shift) per customer type
DELAY = {
    "prompt": (3.0, 0.5, -3),
    "typical": (8.0, 0.6, 0),
    "chronic_late": (30.0, 0.5, 0),
    "seasonal_late": (7.0, 0.6, 0),
}
SEASONAL_STRETCHES = ((3, 6, 9, 12), (12, 1), (7, 8))
SEASONAL_MULT = 3.0

_FIRST = ("Northwind", "Bluepeak", "Lumen", "Kestrel", "Orchard", "Harbor", "Ivory", "Summit",
          "Quill", "Marlow", "Tidewater", "Cobalt", "Juniper", "Atlas", "Foxglove", "Granite",
          "Helix", "Meridian", "Nimbus", "Obsidian", "Pinecrest", "Redwood", "Sable", "Thistle")
_SECOND = ("Labs", "Trading", "Partners", "Studio", "Group", "Ltd", "GmbH", "Co", "Systems",
           "Retail", "Media", "Works", "Holdings", "Collective")
_SAAS = ("Workspace Suite", "Design Tool", "Code Hosting", "Cloud Compute", "Project Tracker",
         "Video Calls", "Email Platform", "Analytics", "Password Vault", "Accounting App")
_SUP = ("Pacific Components", "Lion City Logistics", "Harbourline Freight", "Sydney Packaging",
        "Coral Textiles", "Merlion Prints", "Outback Materials", "Orchid Fabrication")


# ------------------------------------------------------------------ helpers ---
def _name(rng: np.random.Generator, used: set[str]) -> str:
    for _ in range(50):
        n = f"{_FIRST[rng.integers(len(_FIRST))]} {_SECOND[rng.integers(len(_SECOND))]}"
        if n not in used:
            used.add(n)
            return n
    n = f"{_FIRST[rng.integers(len(_FIRST))]} {len(used)}"
    used.add(n)
    return n


def _cents(x: float) -> int:
    return max(100, int(round(x * 100)))


def _choice_w(rng: np.random.Generator, weights: dict[str, float]) -> str:
    keys = list(weights)
    return keys[int(rng.choice(len(keys), p=np.array(list(weights.values())) / sum(weights.values())))]


def _uniform_int(rng: np.random.Generator, lo: int, hi: int) -> int:
    return int(rng.integers(lo, hi + 1))


def seasonality(month: int) -> float:
    if month == 12:
        return 0.6
    if month in QUARTER_END_MONTHS:
        return 1.3
    return 1.0


@dataclass
class _Cust:
    rec: CustomerRec
    ctype: str
    rate: float
    median_amt: float
    med_dpd: float
    sigma: float
    stretch: tuple[int, ...]
    onboard_idx: int


# ----------------------------------------------------------------- per biz ---
def _gen_business(seed: int, idx: int, archetype: str, start: date, months: int):
    cfg = ARCH[archetype]
    bid = f"B{idx + 1:02d}"
    r = rng_for(seed, "biz", bid, "profile")
    used: set[str] = set()
    as_of = window_end(start, months)
    mstarts = month_starts(start, months)

    biz = BusinessRec(
        business_id=bid, name=f"{_name(r, used)} ({cfg['label']})", archetype=archetype,
        base_currency="INR", payroll_day=int(r.choice(cfg["payroll_days"])),
        growth_rate=round(float(r.uniform(*cfg["growth"])), 3),
    )

    # ---- customers
    rc = rng_for(seed, "biz", bid, "customers")
    n = _uniform_int(rc, *cfg["n_cust"])
    n_chronic = max(1, round(0.12 * n))
    n_seasonal = max(1, round(0.12 * n))
    n_prompt = max(1, round(0.15 * n))
    types = (["chronic_late"] * n_chronic + ["seasonal_late"] * n_seasonal + ["prompt"] * n_prompt)
    types += ["typical"] * (n - len(types))
    types = [types[i] for i in rc.permutation(len(types))]
    custs: list[_Cust] = []
    for i, ctype in enumerate(types):
        med, sig, _shift = DELAY[ctype]
        onboard_idx = 0 if rc.random() < 0.75 else _uniform_int(rc, 1, max(1, min(9, months - 6)))
        cur = _choice_w(rc, cfg["cur_w"])
        rec = CustomerRec(
            customer_id=f"C-{bid}-{i + 1:03d}", business_id=bid, name=_name(rc, used),
            currency=cur, payment_terms_days=int(rc.choice([14, 30, 30, 45, 60])),
            onboarded=mstarts[onboard_idx],
        )
        custs.append(_Cust(
            rec=rec, ctype=ctype, rate=float(rc.uniform(*cfg["rate"])),
            median_amt=float(math.exp(rc.uniform(math.log(cfg["median"][0]), math.log(cfg["median"][1])))),
            med_dpd=med * float(math.exp(rc.normal(0, 0.25))), sigma=sig * float(rc.uniform(0.85, 1.15)),
            stretch=SEASONAL_STRETCHES[int(rc.integers(len(SEASONAL_STRETCHES)))],
            onboard_idx=onboard_idx,
        ))

    # ---- invoices (latent outcome drawn at creation, in a fixed loop order)
    ri = rng_for(seed, "biz", bid, "invoices")
    rd = rng_for(seed, "biz", bid, "delays")
    pending = []  # (invoice_date, customer_id, ...)
    for mi, m0 in enumerate(mstarts):
        t_years = mi / 12.0
        growth = (1.0 + biz.growth_rate) ** t_years
        count_f, amt_f = math.sqrt(growth), math.sqrt(growth)
        season = seasonality(m0.month)
        for c in custs:
            if mi < c.onboard_idx:
                continue
            k = int(ri.poisson(c.rate * season * count_f))
            for _ in range(k):
                if m0.month in QUARTER_END_MONTHS and ri.random() < 0.5:
                    day = _uniform_int(ri, 22, 31)
                else:
                    day = _uniform_int(ri, 1, 28)
                inv_date = roll_back(clamp_day(m0.year, m0.month, day))
                amount = c.median_amt * amt_f * float(math.exp(ri.normal(0, 0.35)))
                one_off = ri.random() < P_ONE_OFF
                if one_off:
                    amount *= float(ri.uniform(4, 10))
                due = inv_date + timedelta(days=c.rec.payment_terms_days)
                outcome, pay, dpd, paid_cents = _draw_outcome(rd, c, due, inv_date, _cents(amount))
                pending.append((inv_date, c, due, _cents(amount), one_off, outcome, pay, dpd, paid_cents))

    pending.sort(key=lambda x: (x[0], x[1].rec.customer_id, x[3]))
    invoices, payments, truth = [], [], []
    for n_inv, (inv_date, c, due, cents, one_off, outcome, pay, dpd, paid_cents) in enumerate(pending, 1):
        iid = f"INV-{bid}-{n_inv:05d}"
        status = _status(outcome, pay, due, as_of)
        invoices.append(InvoiceRec(
            invoice_id=iid, business_id=bid, customer_id=c.rec.customer_id, invoice_date=inv_date,
            due_date=due, currency=c.rec.currency, amount=cents_to_decimal(cents), status=status,
            memo=("One-off project milestone" if one_off else "Services rendered" if archetype != "product_exporter" else "Goods shipped"),
        ))
        if pay is not None and pay <= as_of and paid_cents:
            payments.append(PaymentRec(
                payment_id=f"PAY-{bid}-{len(payments) + 1:05d}", invoice_id=iid, business_id=bid,
                pay_date=pay, currency=c.rec.currency, amount=cents_to_decimal(paid_cents),
            ))
        truth.append(GroundTruthRec(
            invoice_id=iid, business_id=bid, customer_id=c.rec.customer_id, customer_type=c.ctype,
            outcome=outcome, latent_pay_date=pay, days_past_due=dpd,
        ))

    cps, outs = _gen_outflows(seed, bid, cfg, biz, start, months, as_of, mstarts)
    return biz, [c.rec for c in custs], invoices, payments, truth, cps, outs


def _draw_outcome(rd, c: _Cust, due: date, inv_date: date, cents: int):
    u = rd.random()
    if u < P_NEVER_PAID:
        return "never_paid", None, None, 0
    if u < P_NEVER_PAID + P_DISPUTE_UNPAID:
        return "disputed_unpaid", None, None, 0
    if u < P_NEVER_PAID + P_DISPUTE_UNPAID + P_DISPUTE_SETTLED:
        pay = roll_forward(due + timedelta(days=_uniform_int(rd, 45, 150)))
        return "disputed_settled", pay, (pay - due).days, _cents(cents / 100 * float(rd.uniform(0.4, 0.8)))
    med, sig, shift = DELAY[c.ctype]
    mult = SEASONAL_MULT if (c.ctype == "seasonal_late" and due.month in c.stretch) else 1.0
    raw = math.exp(rd.normal(math.log(c.med_dpd * mult), sig))
    dpd = min(180, int(round(raw)) + shift)
    pay = roll_forward(max(due + timedelta(days=dpd), inv_date + timedelta(days=1)))
    return "paid", pay, (pay - due).days, cents


def _status(outcome: str, pay: date | None, due: date, as_of: date) -> str:
    if outcome == "paid":
        return "paid" if pay is not None and pay <= as_of else "open"
    if outcome == "disputed_settled":
        if pay is not None and pay <= as_of:
            return "settled_partial"
        return "disputed" if as_of >= due + timedelta(days=15) else "open"
    if outcome == "disputed_unpaid":
        return "disputed" if as_of >= due + timedelta(days=15) else "open"
    return "written_off" if as_of >= due + timedelta(days=120) else "open"


def _gen_outflows(seed, bid, cfg, biz, start, months, as_of, mstarts):
    r = rng_for(seed, "biz", bid, "outflows")
    cps: list[CounterpartyRec] = []
    outs: list[OutflowRec] = []
    used: set[str] = set()

    def cp(kind: str, name: str, cur: str) -> str:
        cid = f"CP-{bid}-{len(cps) + 1:03d}"
        cps.append(CounterpartyRec(counterparty_id=cid, business_id=bid, kind=kind, name=name,
                                   currency=cur, added_date=start))
        return cid

    def push(cid, kind, issue, due, pay, cur, cents):
        outs.append(OutflowRec(
            outflow_id=f"OUT-{bid}-{len(outs) + 1:05d}", business_id=bid, counterparty_id=cid,
            kind=kind, issue_date=issue, due_date=due, pay_date=pay if pay <= as_of else None,
            currency=cur, amount=cents_to_decimal(cents),
        ))

    # payroll: INR, fixed day, 7% raise each April
    n_emp = _uniform_int(r, *cfg["n_emp"])
    salary = sum(float(r.uniform(*cfg["salary"])) for _ in range(n_emp))
    pid = cp("payroll", f"{biz.name.split(' (')[0]} Payroll", "INR")
    for m0 in mstarts:
        raises = (m0.year - start.year) + (1 if m0.month >= 4 else 0) - (1 if start.month >= 4 else 0)
        pay_d = roll_back(clamp_day(m0.year, m0.month, biz.payroll_day))
        push(pid, "payroll", pay_d, pay_d, pay_d, "INR", _cents(salary * 1.07 ** max(0, raises)))

    # suppliers: SGD / AUD, net-30, irregular
    for _ in range(_uniform_int(r, *cfg["n_sup"])):
        cur = "SGD" if r.random() < 0.5 else "AUD"
        sid = cp("supplier", _SUP[int(r.integers(len(_SUP)))] + f" {cur}", cur)
        med = float(math.exp(r.uniform(math.log(cfg["sup_med"][0]), math.log(cfg["sup_med"][1]))))
        for m0 in mstarts:
            for _k in range(int(r.poisson(cfg["sup_rate"]))):
                issue = roll_back(clamp_day(m0.year, m0.month, _uniform_int(r, 1, 28)))
                due = issue + timedelta(days=30)
                pay = roll_forward(due + timedelta(days=_uniform_int(r, 0, 4)))
                push(sid, "supplier_invoice", issue, due, pay, cur,
                     _cents(med * float(math.exp(r.normal(0, 0.35)))))

    # SaaS: recurring card spend, charged on a fixed day
    for _ in range(_uniform_int(r, *cfg["n_saas"])):
        cur = "EUR" if r.random() < 0.15 else "USD"
        vid = cp("saas_vendor", _SAAS[int(r.integers(len(_SAAS)))] + f" #{len(cps)}", cur)
        price = float(r.uniform(*cfg["saas"]))
        day = _uniform_int(r, 1, 28)
        step_month = _uniform_int(r, 3, max(4, len(mstarts) - 2)) if r.random() < 0.3 else None
        for mi, m0 in enumerate(mstarts):
            if step_month is not None and mi == step_month:
                price *= float(r.uniform(1.05, 1.25))
            d = clamp_day(m0.year, m0.month, day)
            push(vid, "saas_card", d, d, d, cur, _cents(price))
    return cps, outs


# -------------------------------------------------------------- parquet io ---
_SCHEMAS: dict[str, pa.Schema] = {}
_AMOUNT = pa.decimal128(16, 2)


def _arrow_type(name: str, sample) -> pa.DataType:
    if name == "amount":
        return _AMOUNT
    if name in {"days_past_due", "payroll_day", "payment_terms_days"}:
        return pa.int32()
    if name == "growth_rate":
        return pa.float64()
    if name.endswith("_date") or name in {"onboarded", "date"}:
        return pa.date32()
    return pa.string()


def _to_table(rows: list[Rec], model: type[Rec]) -> pa.Table:
    cols = {}
    for name in model.model_fields:
        vals = [getattr(r, name) for r in rows]
        cols[name] = pa.array(vals, type=_arrow_type(name, None))
    return pa.table(cols)


def _write(rows: list[Rec], model: type[Rec], path: Path) -> str:
    pq.write_table(_to_table(rows, model), path, compression="zstd")
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------- driver ---
def generate(
    seed: int, out_dir: Path = Path("data"), start: date = date(2025, 1, 1),
    months: int = 18, fx_source: str | None = None,
) -> dict:
    as_of = window_end(start, months)
    syn_dir = out_dir / "synthetic"
    syn_dir.mkdir(parents=True, exist_ok=True)

    archetypes = [a for a in ARCH for _ in range(N_PER_ARCHETYPE)]
    parts = [_gen_business(seed, i, a, start, months) for i, a in enumerate(archetypes)]
    tables = {
        "businesses": ([p[0] for p in parts], BusinessRec),
        "customers": ([x for p in parts for x in p[1]], CustomerRec),
        "invoices": ([x for p in parts for x in p[2]], InvoiceRec),
        "payments": ([x for p in parts for x in p[3]], PaymentRec),
        "ground_truth": ([x for p in parts for x in p[4]], GroundTruthRec),
        "counterparties": ([x for p in parts for x in p[5]], CounterpartyRec),
        "outflows": ([x for p in parts for x in p[6]], OutflowRec),
    }
    hashes = {name: _write(rows, model, syn_dir / f"{name}.parquet") for name, (rows, model) in tables.items()}

    fx = load_fx(start - timedelta(days=366), as_of, seed=seed, data_dir=out_dir, source=fx_source)
    manifest = {
        "generator_version": __version__, "seed": seed, "start": str(start), "months": months,
        "as_of": str(as_of), "fx_source": fx.source,
        "rows": {k: len(v[0]) for k, v in tables.items()},
        "sha256": hashes,
        "note": "ground_truth.parquet holds latent labels for evaluation only; never use as features.",
    }
    (syn_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tessera_evalkit.generate",
                                description="Generate deterministic synthetic SME data + FX history.")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", type=Path, default=Path("data"), help="data root (default: ./data)")
    p.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 1))
    p.add_argument("--months", type=int, default=18)
    p.add_argument("--fx-source", choices=["auto", "ecb", "synthetic"], default=None,
                   help="overrides TESSERA_FX_SOURCE (default auto)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    m = generate(a.seed, a.out, a.start, a.months, a.fx_source)
    print(json.dumps({k: m[k] for k in ("seed", "as_of", "fx_source", "rows")}, indent=2))
    if m["fx_source"] == "synthetic":
        print("WARNING: FX series is SYNTHETIC (see data/fx/fx_meta.json).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
