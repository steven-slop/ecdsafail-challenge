#!/usr/bin/env python3
"""Wider both-direction lever scan on the active=394 base (our promoted #1).

For every numeric lever we probe BOTH down and up (not just decrements), plus a
few structural boolean toggles (e.g. enable Karatsuba xtail). Each candidate:
  - p_hat from one packed /sweep over K rerolls (count_all, no island stop)
  - exact T/q/score from one /run probe (deterministic across rerolls)
exp_rerolls = e^(9024*p_hat); cost ~ exp_rerolls * $2e-5 (optimized search binary).

Outputs: full table sorted by score, the Pareto frontier (score vs exp_rerolls),
and a cost-aware utility so we can hill-climb from the best improving move.
"""
import os
import math, time, requests, json, sys

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"   # c3c3f8d + count_all probe evaluator
K = 24
CUR_BEST = 2399448905            # promoted #1 (active=394)
REF = 1.086e10                   # leaderboard DIFF% denominator

A   = "DIALOG_GCD_ACTIVE_ITERATIONS"          # 394
WM  = "DIALOG_GCD_WIDTH_MARGIN"               # 26
SL  = "DIALOG_GCD_WIDTH_SLOPE_X1000"          # 711
SCH = "DIALOG_GCD_PA9024_COMPARE_SCHEDULE_MARGIN"  # 6
CB  = "DIALOG_GCD_COMPARE_BITS"               # 56
ACB = "DIALOG_GCD_APPLY_CLEAN_COMPARE_BITS"   # 20
KD  = "KAL_DOUBLE_CARRY_TRUNC_W"              # 20
KF  = "KAL_FOLD_CARRY_TRUNC_W"                # 20
AWB = "DIALOG_GCD_APPLY_WINDOW_BLOCKS"        # 2
AFB = "DIALOG_GCD_APPLY_CHUNKED_F_BLOCKS"     # 4
KARA = "ROUND84_XTAIL_KARATSUBA"              # 0 (disabled)

BASE_ENV = {A: "394"}
CONFIGS = {
    "active394 (base)":      {},
    # ACTIVE_ITERATIONS both ways
    "A=393":                 {A: "393"},
    "A=392":                 {A: "392"},
    "A=395 (revert)":        {A: "395"},
    # WIDTH_MARGIN both ways (affects qubits)
    "WM=25":                 {WM: "25"},
    "WM=24":                 {WM: "24"},
    "WM=27 (up)":            {WM: "27"},
    # WIDTH_SLOPE both ways
    "SL=710":                {SL: "710"},
    "SL=712":                {SL: "712"},
    "SL=713":                {SL: "713"},
    "SL=709 (down)":         {SL: "709"},
    # SCHEDULE_MARGIN both ways
    "SCH=5":                 {SCH: "5"},
    "SCH=7 (up)":            {SCH: "7"},
    # COMPARE_BITS both ways
    "CB=55":                 {CB: "55"},
    "CB=57 (up)":            {CB: "57"},
    # APPLY_CLEAN_COMPARE_BITS both ways
    "ACB=19":                {ACB: "19"},
    "ACB=21 (up)":           {ACB: "21"},
    "ACB=18":                {ACB: "18"},
    # KAL carry trunc widths both ways
    "KD=19":                 {KD: "19"},
    "KD=21 (up)":            {KD: "21"},
    "KF=19":                 {KF: "19"},
    "KF=21 (up)":            {KF: "21"},
    # apply window / chunk blocks both ways
    "AWB=1":                 {AWB: "1"},
    "AWB=3 (up)":            {AWB: "3"},
    "AFB=3":                 {AFB: "3"},
    "AFB=5 (up)":            {AFB: "5"},
    # structural toggle: enable Karatsuba xtail (Toffoli-saving multiply)
    "KARA=1 (enable)":       {KARA: "1"},
    # a couple combos around the known-good direction
    "A=393+WM=25":           {A: "393", WM: "25"},
    "A=392+WM=25":           {A: "392", WM: "25"},
    "A=393+WM=24":           {A: "393", WM: "24"},
}


def parse_probe(out):
    line = next(l for l in out.splitlines() if l.startswith("PROBE"))
    return dict(kv.split("=") for kv in line.split()[1:])


def post(ep, body, tries=4):
    last = None
    for _ in range(tries):
        try:
            return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=180).json()
        except Exception as e:
            last = e; time.sleep(3)
    raise last


def poll(job_ids, label=""):
    t0 = time.time()
    while True:
        res = post("poll", {"job_ids": job_ids})["results"]
        done = sum(1 for v in res.values() if v.get("status") in ("completed", "error"))
        if done == len(job_ids):
            return res
        if int(time.time() - t0) % 15 < 4:
            print(f"  {label}: {done}/{len(job_ids)} done", flush=True)
        time.sleep(4)


sweep_ids = {}
for name, ev in CONFIGS.items():
    r = post("sweep", {"build_key": BUILD_KEY, "env_vars": {**BASE_ENV, **ev},
                       "rerolls": list(range(1, K + 1)), "per_container": K,
                       "stop_on_island": False})
    sweep_ids[name] = r["job_ids"][0]

probe_runs = [{"env_vars": {"COUNT_ALL_FAILURES": "1", "DIALOG_REROLL": "7",
                            **BASE_ENV, **ev}} for ev in CONFIGS.values()]
pb = post("batch", {"build_key": BUILD_KEY, "runs": probe_runs})
probe_ids = dict(zip(CONFIGS.keys(), pb["job_ids"]))

print(f"launched {len(CONFIGS)} sweeps (K={K}) + {len(CONFIGS)} probes; polling...", flush=True)
t0 = time.time()
sres = poll(list(sweep_ids.values()), "sweeps")
pres = poll(list(probe_ids.values()), "probes")
print(f"done in {time.time()-t0:.0f}s", flush=True)

rows = []
for name in CONFIGS:
    s = sres[sweep_ids[name]]; p_run = pres[probe_ids[name]]
    if s.get("status") != "completed" or p_run.get("status") != "completed":
        print(f"{name}: incomplete s={s.get('status')} p={p_run.get('status')}")
        continue
    tot = s["total_failures"]; n_eval = s["n_evaluated"]
    fs = [x["any_fail"] for x in s["sweep"] if "any_fail" in x]
    isl = sum(1 for f in fs if f == 0)
    p = tot / (n_eval * 9024)
    exp = math.inf if p >= 0.0085 else math.exp(9024 * p)
    d = parse_probe(p_run["output"])
    T = int(float(d["toffoli"])); score = int(d["score"])
    q = int(d["qubits"]) if "qubits" in d else round(score / T)
    cost = exp * 2e-5
    rows.append(dict(name=name, T=T, q=q, score=score,
                     dscore=score - CUR_BEST, diffpct=100.0*(score-CUR_BEST)/REF,
                     avg_f=tot/n_eval, p=p, exp=exp, cost=cost,
                     fmin=min(fs), fmax=max(fs), isl=isl))

# Pareto frontier: non-dominated in (score lower better, exp lower better)
def dominated(r, others):
    for o in others:
        if o is r: continue
        if o["score"] <= r["score"] and o["exp"] <= r["exp"] and (
           o["score"] < r["score"] or o["exp"] < r["exp"]):
            return True
    return False
for r in rows:
    r["pareto"] = not dominated(r, rows)

rows.sort(key=lambda r: r["score"])
print(f"\n{'config':20s} {'T':>11s} {'q':>5s} {'score':>14s} {'lb%':>7s} {'avg_f':>6s} "
      f"{'p_hat':>9s} {'exp_rerolls':>13s} {'~$':>8s} {'f_rng':>7s} {'P':>2s}")
for r in rows:
    exp_s = "inf" if r["exp"] == math.inf else f"{r['exp']:,.0f}"
    cost_s = "inf" if r["cost"] == math.inf else f"{r['cost']:,.0f}"
    flag = "*" if r["pareto"] else ""
    print(f"{r['name']:20s} {r['T']:>11,} {r['q']:>5d} {r['score']:>14,} "
          f"{r['diffpct']:>+6.3f}% {r['avg_f']:>6.1f} {r['p']:>9.2e} {exp_s:>13s} "
          f"{cost_s:>8s} {r['fmin']:>3d}-{r['fmax']:<3d} {flag:>2s}")

print(f"\nPARETO FRONTIER (non-dominated, score vs exp_rerolls):")
for r in sorted([x for x in rows if x["pareto"]], key=lambda r: r["score"]):
    exp_s = "inf" if r["exp"] == math.inf else f"{r['exp']:,.0f}"
    print(f"  {r['name']:20s} score={r['score']:,} lb={r['diffpct']:+.3f}% "
          f"exp={exp_s} ~${r['cost']:,.0f}" if r['cost']!=math.inf else
          f"  {r['name']:20s} score={r['score']:,} lb={r['diffpct']:+.3f}% exp=inf")

with open("/home/ubuntu/ecdsafail-runner/explore_result.json", "w") as f:
    json.dump([{k: (None if v==math.inf else v) for k,v in r.items()} for r in rows], f, indent=2)
print(f"\n(current #1 = {CUR_BEST:,}; lb% = (score-#1)/{REF:.3e}; K={K}; ~$ = exp*2e-5)")
