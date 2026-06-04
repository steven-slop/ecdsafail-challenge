#!/usr/bin/env python3
"""100 trials of active_iters=394: tight p_hat + island budget confirmation."""
import os
import math, time, requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"
ENV = {"DIALOG_GCD_ACTIVE_ITERATIONS": "394"}
REROLLS = list(range(1000, 1100))  # 100 distinct streams


def parse(out):
    line = next(l for l in out.splitlines() if l.startswith("PROBE"))
    return dict(kv.split("=") for kv in line.split()[1:])


specs = [{"env_vars": {"COUNT_ALL_FAILURES": "1", "DIALOG_REROLL": str(r), **ENV}} for r in REROLLS]
print(f"launching {len(specs)} trials...")
t0 = time.time()
ids = requests.post(BASE.format("batch"), json={"token": TOKEN, "build_key": BUILD_KEY, "runs": specs}).json()["job_ids"]
while True:
    res = requests.post(BASE.format("poll"), json={"token": TOKEN, "job_ids": ids}).json()["results"]
    done = sum(1 for v in res.values() if v.get("status") in ("completed", "error"))
    print(f"  {done}/{len(ids)} done ({time.time()-t0:.0f}s)")
    if done == len(ids):
        break
    time.sleep(4)

fs, cl, ph, anc, n, ok, durs = [], 0, 0, 0, 0, 0, []
T = q = score = None
for jid in ids:
    r = res[jid]
    if r.get("status") != "completed":
        continue
    d = parse(r["output"])
    f = int(d["any_fail"]); fs.append(f)
    cl += int(d["classical"]); ph += int(d["phase"]); anc += int(d["ancilla"]); n += int(d["n"])
    if f == 0: ok += 1
    if "duration_seconds" in r: durs.append(r["duration_seconds"])
    T, q, score = d["toffoli"], d["qubits"], d["score"]

runs = len(fs)
p = sum(fs) / n
# binomial 95% CI on p
se = math.sqrt(p * (1 - p) / n)
plo, phi = max(p - 1.96 * se, 0), p + 1.96 * se
exp = math.exp(9024 * p)
exp_lo, exp_hi = math.exp(9024 * plo), math.exp(9024 * phi)

print(f"\n=== active_iters=394 over {runs} trials ({n:,} shots) ===")
print(f"T={int(float(T)):,}  qubits={q}  score={int(score):,}")
print(f"total failures: {sum(fs)}  (classical={cl} phase={ph} ancilla={anc})")
print(f"avg f/reroll: {sum(fs)/runs:.2f}   min={min(fs)} max={max(fs)}   islands(f=0): {ok}")
print(f"p_hat = {p:.3e}   95% CI [{plo:.3e}, {phi:.3e}]")
print(f"exp_rerolls = e^(9024*p_hat) = {exp:,.0f}   95% CI [{exp_lo:,.0f}, {exp_hi:,.0f}]")
if durs:
    print(f"per-reroll server compute: avg {sum(durs)/len(durs):.2f}s  (min {min(durs):.2f} max {max(durs):.2f})")
# histogram
from collections import Counter
h = Counter(fs)
print("f histogram:", " ".join(f"{k}:{h[k]}" for k in sorted(h)))
