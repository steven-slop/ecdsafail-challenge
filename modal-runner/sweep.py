#!/usr/bin/env python3
"""Failure-rate sweep: for each config, sample R rerolls in parallel, average f.

p_hat = total_failures / (R * 9024); island budget = e^(9024 * p_hat).
Also reports empirical islands (rerolls with f==0) seen in the sample.
"""
import os
import math, time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"
REROLLS = list(range(1, 13))  # 12 distinct streams

CONFIGS = {
    "baseline":              {},
    "active_iters=394":      {"DIALOG_GCD_ACTIVE_ITERATIONS": "394"},
    "active_iters=393":      {"DIALOG_GCD_ACTIVE_ITERATIONS": "393"},
    "width_margin=25":       {"DIALOG_GCD_WIDTH_MARGIN": "25"},
    "sched_margin=5":        {"DIALOG_GCD_PA9024_COMPARE_SCHEDULE_MARGIN": "5"},
    "active394+wm25":        {"DIALOG_GCD_ACTIVE_ITERATIONS": "394", "DIALOG_GCD_WIDTH_MARGIN": "25"},
}


def parse_probe(out):
    line = next(l for l in out.splitlines() if l.startswith("PROBE"))
    d = dict(kv.split("=") for kv in line.split()[1:])
    return d


def batch(runs):
    r = requests.post(BASE.format("batch"), json={"token": TOKEN, "build_key": BUILD_KEY, "runs": runs}).json()
    return r["job_ids"]


def poll(job_ids):
    while True:
        res = requests.post(BASE.format("poll"), json={"token": TOKEN, "job_ids": job_ids}).json()["results"]
        if all(v.get("status") in ("completed", "error") for v in res.values()):
            return res
        time.sleep(3)


# Build one big batch: every (config, reroll) pair
specs, index = [], []
for name, ev in CONFIGS.items():
    for rr in REROLLS:
        specs.append({"env_vars": {"COUNT_ALL_FAILURES": "1", "DIALOG_REROLL": str(rr), **ev}})
        index.append((name, rr))

print(f"launching {len(specs)} runs ({len(CONFIGS)} configs x {len(REROLLS)} rerolls)...")
ids = batch(specs)
res = poll(ids)

agg = {}
for jid, (name, rr) in zip(ids, index):
    r = res[jid]
    if r.get("status") != "completed":
        continue
    d = parse_probe(r["output"])
    a = agg.setdefault(name, {"any": 0, "cl": 0, "ph": 0, "anc": 0, "n": 0, "islands": 0,
                              "runs": 0, "fs": [], "T": d["toffoli"], "q": d["qubits"], "score": d["score"]})
    f = int(d["any_fail"]); n = int(d["n"])
    a["any"] += f; a["cl"] += int(d["classical"]); a["ph"] += int(d["phase"]); a["anc"] += int(d["ancilla"])
    a["n"] += n; a["runs"] += 1; a["fs"].append(f)
    if f == 0: a["islands"] += 1

print(f"\n{'config':18s} {'T':>11s} {'dScore%':>8s} {'avg_f':>6s} {'cl/ph/anc':>12s} "
      f"{'p_hat':>9s} {'exp_rerolls':>13s} {'f range':>9s}")
BASE_SCORE = 2402529905
for name in CONFIGS:
    a = agg.get(name)
    if not a or a["runs"] == 0:
        print(f"{name:18s}  (no data)"); continue
    p = a["any"] / a["n"]
    exp = math.inf if p >= 0.077 else math.exp(9024 * p)
    avg_f = a["any"] / a["runs"]
    dscore = 100.0 * (int(a["score"]) - BASE_SCORE) / BASE_SCORE
    clpa = f"{a['cl']/a['runs']:.1f}/{a['ph']/a['runs']:.1f}/{a['anc']/a['runs']:.1f}"
    frange = f"{min(a['fs'])}-{max(a['fs'])}"
    print(f"{name:18s} {int(float(a['T'])):>11,} {dscore:>+7.3f}% {avg_f:>6.1f} {clpa:>12s} "
          f"{p:>9.2e} {exp:>13,.0f} {frange:>9s}")
