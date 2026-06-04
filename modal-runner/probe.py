#!/usr/bin/env python3
"""Probe ecdsafail circuit configs by FAILURE FREQUENCY (no island hunting).

For a config with per-input failure prob p, the chance a random reroll is a
clean 9024-shot island is (1-p)^9024 ~= e^(-9024*p). So just measure the
failure count f over the sample and read off the island budget directly.

Usage: python3 probe.py
"""
import os
import math
import requests

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
BUILD_KEY = "486c58f866f39d6e"  # fork main c3c3f8d, built with the probe evaluator


def probe(env_vars=None):
    """Run one probe; returns parsed PROBE fields + a human-readable island budget."""
    r = requests.post(BASE.format("run"), json={
        "token": TOKEN,
        "build_key": BUILD_KEY,
        "env_vars": {"COUNT_ALL_FAILURES": "1", **(env_vars or {})},
    }).json()
    if r.get("status") != "completed":
        raise RuntimeError(r.get("error", r))
    line = next(l for l in r["output"].splitlines() if l.startswith("PROBE"))
    d = dict(kv.split("=") for kv in line.split()[1:])
    for k in ("any_fail", "classical", "phase", "ancilla", "n", "qubits", "score"):
        d[k] = int(float(d[k]))
    d["p_hat"] = float(d["p_hat"])
    d["ln_p_island"] = float(d["ln_p_island"])
    # expected number of rerolls to land a clean island
    d["exp_rerolls"] = math.inf if d["ln_p_island"] <= -700 else math.exp(-d["ln_p_island"])
    return d


def show(name, env_vars=None):
    d = probe(env_vars)
    print(f"{name:28s} fail={d['any_fail']:>4} (cl={d['classical']} ph={d['phase']} anc={d['ancilla']})"
          f"  p_hat={d['p_hat']:.2e}  exp_rerolls~{d['exp_rerolls']:.1f}"
          f"  T={int(d['toffoli'] if False else float(d['toffoli'])):,}  q={d['qubits']}  score={d['score']:,}")
    return d


if __name__ == "__main__":
    show("baseline (committed)")
    show("compare_bits=57", {"DIALOG_GCD_COMPARE_BITS": "57"})
    show("active_iters=394", {"DIALOG_GCD_ACTIVE_ITERATIONS": "394"})
