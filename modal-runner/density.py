#!/usr/bin/env python3
"""GROUND-TRUTH island density: run /search over a fixed nonce range with
stop_on_island=False, count islands actually found per nonce checked.
This bypasses the broken e^(9024*p_hat) estimator entirely.

Also recompute the variance-corrected (negative-binomial) analytic estimate from
the full per-reroll any_fail distribution, to show how far off the plug-in mean was.
"""
import os
import math, time, requests, statistics

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
KEY = "02bf750cc8ce12b9"   # fork dev bba102c: tail-nonce + search_circuit
A  = "DIALOG_GCD_ACTIVE_ITERATIONS"
WM = "DIALOG_GCD_WIDTH_MARGIN"

# (label, env, nonce_count, per_container)  -> nonce_count = ncont*per
TARGETS = [
    ("A=393+WM=25 (current #1)", {A: "393", WM: "25"}, 1_000_000, 5000),
    ("A=392+WM=25",              {A: "392", WM: "25"}, 1_500_000, 5000),
]

def post(ep, body, tries=6, timeout=180):
    last = None
    for _ in range(tries):
        try:
            return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=timeout).json()
        except Exception as e:
            last = e; time.sleep(3)
    raise last

def launch_search(body):
    return requests.post(BASE.format("search"), json={"token": TOKEN, **body}, timeout=600).json()

def run_density(label, env, ncount, per):
    print(f"\n=== {label}: searching {ncount:,} nonces (per={per}, stop_on_island=False) ===", flush=True)
    r = launch_search({"build_key": KEY, "env_vars": env, "nonce_start": 1,
                       "nonce_count": ncount, "per_container": per, "stop_on_island": False})
    jids = r.get("job_ids")
    if not jids:
        print(f"  launch error: {str(r)[:300]}"); return None
    print(f"  launched {len(jids)} containers", flush=True)
    pending = set(jids); checked = 0; islands = []
    t0 = time.time()
    while pending and (time.time() - t0) < 1800:
        time.sleep(8)
        res = post("poll", {"job_ids": list(pending)})["results"]
        done = []
        for jid, s in res.items():
            st = s.get("status")
            if st in ("completed", "error"):
                done.append(jid)
                if st == "completed":
                    checked += int(s.get("checked", 0) or 0)
                    islands += (s.get("islands") or [])
                else:
                    print(f"  container error: {str(s)[:150]}")
        for jid in done: pending.discard(jid)
        if int(time.time()-t0) % 32 < 8:
            print(f"  {len(jids)-len(pending)}/{len(jids)} done, checked~{checked:,}, islands={len(islands)}", flush=True)
    n_isl = len(islands)
    dens = n_isl / checked if checked else 0
    # Poisson CI on island count
    lo = max(0.0, n_isl - 1.96*math.sqrt(n_isl)) if n_isl else 0
    hi = n_isl + 1.96*math.sqrt(max(n_isl,1))
    exp = (1/dens) if dens > 0 else math.inf
    print(f"  RESULT {label}: checked={checked:,} islands={n_isl} "
          f"density={dens:.3e}  exp_rerolls~{('inf' if exp==math.inf else format(exp,',.0f'))}", flush=True)
    if n_isl:
        print(f"    island nonces (first 15): {sorted(islands)[:15]}")
        print(f"    exp_rerolls 95% CI: [{checked/hi:,.0f}, {(checked/lo) if lo>0 else float('inf'):,.0f}]  "
              f"cost~${exp*2e-5:,.0f}")
    return dict(label=label, checked=checked, islands=n_isl, density=dens, exp=exp)

# variance-corrected analytic estimate from full any_fail arrays (one big sweep)
def nb_estimate(label, env, K=300, per=10):
    r = post("sweep", {"build_key": "486c58f866f39d6e", "env_vars": env,
                       "rerolls": list(range(1, K+1)), "per_container": per,
                       "stop_on_island": False})
    jids = r["job_ids"]
    pending = set(jids); recs = []
    t0 = time.time()
    while pending and (time.time()-t0) < 900:
        time.sleep(4)
        res = post("poll", {"job_ids": list(pending)})["results"]
        for jid, s in list(res.items()):
            if s.get("status") in ("completed", "error"):
                pending.discard(jid)
                if s.get("status") == "completed":
                    recs += [x["any_fail"] for x in s.get("sweep", []) if "any_fail" in x]
    if not recs: return None
    mean = statistics.mean(recs); var = statistics.pvariance(recs)
    p_hat = mean / 9024
    naive = math.exp(9024 * p_hat)
    # Negative-binomial MoM: var = mean + mean^2/size  -> size = mean^2/(var-mean)
    if var > mean:
        size = mean*mean/(var-mean)
        # P(0) = (size/(size+mean))^size
        p0 = (size/(size+mean))**size
        nb = 1/p0 if p0>0 else math.inf
    else:
        size = math.inf; nb = naive
    # empirical lognormal-ish: E[e^{-f}] approx via sample
    emp = statistics.mean(math.exp(-f) for f in recs)
    emp_exp = 1/emp if emp>0 else math.inf
    print(f"\n[NB] {label}: n={len(recs)} mean_f={mean:.2f} var_f={var:.2f} "
          f"(Poisson var would be {mean:.2f})")
    print(f"     naive e^(9024 p_hat) = {naive:,.0f}")
    print(f"     neg-binomial P(0)    -> exp_rerolls = {('inf' if nb==math.inf else format(nb,',.0f'))} (size={size:.2f})")
    print(f"     empirical E[e^-f]    -> exp_rerolls = {emp_exp:,.0f}")
    return dict(label=label, mean=mean, var=var, naive=naive, nb=nb, emp=emp_exp)

if __name__ == "__main__":
    print("######## VARIANCE-CORRECTED ANALYTIC (from sweep distribution) ########")
    for label, env, _, _ in TARGETS:
        nb_estimate(label, env)
    print("\n######## GROUND TRUTH (direct island count via /search) ########")
    out = []
    for label, env, nc, per in TARGETS:
        out.append(run_density(label, env, nc, per))
    print("\n######## SUMMARY ########")
    for o in out:
        if o:
            print(f"  {o['label']:28s} islands={o['islands']:>3d}/{o['checked']:>9,} "
                  f"-> exp_rerolls~{('inf' if o['exp']==math.inf else format(o['exp'],',.0f')):>12s} "
                  f"cost~${(o['exp']*2e-5) if o['exp']!=math.inf else float('inf'):,.0f}")
