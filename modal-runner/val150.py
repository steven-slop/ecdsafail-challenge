#!/usr/bin/env python3
"""K=150 validation of the configs that still beat our NEW #1 (A=393+WM=25 = 2,381,382,450).
Fan out 150 rerolls across containers for a tight p_hat, then real cost/time."""
import os
import math, time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"
A   = "DIALOG_GCD_ACTIVE_ITERATIONS"
WM  = "DIALOG_GCD_WIDTH_MARGIN"
NEW_BEST = 2381382450
REF = 1.086e10
K = 150
PER = 10  # rerolls/container -> 15 containers per config

CONFIGS = {
    "A=393+WM=25 (current #1)": {A: "393", WM: "25"},
    "A=392+WM=25":              {A: "392", WM: "25"},
    "A=393+WM=24":              {A: "393", WM: "24"},
}

def post(ep, body, tries=5, timeout=180):
    last = None
    for _ in range(tries):
        try:
            return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=timeout).json()
        except Exception as e:
            last = e; time.sleep(3)
    raise last

job_map = {}
for name, ev in CONFIGS.items():
    r = post("sweep", {"build_key": BUILD_KEY, "env_vars": ev,
                       "rerolls": list(range(1, K + 1)), "per_container": PER,
                       "stop_on_island": False})
    job_map[name] = r["job_ids"]
    print(f"launched {name}: {len(r['job_ids'])} containers", flush=True)

all_ids = [j for ids in job_map.values() for j in ids]
t0 = time.time()
while True:
    res = post("poll", {"job_ids": all_ids})["results"]
    done = sum(1 for v in res.values() if v.get("status") in ("completed", "error"))
    if done == len(all_ids):
        break
    if int(time.time() - t0) % 12 < 4:
        print(f"  {done}/{len(all_ids)} done", flush=True)
    time.sleep(4)

print(f"\n{'config':28s} {'n':>5s} {'tot_f':>6s} {'avg_f':>6s} {'p_hat':>10s} "
      f"{'exp_rerolls':>15s} {'~$':>10s} {'time@1000':>10s}")
for name in CONFIGS:
    tot = n = 0
    for jid in job_map[name]:
        s = res[jid]
        if s.get("status") != "completed":
            print(f"{name}: job {jid} {s.get('status')}"); continue
        tot += s["total_failures"]; n += s["n_evaluated"]
    p = tot / (n * 9024)
    exp = math.inf if p >= 0.0085 else math.exp(9024 * p)
    cost = exp * 2e-5
    secs = exp / (1000 * 9.0)  # ~9 rerolls/s/container, 1000 containers
    tstr = "inf" if exp == math.inf else (f"{secs/60:.0f} min" if secs < 36000 else f"{secs/3600:.1f} h")
    cstr = "inf" if exp == math.inf else f"${cost:,.0f}"
    estr = "inf" if exp == math.inf else f"{exp:,.0f}"
    print(f"{name:28s} {n:>5d} {tot:>6d} {tot/n:>6.2f} {p:>10.3e} {estr:>15s} {cstr:>10s} {tstr:>10s}")
print(f"\n(bar to beat = our new #1 {NEW_BEST:,})")
