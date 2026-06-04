#!/usr/bin/env python3
"""Stage 2: do the score-improving levers STACK, and what's each one's island rarity?

- Score probe (1 reroll) for cumulative greedy stacks + the top singles.
- Failure-rate sweep (K rerolls) for each, no island stop -> p_hat.
exp_rerolls = e^(9024*p_hat) is the OLD upper-bound screen; real /search density
runs ~50-100x cheaper, so divide by ~75 for a realistic central cost guess.
"""
import os
import math, time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"
CUR_BEST = 2381382450
K = 48                              # rerolls/config for p_hat
REROLL = "7"

A="DIALOG_GCD_ACTIVE_ITERATIONS"; WM="DIALOG_GCD_WIDTH_MARGIN"
SL="DIALOG_GCD_WIDTH_SLOPE_X1000"; CB="DIALOG_GCD_COMPARE_BITS"
ACB="DIALOG_GCD_APPLY_CLEAN_COMPARE_BITS"
KDW="KAL_DOUBLE_CARRY_TRUNC_W"; KFW="KAL_FOLD_CARRY_TRUNC_W"
BASE_ENV = {A: "393", WM: "25"}

# top singles worth a real p_hat
CONFIGS = {
    "base (A393WM25)": {},
    "WM=24":           {WM: "24"},
    "WM=23":           {WM: "23"},
    "A=392":           {A: "392"},
    "A=391":           {A: "391"},
    "SL=714":          {SL: "714"},
    "CB=54":           {CB: "54"},
    "KDW=18":          {KDW: "18"},
}
# cumulative greedy stack (biggest score gains first), all keep q=1350 hopefully
STACK = [
    ("s1 WM24",                 {WM:"24"}),
    ("s2 +A392",                {WM:"24", A:"392"}),
    ("s3 +SL714",               {WM:"24", A:"392", SL:"714"}),
    ("s4 +CB54",                {WM:"24", A:"392", SL:"714", CB:"54"}),
    ("s5 +KDW18+ACB18+KFW18",   {WM:"24", A:"392", SL:"714", CB:"54", KDW:"18", ACB:"18", KFW:"18"}),
    ("x WM23 stack",            {WM:"23", A:"392", SL:"714", CB:"54", KDW:"18", ACB:"18", KFW:"18"}),
]
for tag, ev in STACK:
    CONFIGS[tag] = ev

names = list(CONFIGS)


def post(ep, body):
    return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=120).json()

def parse_probe(out):
    line = next((l for l in out.splitlines() if l.startswith("PROBE")), None)
    return dict(kv.split("=") for kv in line.split()[1:]) if line else None

def poll(job_ids):
    while True:
        res = post("poll", {"job_ids": job_ids})["results"]
        if all(v.get("status") in ("completed","error") for v in res.values()):
            return res
        time.sleep(3)

# score probes
pruns = [{"env_vars": {"COUNT_ALL_FAILURES":"1","DIALOG_REROLL":REROLL, **BASE_ENV, **CONFIGS[n]}} for n in names]
pid = dict(zip(names, post("batch", {"build_key": BUILD_KEY, "runs": pruns})["job_ids"]))
# failure-rate sweeps (K rerolls, no stop)
sid = {}
for n in names:
    r = post("sweep", {"build_key": BUILD_KEY, "env_vars": {**BASE_ENV, **CONFIGS[n]},
                       "rerolls": list(range(1, K+1)), "per_container": K, "stop_on_island": False})
    sid[n] = r["job_ids"][0]

print(f"launched {len(names)} probes + {len(names)} sweeps (K={K}); polling...")
t0=time.time()
pres = poll(list(pid.values())); sres = poll(list(sid.values()))
print(f"done in {time.time()-t0:.0f}s\n")

rows=[]
for n in names:
    p=pres[pid[n]]; s=sres[sid[n]]
    if p.get("status")!="completed" or s.get("status")!="completed":
        print(f"{n}: p={p.get('status')} s={s.get('status')}"); continue
    d=parse_probe(p["output"])
    if not d: print(f"{n}: no PROBE"); continue
    T=int(float(d["toffoli"])); q=int(float(d["qubits"])); score=int(d["score"])
    tot=s["total_failures"]; nev=s["n_evaluated"]
    fs=[x["any_fail"] for x in s["sweep"] if "any_fail" in x]
    ph=tot/(nev*9024); exp=math.inf if ph>=0.0085 else math.exp(9024*ph)
    real=exp/75.0                  # ~50-100x ground-truth correction
    rows.append((n,T,q,score,100.0*(score-CUR_BEST)/CUR_BEST,ph,exp,real,min(fs),max(fs),sum(1 for f in fs if f==0)))

rows.sort(key=lambda x:x[3])
print(f"{'config':24s} {'T':>11s} {'q':>5s} {'score':>14s} {'dScr%':>7s} {'p_hat':>9s} {'exp_rr':>11s} {'~real_rr':>10s} {'f_rng':>7s}")
for (n,T,q,score,ds,ph,exp,real,fmn,fmx,isl) in rows:
    er = "inf" if exp==math.inf else f"{exp:,.0f}"
    rr = "inf" if real==math.inf else f"{real:,.0f}"
    print(f"{n:24s} {T:>11,} {q:>5d} {score:>14,} {ds:>+6.3f}% {ph:>9.2e} {er:>11s} {rr:>10s} {fmn:>3d}-{fmx:<3d}")
print(f"\n(#1={CUR_BEST:,}; ~real_rr = exp_rr/75 ground-truth-corrected; q must stay 1350)")
