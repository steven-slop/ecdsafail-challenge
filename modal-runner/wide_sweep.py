#!/usr/bin/env python3
"""Wide failure-rate net stacked on the live active=394 baseline.

Per config:
  - p_hat from one packed /sweep over K rerolls (no island stop).
  - T/q/score from one /run probe (constant across rerolls).
exp_rerolls = e^(9024 * p_hat). Ranks by score vs reroll budget vs current #1.
"""
import os
import math, time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"   # c3c3f8d + probe evaluator
K = 24                            # rerolls sampled per config
CUR_BEST = 2399448905             # our promoted #1 (active=394)

A = "DIALOG_GCD_ACTIVE_ITERATIONS"
WM = "DIALOG_GCD_WIDTH_MARGIN"
SL = "DIALOG_GCD_WIDTH_SLOPE_X1000"
SCH = "DIALOG_GCD_PA9024_COMPARE_SCHEDULE_MARGIN"
ACB = "DIALOG_GCD_APPLY_CLEAN_COMPARE_BITS"
CB = "DIALOG_GCD_COMPARE_BITS"

BASE_ENV = {A: "394"}             # every config stacks on active=394
CONFIGS = {
    "active394 (base)":          {},
    "active393":                 {A: "393"},
    "wm25":                      {WM: "25"},
    "wm24":                      {WM: "24"},
    "slope712":                  {SL: "712"},
    "slope713":                  {SL: "713"},
    "slope715":                  {SL: "715"},
    "sched5":                    {SCH: "5"},
    "applycb19":                 {ACB: "19"},
    "cb55":                      {CB: "55"},
    "active393+wm25":            {A: "393", WM: "25"},
    "wm25+slope712":             {WM: "25", SL: "712"},
    "active393+slope712":        {A: "393", SL: "712"},
    "wm25+applycb19":            {WM: "25", ACB: "19"},
    "slope712+applycb19":        {SL: "712", ACB: "19"},
    "wm25+sched5":               {WM: "25", SCH: "5"},
    "active393+wm25+slope712":   {A: "393", WM: "25", SL: "712"},
    "wm25+slope712+applycb19":   {WM: "25", SL: "712", ACB: "19"},
}


def parse_probe(out):
    line = next(l for l in out.splitlines() if l.startswith("PROBE"))
    return dict(kv.split("=") for kv in line.split()[1:])


def post(ep, body):
    return requests.post(BASE.format(ep), json={"token": TOKEN, **body}).json()


def poll(job_ids):
    while True:
        res = post("poll", {"job_ids": job_ids})["results"]
        if all(v.get("status") in ("completed", "error") for v in res.values()):
            return res
        time.sleep(3)


# Phase A: one packed sweep per config (K rerolls, no island stop)
sweep_ids = {}
for name, ev in CONFIGS.items():
    r = post("sweep", {"build_key": BUILD_KEY, "env_vars": {**BASE_ENV, **ev},
                       "rerolls": list(range(1, K + 1)), "per_container": K,
                       "stop_on_island": False})
    sweep_ids[name] = r["job_ids"][0]

# Phase B: one single probe per config for T/q/score
probe_runs = [{"env_vars": {"COUNT_ALL_FAILURES": "1", "DIALOG_REROLL": "7",
                            **BASE_ENV, **ev}} for ev in CONFIGS.values()]
pb = post("batch", {"build_key": BUILD_KEY, "runs": probe_runs})
probe_ids = dict(zip(CONFIGS.keys(), pb["job_ids"]))

print(f"launched {len(CONFIGS)} sweeps (K={K}) + {len(CONFIGS)} probes; polling...")
t0 = time.time()
sres = poll(list(sweep_ids.values()))
pres = poll(list(probe_ids.values()))
print(f"done in {time.time()-t0:.0f}s")

rows = []
for name in CONFIGS:
    s = sres[sweep_ids[name]]
    p_run = pres[probe_ids[name]]
    if s.get("status") != "completed" or p_run.get("status") != "completed":
        print(f"{name}: incomplete s={s.get('status')} p={p_run.get('status')}")
        continue
    tot = s["total_failures"]
    n_eval = s["n_evaluated"]
    fs = [x["any_fail"] for x in s["sweep"] if "any_fail" in x]
    isl = sum(1 for f in fs if f == 0)
    p = tot / (n_eval * 9024)
    exp = math.inf if p >= 0.0085 else math.exp(9024 * p)
    d = parse_probe(p_run["output"])
    T = int(float(d["toffoli"])); score = int(d["score"])
    rows.append((name, T, score, 100.0 * (score - CUR_BEST) / CUR_BEST,
                 tot / n_eval, p, exp, min(fs), max(fs), isl, n_eval))

rows.sort(key=lambda r: r[2])  # by score asc (best first)
print(f"\n{'config':26s} {'T':>11s} {'score':>14s} {'dScore%':>8s} {'avg_f':>6s} "
      f"{'p_hat':>9s} {'exp_rerolls':>14s} {'f_rng':>7s} {'isl':>4s}")
for (name, T, score, ds, af, p, exp, fmin, fmax, isl, nev) in rows:
    print(f"{name:26s} {T:>11,} {score:>14,} {ds:>+7.3f}% {af:>6.1f} "
          f"{p:>9.2e} {exp:>14,.0f} {fmin:>3d}-{fmax:<3d} {isl:>4d}")
print(f"\n(current #1 = {CUR_BEST:,}; negative dScore% beats it; K={K} rerolls/config)")
