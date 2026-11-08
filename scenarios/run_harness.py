"""T2 harness (Step 8): scenarios 1 to 12, Hub vs latest-value-wins. No LLM calls.

Usage: python -m scenarios.run_harness

Runs on its own database, context_hub_harness, created if missing and reset before every
scenario, so the demo database is never touched. Each scenario's steps go to both backends;
then each answers the probe from its own context. Predictions are read from the table in
docs/SCENARIOS.md, and every cell that differs is marked and must carry a one-line reason.
Writes results/t2.csv.
"""
import csv
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backends.lww import LABEL, LwwBackend
from backends.structured import HubBackend
from scenarios.t2 import SCENARIOS
from scenarios.timeline import START
from scripts.reset_db import reset

ROOT = Path(__file__).resolve().parent.parent
HARNESS_DB = "context_hub_harness"
OUT = ROOT / "results" / "t2.csv"
THRESHOLD = timedelta(minutes=15)

# One line per cell where the measured result differs from the SCENARIOS.md prediction.
DIFFERENCES = {
    (3, "LWW"): "The baseline is fed the same dialler records; its latest callback_status is attempted_no_answer, "
                "which answers the probe. The prediction assumed no dialler feed.",
    (10, "LWW"): "Both backends fetch live facts from stored references at read time; the baseline stores the case's "
                 "dispute_ref, so it gets the live status too. The prediction assumed it reads a copied status.",
}


def predictions() -> dict[int, tuple[str, str]]:
    """{scenario: (hub, lww)} from the SCENARIOS.md table column 'Predicted S / SUM / LWW'."""
    out = {}
    for line in (ROOT / "docs" / "SCENARIOS.md").read_text().splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|.*\|\s*(Pass|Fail)\s*/\s*(?:Pass|Fail|n\.a\.)\s*/\s*(Pass|Fail)", line)
        if m:
            out[int(m.group(1))] = (m.group(2), m.group(3))
    return out


def probes() -> dict[int, tuple[str, str]]:
    out = {}
    for line in (ROOT / "docs" / "SCENARIOS.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit():
            out[int(cells[0])] = (cells[2], cells[3])
    return out


def ensure_database(admin_url: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (HARNESS_DB,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(HARNESS_DB)))


def run(admin_url: str, app_url: str) -> list[dict]:
    admin = make_conninfo(admin_url, dbname=HARNESS_DB)
    app = make_conninfo(app_url, dbname=HARNESS_DB)
    ensure_database(admin_url)
    predicted, probe_text = predictions(), probes()
    rows = []
    for scenario in SCENARIOS:
        reset(admin)  # every scenario starts from the seed: no scenario depends on another
        hub, lww = HubBackend(app, START, THRESHOLD), LwwBackend(admin, START)
        try:
            for step in scenario.steps:
                hub.apply(step)
                lww.apply(step)
            hub_ok, hub_says = scenario.check_hub(hub.context(scenario.role))
            lww_ok, lww_says = scenario.check_lww(lww.context(scenario.role))
        finally:
            hub.close()
            lww.close()
        n = scenario.number
        hub_res, lww_res = ("Pass" if hub_ok else "Fail"), ("Pass" if lww_ok else "Fail")
        notes = [f"{name}: {DIFFERENCES.get((n, name), 'UNEXPLAINED difference')}"
                 for name, res, pred in (("Hub", hub_res, predicted[n][0]), ("LWW", lww_res, predicted[n][1]))
                 if res != pred]
        rows.append({"scenario": n, "probe": probe_text[n][0], "correct_context": probe_text[n][1],
                     "hub": hub_res, "hub_predicted": predicted[n][0], "lww": lww_res, "lww_predicted": predicted[n][1],
                     "hub_context_says": hub_says, "lww_context_says": lww_says, "difference_note": " ".join(notes)})
    return rows


def render(rows: list[dict]) -> list[str]:
    def cell(res, pred):
        return f"{res} *" if res != pred else res
    lines = [f"T2: Hub vs {LABEL}. Scenarios 1 to 12 from docs/SCENARIOS.md; deterministic probes, no LLM.",
             "* = differs from the prediction in docs/SCENARIOS.md", "",
             f"{'#':>2}  {'Probe':<44} {'Hub':<7} {'LWW':<7} LWW context says"]
    for r in rows:
        lines.append(f"{r['scenario']:>2}  {r['probe'][:44]:<44} {cell(r['hub'], r['hub_predicted']):<7} "
                     f"{cell(r['lww'], r['lww_predicted']):<7} {r['lww_context_says']}")
    hub_n, lww_n = sum(r["hub"] == "Pass" for r in rows), sum(r["lww"] == "Pass" for r in rows)
    hub_p, lww_p = sum(r["hub_predicted"] == "Pass" for r in rows), sum(r["lww_predicted"] == "Pass" for r in rows)
    lines += ["", f"Measured: Hub {hub_n}/12, LWW {lww_n}/12.   Predicted: Hub {hub_p}/12, LWW {lww_p}/12.", ""]
    lines += [f"  #{r['scenario']} {r['difference_note']}" for r in rows if r["difference_note"]]
    return lines


def main() -> None:
    load_dotenv()
    rows = run(os.environ["DATABASE_URL_ADMIN"], os.environ["DATABASE_URL_APP"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(render(rows)))
    print(f"\nWritten to {OUT.relative_to(ROOT)}")
    sys.exit(1 if any("UNEXPLAINED" in r["difference_note"] for r in rows) else 0)


if __name__ == "__main__":
    main()
