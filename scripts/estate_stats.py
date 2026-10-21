#!/usr/bin/env python3
"""Sanity check that the estate files are present and readable."""
import csv, glob, json

cfg = json.load(open("task2_estate/transfer_configs.json"))["transferConfigs"]
hist = list(csv.DictReader(open("task2_estate/run_history.csv")))
usage = list(csv.DictReader(open("task2_estate/downstream_usage.csv")))
cat = list(csv.DictReader(open("task2_estate/table_catalog.csv")))

print(f"{len(glob.glob('task2_estate/scheduled_queries/*.sql')):>6} sql files")
print(f"{len(cfg):>6} transfer configs ({sum(c['state'] == 'ENABLED' for c in cfg)} enabled)")
print(f"{len(hist):>6} run-history rows ({sum(r['state'] == 'FAILED' for r in hist)} failed)")
print(f"{len(cat):>6} tables in the catalog")
print(f"{len(usage):>6} downstream-usage rows")
from collections import defaultdict

by_cfg_bytes, by_cfg_days = defaultdict(int), defaultdict(set)
for r in hist:
    by_cfg_bytes[r["config_id"]] += int(r["total_bytes_processed"])
    by_cfg_days[r["config_id"]].add(r["schedule_time"][:10])
tib_day = sum(by_cfg_bytes[c] / len(by_cfg_days[c]) for c in by_cfg_bytes) / 1024 ** 4
print(f"{tib_day:>6.1f} TiB scanned per day across the estate "
      f"(~${tib_day * 6.25:,.0f}/day at on-demand pricing)")
