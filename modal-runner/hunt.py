import requests, time, json, sys, os

TOKEN = os.environ.get("ECDSAFAIL_TOKEN", "")
BASE = "https://steven-party--ecdsafail-{}.modal.run"
KEY = "02bf750cc8ce12b9"                       # fork dev bba102c (tail-nonce + search_circuit)
ENV = {"DIALOG_GCD_ACTIVE_ITERATIONS": "393", "DIALOG_GCD_WIDTH_MARGIN": "25"}
LOG = "/home/ubuntu/ecdsafail-runner/hunt_log.txt"

PER = 5000               # nonces per container (~275s at 18/s, well under 55min timeout)
NCONT = 1000             # containers per round
ROUND = PER * NCONT      # 5,000,000 nonces/round
START = 0
CAP = 250_000_000        # safety cap on total nonces scanned

def post(ep, body, tries=5, timeout=120):
    last = None
    for _ in range(tries):
        try:
            return requests.post(BASE.format(ep), json={"token": TOKEN, **body}, timeout=timeout).json()
        except Exception as e:
            last = e; time.sleep(3)
    raise last

def launch(body):
    # single attempt, long timeout — NEVER retry a spawn (would double-spawn 1000 containers)
    return requests.post(BASE.format("search"), json={"token": TOKEN, **body}, timeout=600).json()

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def main():
    log(f"HUNT START active393+wm25 key={KEY} per={PER} ncont={NCONT} round={ROUND}")
    t0 = time.time()
    offset = START
    total_checked = 0
    rnd = 0
    while offset < CAP:
        rnd += 1
        r = launch({"build_key": KEY, "env_vars": ENV,
                    "nonce_start": offset, "nonce_count": ROUND,
                    "per_container": PER, "stop_on_island": True})
        jids = r.get("job_ids")
        if not jids:
            log(f"round {rnd}: launch error {str(r)[:300]}"); time.sleep(10); continue
        log(f"round {rnd}: launched {len(jids)} containers, offset={offset:,}")
        # poll until all done
        rt0 = time.time()
        pending = set(jids)
        islands = []
        checked = 0
        while pending and (time.time() - rt0) < 1800:
            time.sleep(8)
            res = post("poll", {"job_ids": list(pending)})["results"]
            done_now = []
            for jid, s in res.items():
                st = s.get("status")
                if st in ("completed", "error"):
                    done_now.append(jid)
                    if st == "completed":
                        checked += int(s.get("checked", 0) or 0)
                        for n in (s.get("islands") or []):
                            islands.append(n)
                    else:
                        log(f"  container error: {str(s)[:200]}")
            for jid in done_now:
                pending.discard(jid)
            if islands:
                log(f"  ISLAND(S) found mid-round: {islands[:10]} (still {len(pending)} pending)")
                break
            if int(time.time() - rt0) % 40 < 8:
                log(f"  round {rnd}: {len(jids)-len(pending)}/{len(jids)} done, checked~{checked:,}")
        total_checked += checked
        if islands:
            best = min(islands)
            log(f"DONE round {rnd}: ISLAND nonce={best} (all={sorted(set(islands))[:20]}) "
                f"total_checked~{total_checked:,} wall={time.time()-t0:.0f}s")
            result = {"nonce": best, "all_islands": sorted(set(islands)),
                      "env": ENV, "key": KEY, "total_checked": total_checked,
                      "wall_s": round(time.time()-t0, 1)}
            with open("/home/ubuntu/ecdsafail-runner/hunt_result.json", "w") as f:
                json.dump(result, f, indent=2)
            log(f"RESULT written: {result}")
            return
        offset += ROUND
        log(f"round {rnd}: no island, total_checked~{total_checked:,}, wall={time.time()-t0:.0f}s")
    log(f"HUNT EXHAUSTED cap={CAP:,} no island found, total_checked~{total_checked:,}")

if __name__ == "__main__":
    main()
