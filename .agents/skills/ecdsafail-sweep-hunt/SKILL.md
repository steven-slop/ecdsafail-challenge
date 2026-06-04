---
name: ecdsafail-sweep-hunt
description: How to find, cost-estimate, hunt, validate, and submit ecdsa.fail leaderboard improvements using the Modal infrastructure in /home/ubuntu/ecdsafail-runner. Covers the /build /run /sweep /search /poll endpoints, the p-hat -> expected-rerolls cost model (and its known upward bias), and the distinction between parameter sweeps and algorithmic changes.
---

# ecdsa.fail sweep & hunt strategy (Modal)

The goal of the ecdsa.fail challenge: minimize the leaderboard **score = peak_qubits ×
avg_executed_Toffoli** for the ECDSA point-add circuit. Lower is better. A submission is
only accepted if it (a) strictly beats the current #1 score AND (b) is a clean **island**:
all 9024 evaluation shots pass (0 classical / 0 phase / 0 ancilla failures) on the
official override-free grader.

Two independent things determine whether a candidate config is worth pursuing:
1. **Its score** (deterministic given the config) — how much it would improve the board.
2. **Its island rarity** (how many random rerolls you must try to hit a clean 9024/9024) —
   how much Modal compute the hunt costs.

This skill explains how to measure both with the Modal setup, and the key pitfall in
estimating (2).

---

## 1. The Modal infrastructure

All code lives in `/home/ubuntu/ecdsafail-runner/`. The Modal app is `app.py`
(deploy with `modal deploy app.py`). Endpoints are HTTP POST, auth via a token in the body.

- **Base URL:** `https://steven-party--ecdsafail-{ENDPOINT}.modal.run`
- **Auth:** every request body includes `{"token": <ECDSAFAIL_TOKEN>, ...}`. The server reads the
  expected value from the `ecdsafail-runner-auth` modal secret (`AUTH_TOKEN`); the client scripts read
  it from the `ECDSAFAIL_TOKEN` env var (`export ECDSAFAIL_TOKEN=...` before running them). Never hardcode it.
- **Container:** 8 cores + 32 GiB = $0.633/hr = **$0.000176/s**. `min_containers=1` keeps one warm (~$15/day) for snappy `/run`.

### Endpoints
| endpoint | purpose |
|---|---|
| `/build` | Clone a repo+commit, `cargo build` the circuit binaries, cache under a `build_key`. Returns `build_key`. Body: `{repo_url, commit}`. |
| `/run` | Synchronous single run on a cached `build_key` with `env_vars`. Returns exact T/q/score + (if `COUNT_ALL_FAILURES=1` in env) a `PROBE` line with any_fail/classical/phase/ancilla. Use for exact validation of one config/reroll. |
| `/batch` | Up to 100 parallel `/run`s, one per container (1 reroll each). Coarse; prefer `/sweep`. |
| `/sweep` | Each container loops over K rerolls (reusing copied binaries), returns per-reroll failure counts. Varies `DIALOG_REROLL` by default. Used to estimate p-hat. Max 100 containers/call. |
| `/search` | The fast island hunt. Partitions a contiguous **nonce** range across containers using the `search_circuit` binary (incremental hash + early-exit). Each container returns `.islands` (nonce list), `.checked`, `.rate_per_s`. `stop_on_island` short-circuits. Max 1000 containers/call. |
| `/poll` | Poll one or many `job_ids`. Returns per-job status/result. |

### The two build keys (two different mechanisms)
- **`486c58f866f39d6e`** — fork `dev` @ c3c3f8d, the **probe evaluator** (`build_circuit` +
  `eval_circuit_par` with `COUNT_ALL_FAILURES`). Counts all failures per reroll without
  early-exit. Used by `/sweep` and probe `/run`. Varies **`DIALOG_REROLL`** (appends X;X to
  the op stream; requires a `build_circuit` regen per reroll, ~5 s/reroll).
- **`02bf750cc8ce12b9`** — fork `dev` @ bba102c, the **search binary** (`search_circuit`).
  Varies **`DIALOG_TAIL_NONCE`**: a fixed-length 96-op identity (X;X) tail that only reseeds
  the SHAKE256 test inputs. Because the tail is fixed-length, the hashed prefix is constant,
  so the binary hashes the 12.7M-op prefix once and only re-absorbs the 96-op tail per nonce,
  and early-exits eval on the first failing shot. ~0.1 s/reroll → **~$2e-5/reroll**. This is
  what the real hunt and the submission island use.

To target a new base config, rebuild with `/build` (then the new `build_key` carries the
config), or pass levers as `env_vars` overrides on top of an existing key.

---

## 2. Parameter sweeps vs algorithmic changes (both valid)

There are two distinct ways to improve the score. Treat them differently.

### (a) Parameter sweeps — tuning constants
Numeric "lever" env vars change loop bounds / margins without changing the algorithm. The full
set (committed defaults from `configure_ecdsafail_submission_route`, mod.rs ~31052), the ones
that move the score:

| lever env var | default | direction that lowers score |
|---|---|---|
| `DIALOG_GCD_ACTIVE_ITERATIONS` | 394 | down (but ≤393 to stay at q=1350; 394+ jumps to 1355) |
| `DIALOG_GCD_WIDTH_MARGIN` | 26 | down (biggest single lever: 26→23 ≈ −0.5%) |
| `DIALOG_GCD_WIDTH_SLOPE_X1000` | 711 | **UP** (711→714 lowers T — the one inverted lever) |
| `DIALOG_GCD_COMPARE_BITS` | 56 | down |
| `DIALOG_GCD_APPLY_CLEAN_COMPARE_BITS` | 20 | down |
| `DIALOG_GCD_PA9024_COMPARE_SCHEDULE_MARGIN` | 6 | down |
| `KAL_DOUBLE_CARRY_TRUNC_W` | 20 | down |
| `KAL_FOLD_CARRY_TRUNC_W` | 20 | down |

`DIALOG_GCD_APPLY_WINDOW_BLOCKS` (2) had no score effect; `DIALOG_GCD_APPLY_CHUNKED_F_BLOCKS`
(4) trades T for a big qubit jump (3→q=1462, 5→q=1381) so it's score-negative. `ROUND84_XTAIL_KARATSUBA`
(0) adds 56 qubits when enabled. There are ~40 more boolean structure toggles, almost all
correctness-critical (flipping breaks the circuit → mass failures); leave them at defaults.

- Cheap to explore: just set env_vars and probe. No code edits, no rebuild needed if the
  lever is read at runtime.
- **Tightening a lever almost always lowers T but raises the per-shot failure rate**, so the
  island gets rarer. This is the score ↔ hunt-cost frontier. Each step tends to ~double the
  failure rate, so cost grows fast (exponential wall) as you stack levers.
- Both directions matter — don't assume "lower is always better." Most levers improve when
  tightened, but `WIDTH_SLOPE_X1000` improves when *raised* (711→714). Always sweep up AND
  down on every lever; a prior contributor may have set one on the wrong side.

### (b) Algorithmic changes — editing the circuit
Editing `src/point_add/mod.rs` (the circuit construction) to restructure the computation:
e.g. CCX-gated uncompute, cutting the **qubit peak** (1350→1349), Karatsuba variants.
- These change qubits and/or Toffoli **structurally**. A qubit-peak cut multiplies against T
  (score = q × T), so −1 qubit ≈ −1.76M score — bigger than a whole lever step — and it does
  NOT compound island rarity the way lever-tightening does.
- More effort (real code + correctness work) but a different, often better, lever once the
  parameter frontier is exhausted.

Rule of thumb: exhaust cheap parameter sweeps first; when the frontier hits the exponential
wall, switch to algorithmic (especially qubit-peak) changes.

---

## 3. Cost estimation: p-hat -> expected rerolls (AND its bias)

### The principle
For a candidate config, `/sweep` K rerolls and compute:
- `p_hat = total_failures / (n_evaluated × 9024)`  (mean per-shot failure rate; `total_failures`
  is the sum of per-reroll `any_fail` counts — already correct, NOT a double-count of
  classical+phase+ancilla).
- If shots failed independently at rate p, then P(a reroll is a clean island) = (1−p)^9024 ≈
  e^(−9024·p), so **expected rerolls to find one island ≈ `exp_rerolls = e^(9024·p_hat)`**.
- Cost ≈ `exp_rerolls × $2e-5` (search binary). Wall-time = `exp_rerolls × (rate) / N_containers`;
  cost is independent of container count — parallelism buys speed, not money.

`exp_rerolls` is **exponentially sensitive** to p_hat, so use K≥150 rerolls to pin p_hat, and
treat the number as order-of-magnitude.

### KNOWN BIAS — do not trust exp_rerolls as an absolute cost
Measured empirically (June 2026): `e^(9024·p_hat)` **overestimates the real hunt cost by
~50–100×**. For A=393+WM=25 (our current #1), the sweep predicted ~50M rerolls (~$1000), but a
direct `/search` over ~1M nonces found islands at **~1 per ~1,000,000** (≈$20). Same story on
every real hunt: both islands we actually landed cost ~$15, not the hundreds predicted.

Why (verified):
- It is NOT overdispersion. The per-reroll failure count has **var ≈ mean** (e.g. A=393+WM=25:
  mean_f=17.7, var_f=15.4), i.e. the within-reroll shots behave independently, so a
  variance-corrected (negative-binomial) P(0) gives the same ~50M.
- The gap is that the **`/sweep` probe path counts more failures than the real `/search` path
  experiences**: `/sweep` uses `build_circuit`+`eval_circuit_par` varying `DIALOG_REROLL`,
  while `/search` uses `search_circuit` varying `DIALOG_TAIL_NONCE`. The mean failure rate the
  probe reports (~17.7) is higher than the effective rate the search path sees (~ln(1e6)=13.8).

### Therefore — the authoritative method is direct island density
Use `exp_rerolls = e^(9024·p_hat)` only as a **fast relative ranking screen / upper bound**
across many candidates (it orders configs correctly even though the absolute number is high).
To get a real cost, **ground-truth it**: run `/search` with `stop_on_island=False` over a fixed
nonce range and count islands:

```
true_density = islands_found / nonces_checked
exp_rerolls_true = 1 / true_density       (95% CI: Poisson on the island count)
cost_true = exp_rerolls_true × $2e-5
```

Size the range to yield several islands (e.g. 1–3M nonces). See `density.py`.

---

## 4. End-to-end workflow

1. **Pick candidates.** Parameter sweep (levers, both directions) and/or an algorithmic edit.
2. **Screen scores FAST first.** `fast_sweep.py`: one `/batch` (≤100 configs, 1 reroll each,
   COUNT_ALL) probes every lever value BOTH directions in ~15s. One reroll pins the score
   exactly (T/q deterministic per config); rank by score and keep only the configs that beat
   #1. Don't waste failure-rate sweeps on configs that raise the score.
3. **Then screen cost + check stacking.** `stage2_stacks.py` / `sweep.py`: K=24–48 `/sweep`
   over the score-improving shortlist + cumulative stacks, compute `exp_rerolls` (relative
   ranking only). Stacked levers compound island rarity — verify each stack still lands at
   q=1350 and estimate its combined p_hat. Drop Pareto-dominated configs.
4. **Pin p_hat** on the shortlist with K≥150 (`val150.py`).
5. **Ground-truth the cost** of the chosen target with a direct `/search` density run
   (`density.py`) — this is the number to trust before spending real money.
6. **Hunt.** `hunt.py`: `/search` with `stop_on_island=True`, 100–1000 containers, rounds of
   a few M nonces until an island nonce is returned. Writes `hunt_result.json`.
7. **Validate the island officially.** Rebuild the clean minimal diff and run the
   **override-free** grader (`ecdsafail run` in `ecdsafail-submit`): confirm all 9024 OK and
   the score. Never trust the probe for the final check.
8. **Submit.** Clean minimal diff (only `src/point_add/mod.rs`: the lever values +
   `DIALOG_TAIL_NONCE`), `ecdsafail submit --model "Devin"` with a substance-first note.
   CLI token + login already configured. The note is locked at submit time (no edit-note).

### Scripts inventory (`/home/ubuntu/ecdsafail-runner/`)
- `fast_sweep.py` — **stage-1 fast score screen**: one `/batch`, every lever both directions,
  1 reroll each, ranks by exact score in ~15s. Start here.
- `stage2_stacks.py` — stage-2: do the score-winning levers stack? score + K-reroll p_hat on
  cumulative stacks; checks each stack stays at q=1350.
- `probe.py` — single `/run` in probe mode; quick failure breakdown for one config.
- `sweep.py` / `wide_sweep.py` — K=24-ish sweep across configs, frontier table.
- `explore_wide.py` — both-direction lever scan + structural toggles, Pareto frontier.
- `trials100.py` / `val150.py` — tighten p_hat for a shortlist (K=100/150).
- `density.py` — **ground-truth island density** via `/search` (the cost-of-record method) +
  a variance-corrected (negative-binomial) analytic cross-check.
- `hunt.py` — the production island hunt (rounds of `/search`, writes `hunt_result.json`).

NOTE: never retry a `/search` spawn on timeout — it can double-spawn 1000 containers. Launch
once with a long client timeout, then poll.

---

## 5. Scoring / leaderboard conventions
- Board shows the absolute **score** (q × avg_Toffoli). Lower ranks higher.
- Board **DIFF** = gap to the row directly below you (the submission you beat).
- Board **DIFF%** = DIFF ÷ a fixed reference baseline ≈ **1.086e10** (back-solved from existing
  rows), NOT ÷ current score. So a −2.98M score gap shows as ≈ −0.03%. When quoting % to the
  user, use this convention, not "% of current score".
