#!/usr/bin/env python3
"""FAST stage-1 lever screen, stacked on our current #1 (A=393, WM=25).

For EVERY tunable numeric lever, both directions, in ONE parallel /batch
(<=100 configs, 1 reroll each, COUNT_ALL probe). One reroll pins the score
exactly (T/qubits are deterministic per config); the 1-sample any_fail is a
rough rarity hint only. Ranks by score. Only score-improving configs are
worth a stage-2 failure-rate sweep + /search density.
"""
import os
import time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"      # fork dev + probe (COUNT_ALL) evaluator
CUR_BEST = 2381382450               # our promoted #1 (A=393, WM=25, f664d6f)
REROLL = "7"                        # fixed -> identical test inputs across configs

# committed defaults (configure_ecdsafail_submission_route); we run A=393/WM=25
A   = "DIALOG_GCD_ACTIVE_ITERATIONS"          # def 394, cur 393
WM  = "DIALOG_GCD_WIDTH_MARGIN"               # def 26,  cur 25
SL  = "DIALOG_GCD_WIDTH_SLOPE_X1000"          # def 711
SCH = "DIALOG_GCD_PA9024_COMPARE_SCHEDULE_MARGIN"  # def 6
ACB = "DIALOG_GCD_APPLY_CLEAN_COMPARE_BITS"   # def 20
CB  = "DIALOG_GCD_COMPARE_BITS"               # def 56
KDW = "KAL_DOUBLE_CARRY_TRUNC_W"              # def 20
KFW = "KAL_FOLD_CARRY_TRUNC_W"                # def 20
AWB = "DIALOG_GCD_APPLY_WINDOW_BLOCKS"        # def 2
AFB = "DIALOG_GCD_APPLY_CHUNKED_F_BLOCKS"     # def 4

BASE_ENV = {A: "393", WM: "25"}     # our current #1

# each lever, both directions from its committed default
CONFIGS = {"base (A393 WM25)": {}}
def add(tag, key, vals):
    for v in vals:
        CONFIGS[f"{tag}{v}"] = {key: str(v)}

add("A=",   A,   [391, 392, 394, 395])
add("WM=",  WM,  [23, 24, 26, 27])
add("SL=",  SL,  [708, 709, 710, 712, 713, 714])
add("SCH=", SCH, [4, 5, 7, 8])
add("ACB=", ACB, [18, 19, 21, 22])
add("CB=",  CB,  [54, 55, 57, 58])
add("KDW=", KDW, [18, 19, 21, 22])
add("KFW=", KFW, [18, 19, 21, 22])
add("AWB=", AWB, [1, 3])
add("AFB=", AFB, [3, 5])

names = list(CONFIGS)
assert len(names) <= 100, len(names)


def post(ep, body):
    return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=120).json()


def parse_probe(out):
    line = next((l for l in out.splitlines() if l.startswith("PROBE")), None)
    return dict(kv.split("=") for kv in line.split()[1:]) if line else None


runs = [{"env_vars": {"COUNT_ALL_FAILURES": "1", "DIALOG_REROLL": REROLL,
                      **BASE_ENV, **CONFIGS[n]}} for n in names]
r = post("batch", {"build_key": BUILD_KEY, "runs": runs})
ids = dict(zip(names, r["job_ids"]))
print(f"launched {len(names)} score probes; polling...")

t0 = time.time()
while True:
    res = post("poll", {"job_ids": list(ids.values())})["results"]
    if all(v.get("status") in ("completed", "error") for v in res.values()):
        break
    time.sleep(3)
print(f"done in {time.time()-t0:.0f}s\n")

rows = []
for n in names:
    j = res[ids[n]]
    if j.get("status") != "completed":
        print(f"{n}: {j.get('status')} {j.get('error','')[:60]}")
        continue
    d = parse_probe(j["output"])
    if not d:
        print(f"{n}: no PROBE line; exit={j.get('exit_code')}")
        continue
    T = int(float(d["toffoli"])); q = int(float(d["qubits"]))
    score = int(d["score"]); af = int(d.get("any_fail", -1))
    rows.append((n, T, q, score, 100.0*(score-CUR_BEST)/CUR_BEST, af))

rows.sort(key=lambda x: x[3])
print(f"{'config':18s} {'T':>11s} {'q':>5s} {'score':>14s} {'dScore%':>8s} {'f(1smpl)':>8s}")
for (n, T, q, score, ds, af) in rows:
    flag = " <-- beats #1" if score < CUR_BEST else ""
    print(f"{n:18s} {T:>11,} {q:>5d} {score:>14,} {ds:>+7.3f}% {af:>8d}{flag}")
print(f"\n(current #1 = {CUR_BEST:,}; negative dScore% = lower score = better)")
