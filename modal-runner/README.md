# modal-runner — ecdsa.fail sweep & hunt tooling

Modal infrastructure for finding, cost-estimating, hunting, validating, and submitting
ecdsa.fail leaderboard improvements. The full strategy is documented in the skill at
[`.agents/skills/ecdsafail-sweep-hunt/SKILL.md`](../.agents/skills/ecdsafail-sweep-hunt/SKILL.md)
— read that first.

## Setup
```bash
pip install modal requests
modal token new                      # auth to Modal
export ECDSAFAIL_TOKEN=...           # the /sweep /search auth token (Devin org secret)
modal deploy app.py                  # deploy the endpoints
```
`app.py` reads its expected auth token from the `ecdsafail-runner-auth` modal secret
(`AUTH_TOKEN`). The client scripts read the same value from `ECDSAFAIL_TOKEN`. Nothing is
hardcoded.

## What's here
- `app.py` — the Modal app. HTTP POST endpoints: `/build /run /batch /sweep /search /poll`.
- `search_circuit.rs`, `eval_circuit_par.rs` — the Rust binaries injected into the fork build:
  the fast incremental-hash island searcher and the parallel COUNT_ALL probe evaluator.

Client scripts (run locally, hit the deployed endpoints):
- `fast_sweep.py` — **start here.** One `/batch`: every numeric lever, both directions, 1
  reroll each, ranked by exact score in ~15s. Keep only configs that beat the current #1.
- `stage2_stacks.py` — do the score-winning levers stack? score + K-reroll p_hat on cumulative
  stacks; verifies each stack stays at q=1350.
- `sweep.py` / `wide_sweep.py` / `explore_wide.py` — broader K=24–48 sweeps + Pareto frontier.
- `val150.py` / `trials100.py` — pin p_hat on a shortlist (K≥150) before spending real money.
- `density.py` — **ground-truth island density** via `/search` (the authoritative cost basis;
  `exp_rerolls = e^(9024·p_hat)` overestimates real cost ~50–100×, see the skill).
- `hunt.py` — the production island hunt (`/search` with `stop_on_island`, writes
  `hunt_result.json`).
- `probe.py` — single `/run` in probe mode; failure breakdown for one config.

## Typical flow
1. `fast_sweep.py` → score-improving levers.
2. `stage2_stacks.py` → which stack, and rough cost.
3. `val150.py` → pin p_hat on the chosen target.
4. `density.py` → real cost (islands per nonce).
5. `hunt.py` → land an island nonce.
6. Rebuild the clean minimal diff, run the override-free grader, submit.
