import modal
import uuid
import time
import json
import os
import hashlib

app = modal.App("ecdsafail-runner")

# High-powered image with Rust toolchain
runner_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl", "build-essential", "pkg-config")
    .run_commands(
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.93.0",
    )
    .env({"PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"})
    .add_local_file("/home/ubuntu/ecdsafail-runner/eval_circuit_par.rs", "/opt/eval_circuit_par.rs")
    .add_local_file("/home/ubuntu/ecdsafail-runner/search_circuit.rs", "/opt/search_circuit.rs")
)

# Lightweight image for API endpoints
api_image = modal.Image.debian_slim(python_version="3.11")

# Persistent dict for job results
jobs_dict = modal.Dict.from_name("ecdsafail-jobs", create_if_missing=True)

# Volume for caching compiled binaries
build_cache = modal.Volume.from_name("ecdsafail-build-cache", create_if_missing=True)

# Auth secret
auth_secret = modal.Secret.from_name("ecdsafail-runner-auth")


def check_auth(request: dict) -> str | None:
    """Returns error message if unauthorized, None if OK."""
    token = request.get("token", "")
    expected_token = os.environ.get("AUTH_TOKEN", "")
    if not expected_token or token != expected_token:
        return "unauthorized"
    return None


def cache_key(repo_url: str, commit: str) -> str:
    """Deterministic key for a (repo, commit) pair."""
    h = hashlib.sha256(f"{repo_url}:{commit}".encode()).hexdigest()[:16]
    return h


@app.function(image=runner_image, timeout=1800, cpu=8, memory=32768, volumes={"/cache": build_cache}, region="us-east")
def do_build(job_id: str, repo_url: str, commit: str):
    """Clone repo, checkout commit, compile binaries, cache them."""
    import subprocess

    start = time.time()
    jobs_dict[job_id] = json.dumps({
        "status": "building",
        "repo_url": repo_url,
        "commit": commit,
        "started_at": start,
    })

    key = cache_key(repo_url, commit)
    cache_dir = f"/cache/{key}"

    import tempfile
    repo_dir = tempfile.mkdtemp(prefix="repo_")
    try:
        # Clone (unique dir per invocation — avoids warm-container collisions)
        result = subprocess.run(
            ["git", "clone", "--depth", "100", repo_url, repo_dir],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"git clone failed: {result.stderr}")

        # Checkout
        result = subprocess.run(
            ["git", "checkout", commit],
            capture_output=True, text=True, timeout=30, cwd=repo_dir
        )
        if result.returncode != 0:
            # commit may be on a non-default branch not covered by the shallow
            # default-branch clone — fetch ALL branch tips, then retry.
            subprocess.run(["git", "fetch", "--depth", "100", "origin",
                            "+refs/heads/*:refs/remotes/origin/*"],
                           capture_output=True, text=True, timeout=180, cwd=repo_dir)
            result = subprocess.run(
                ["git", "checkout", commit],
                capture_output=True, text=True, timeout=30, cwd=repo_dir
            )
            if result.returncode != 0:
                subprocess.run(["git", "fetch", "--unshallow", "origin",
                                "+refs/heads/*:refs/remotes/origin/*"],
                               capture_output=True, text=True, timeout=300, cwd=repo_dir)
                result = subprocess.run(
                    ["git", "checkout", commit],
                    capture_output=True, text=True, timeout=30, cwd=repo_dir
                )
                if result.returncode != 0:
                    raise Exception(f"git checkout failed: {result.stderr}")

        # Inject parallel evaluator + dedicated island-search binary sources
        subprocess.run(["cp", "/opt/eval_circuit_par.rs", f"{repo_dir}/src/bin/eval_circuit_par.rs"], check=True)
        subprocess.run(["cp", "/opt/search_circuit.rs", f"{repo_dir}/src/bin/search_circuit.rs"], check=True)

        # Build all binaries (including parallel evaluator)
        result = subprocess.run(
            ["cargo", "build", "--release"],
            capture_output=True, text=True, timeout=600, cwd=repo_dir,
            env={**os.environ, "PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}
        )
        if result.returncode != 0:
            raise Exception(f"cargo build failed: {result.stderr[-5000:]}")

        # Cache the binaries
        os.makedirs(cache_dir, exist_ok=True)
        subprocess.run(["cp", f"{repo_dir}/target/release/build_circuit", f"{cache_dir}/build_circuit"], check=True)
        subprocess.run(["cp", f"{repo_dir}/target/release/eval_circuit", f"{cache_dir}/eval_circuit"], check=True)
        subprocess.run(["cp", f"{repo_dir}/target/release/eval_circuit_par", f"{cache_dir}/eval_circuit_par"], check=True)
        subprocess.run(["cp", f"{repo_dir}/target/release/search_circuit", f"{cache_dir}/search_circuit"], check=True)
        subprocess.run(["cp", f"{repo_dir}/Cargo.toml", f"{cache_dir}/Cargo.toml"], check=True)
        build_cache.commit()

        duration = time.time() - start
        jobs_dict[job_id] = json.dumps({
            "status": "completed",
            "success": True,
            "build_key": key,
            "duration_seconds": round(duration, 2),
            "output": f"Build successful. Cached as key={key}.\n{result.stderr[-3000:]}",
            "repo_url": repo_url,
            "commit": commit,
        })

    except Exception as e:
        duration = time.time() - start
        jobs_dict[job_id] = json.dumps({
            "status": "error",
            "success": False,
            "error": str(e),
            "duration_seconds": round(duration, 2),
            "repo_url": repo_url,
            "commit": commit,
        })
    finally:
        subprocess.run(["rm", "-rf", repo_dir], check=False)


@app.function(image=runner_image, timeout=600, cpu=8, memory=32768, volumes={"/cache": build_cache}, region="us-east", min_containers=1)
def do_run(job_id: str, build_key: str, env_vars: dict, command: str | None = None):
    """Run benchmark using cached binaries. Accepts custom env vars for seed search."""
    import subprocess

    start = time.time()
    cache_dir = f"/cache/{build_key}"

    jobs_dict[job_id] = json.dumps({
        "status": "running",
        "build_key": build_key,
        "env_vars": env_vars,
        "started_at": start,
    })

    try:
        # Check cache exists. Warm containers (min_containers=1) hold a stale
        # volume view, so reload once if the key isn't visible yet.
        if not os.path.exists(f"{cache_dir}/build_circuit"):
            build_cache.reload()
        if not os.path.exists(f"{cache_dir}/build_circuit"):
            raise Exception(f"Build key '{build_key}' not found in cache. Run /build first.")

        # Per-build_key work dir — persists across invocations on warm containers
        work_dir = f"/tmp/work/{build_key}"
        par_path = f"{cache_dir}/eval_circuit_par"
        has_par = os.path.exists(par_path)

        # Skip copy if binaries already local from a previous invocation
        if not os.path.exists(f"{work_dir}/build_circuit"):
            os.makedirs(work_dir, exist_ok=True)
            subprocess.run(["cp", f"{cache_dir}/build_circuit", f"{work_dir}/build_circuit"], check=True)
            subprocess.run(["cp", f"{cache_dir}/eval_circuit", f"{work_dir}/eval_circuit"], check=True)
            if has_par:
                subprocess.run(["cp", par_path, f"{work_dir}/eval_circuit_par"], check=True)
            subprocess.run(["chmod", "+x"] + [f"{work_dir}/{b}" for b in os.listdir(work_dir)], check=True)
        else:
            # Clean up any leftover ops.bin / score.json from previous run
            for f in ("ops.bin", "score.json", "results.tsv"):
                p = os.path.join(work_dir, f)
                if os.path.exists(p):
                    os.remove(p)

        # Build environment with custom vars
        run_env = {**os.environ, "PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}
        run_env.update(env_vars or {})

        # Default command: build_circuit then parallel eval_circuit
        if command:
            cmd = command
        else:
            eval_bin = "./eval_circuit_par" if has_par else "./eval_circuit"
            cmd = f"./build_circuit && {eval_bin}"

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=300,
            cwd=work_dir, env=run_env
        )

        duration = time.time() - start
        output = result.stdout[-100000:] if len(result.stdout) > 100000 else result.stdout
        stderr = result.stderr[-50000:] if len(result.stderr) > 50000 else result.stderr

        jobs_dict[job_id] = json.dumps({
            "status": "completed",
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": output,
            "stderr": stderr,
            "duration_seconds": round(duration, 2),
            "build_key": build_key,
            "env_vars": env_vars,
        })

    except Exception as e:
        duration = time.time() - start
        jobs_dict[job_id] = json.dumps({
            "status": "error",
            "success": False,
            "error": str(e),
            "duration_seconds": round(duration, 2),
            "build_key": build_key,
            "env_vars": env_vars,
        })


def _parse_probe(stdout: str):
    """Pull the machine-readable PROBE line out of eval output, if present."""
    for line in stdout.splitlines():
        if line.startswith("PROBE "):
            d = {}
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                d[k] = v
            return d
    return None


def _lead_float(s):
    """Parse the leading numeric portion of a string like '0.61/reroll/s'."""
    if not s:
        return None
    import re
    m = re.match(r"[-+]?[0-9]*\.?[0-9]+", s)
    return float(m.group(0)) if m else None


def _parse_search(stdout: str):
    """Pull ISLAND lines and the SEARCH_DONE / COUNT_ALL_SUMMARY line out of
    search_circuit output."""
    islands = []
    done = None
    summary = None
    trials = {}
    for line in stdout.splitlines():
        if line.startswith("ISLAND "):
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                if k == "nonce":
                    islands.append(int(v))
        elif line.startswith("TRIAL "):
            d = {}
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                d[k] = v
            if "nonce" in d and "any_fail" in d:
                trials[int(d["nonce"])] = int(d["any_fail"])
        elif line.startswith("SEARCH_DONE "):
            done = {}
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                done[k] = v
        elif line.startswith("COUNT_ALL_SUMMARY "):
            summary = {}
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                summary[k] = v
    return islands, done, summary, trials


@app.function(image=runner_image, timeout=3600, cpu=8, memory=32768, volumes={"/cache": build_cache}, region="us-east")
def do_search(job_id: str, build_key: str, start: int, count: int, base_env: dict,
              stop_on_island: bool = True, count_all: bool = False, threads: int | None = None):
    """Hunt a contiguous nonce range [start, start+count) on ONE container using
    the dedicated search_circuit binary: builds the circuit ONCE in-process,
    pre-hashes the fixed op prefix once, then per nonce only re-absorbs the
    96-op tail + early-exits eval on first failing shot. Internally multi-threaded
    across all cores. Short-circuits the whole container on the first island.
    """
    import subprocess

    t0 = time.time()
    cache_dir = f"/cache/{build_key}"
    jobs_dict[job_id] = json.dumps({
        "status": "running", "build_key": build_key,
        "start": start, "count": count, "started_at": t0,
    })

    try:
        if not os.path.exists(f"{cache_dir}/search_circuit"):
            build_cache.reload()
        if not os.path.exists(f"{cache_dir}/search_circuit"):
            raise Exception(f"Build key '{build_key}' has no search_circuit. Rebuild with /build first.")

        work_dir = f"/tmp/work/{build_key}"
        if not os.path.exists(f"{work_dir}/search_circuit"):
            os.makedirs(work_dir, exist_ok=True)
            subprocess.run(["cp", f"{cache_dir}/search_circuit", f"{work_dir}/search_circuit"], check=True)
            subprocess.run(["chmod", "+x", f"{work_dir}/search_circuit"], check=True)

        run_env = {**os.environ, "PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}
        run_env.update(base_env or {})
        run_env["SEARCH_START"] = str(start)
        run_env["SEARCH_COUNT"] = str(count)
        run_env["STOP_ON_ISLAND"] = "1" if stop_on_island else "0"
        if count_all:
            run_env["COUNT_ALL"] = "1"
        if threads:
            run_env["SEARCH_THREADS"] = str(threads)

        res = subprocess.run(
            ["./search_circuit"],
            capture_output=True, text=True, timeout=3300, cwd=work_dir, env=run_env,
        )
        islands, done, summary, trials = _parse_search(res.stdout)
        record = {
            "status": "completed",
            "success": res.returncode == 0 and done is not None,
            "build_key": build_key, "base_env": base_env,
            "start": start, "count": count,
            "islands": islands,
            "checked": int(done.get("checked", -1)) if done else -1,
            "rate_per_s": _lead_float(done.get("rate")) if done else None,
            "search_done": done,
            "duration_seconds": round(time.time() - t0, 2),
        }
        if summary:
            record["count_all"] = summary
        if trials:
            record["trials"] = trials
        if not record["success"]:
            record["stderr"] = (res.stderr or res.stdout)[-1500:]
        jobs_dict[job_id] = json.dumps(record)
    except Exception as e:
        jobs_dict[job_id] = json.dumps({
            "status": "error", "success": False, "error": str(e),
            "build_key": build_key, "start": start, "count": count,
            "duration_seconds": round(time.time() - t0, 2),
        })


@app.function(image=runner_image, timeout=1800, cpu=8, memory=32768, volumes={"/cache": build_cache}, region="us-east")
def do_sweep(job_id: str, build_key: str, rerolls: list, base_env: dict,
             reroll_var: str = "DIALOG_REROLL", stop_on_island: bool = True):
    """Sweep MANY rerolls inside ONE container (amortizes scheduling + binary copy).

    For each reroll value: set {reroll_var}=<value>, run build_circuit + eval_circuit_par
    in COUNT_ALL_FAILURES probe mode, parse the per-reroll failure count. Short-circuits
    the moment a clean island (any_fail==0) is found if stop_on_island is set.
    """
    import subprocess

    start = time.time()
    cache_dir = f"/cache/{build_key}"
    jobs_dict[job_id] = json.dumps({
        "status": "running", "build_key": build_key,
        "n_rerolls": len(rerolls), "started_at": start,
    })

    try:
        if not os.path.exists(f"{cache_dir}/build_circuit"):
            build_cache.reload()
        if not os.path.exists(f"{cache_dir}/build_circuit"):
            raise Exception(f"Build key '{build_key}' not found in cache. Run /build first.")

        work_dir = f"/tmp/work/{build_key}"
        par_path = f"{cache_dir}/eval_circuit_par"
        has_par = os.path.exists(par_path)
        if not os.path.exists(f"{work_dir}/build_circuit"):
            os.makedirs(work_dir, exist_ok=True)
            subprocess.run(["cp", f"{cache_dir}/build_circuit", f"{work_dir}/build_circuit"], check=True)
            subprocess.run(["cp", f"{cache_dir}/eval_circuit", f"{work_dir}/eval_circuit"], check=True)
            if has_par:
                subprocess.run(["cp", par_path, f"{work_dir}/eval_circuit_par"], check=True)
            subprocess.run(["chmod", "+x"] + [f"{work_dir}/{b}" for b in os.listdir(work_dir)], check=True)

        eval_bin = "./eval_circuit_par" if has_par else "./eval_circuit"
        sweep = []
        island = None
        for r in rerolls:
            for f in ("ops.bin", "score.json", "results.tsv"):
                p = os.path.join(work_dir, f)
                if os.path.exists(p):
                    os.remove(p)

            run_env = {**os.environ, "PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}
            run_env.update(base_env or {})
            run_env["COUNT_ALL_FAILURES"] = "1"
            run_env[reroll_var] = str(r)

            res = subprocess.run(
                ["bash", "-c", f"./build_circuit && {eval_bin}"],
                capture_output=True, text=True, timeout=300, cwd=work_dir, env=run_env,
            )
            probe = _parse_probe(res.stdout)
            if probe is None:
                sweep.append({"reroll": r, "error": (res.stderr or res.stdout)[-500:]})
                continue
            rec = {
                "reroll": r,
                "any_fail": int(probe.get("any_fail", -1)),
                "classical": int(probe.get("classical", -1)),
                "phase": int(probe.get("phase", -1)),
                "ancilla": int(probe.get("ancilla", -1)),
            }
            sweep.append(rec)
            if rec["any_fail"] == 0 and island is None:
                island = {"reroll": r, **{k: probe.get(k) for k in ("toffoli", "qubits", "score")}}
                if stop_on_island:
                    break

        ok = [s for s in sweep if "any_fail" in s]
        tot_fail = sum(s["any_fail"] for s in ok)
        jobs_dict[job_id] = json.dumps({
            "status": "completed", "success": True,
            "build_key": build_key, "base_env": base_env,
            "n_rerolls": len(rerolls), "n_evaluated": len(ok),
            "total_failures": tot_fail,
            "island": island,
            "sweep": sweep,
            "duration_seconds": round(time.time() - start, 2),
        })
    except Exception as e:
        jobs_dict[job_id] = json.dumps({
            "status": "error", "success": False, "error": str(e),
            "build_key": build_key,
            "duration_seconds": round(time.time() - start, 2),
        })


@app.function(image=runner_image, timeout=1800, cpu=8, memory=32768, volumes={"/cache": build_cache})
def do_full_run(job_id: str, repo_url: str, commit: str, command: str, env_vars: dict | None = None):
    """Legacy: clone, compile, and run in one shot."""
    import subprocess

    start = time.time()
    jobs_dict[job_id] = json.dumps({
        "status": "running",
        "repo_url": repo_url,
        "commit": commit,
        "command": command,
        "started_at": start,
    })

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "100", repo_url, "/tmp/repo"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"git clone failed: {result.stderr}")

        result = subprocess.run(
            ["git", "checkout", commit],
            capture_output=True, text=True, timeout=30, cwd="/tmp/repo"
        )
        if result.returncode != 0:
            subprocess.run(["git", "fetch", "--unshallow"],
                           capture_output=True, text=True, timeout=120, cwd="/tmp/repo")
            result = subprocess.run(
                ["git", "checkout", commit],
                capture_output=True, text=True, timeout=30, cwd="/tmp/repo"
            )
            if result.returncode != 0:
                raise Exception(f"git checkout failed: {result.stderr}")

        run_env = {**os.environ, "PATH": "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin"}
        run_env.update(env_vars or {})

        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=1500,
            cwd="/tmp/repo", env=run_env
        )

        duration = time.time() - start
        output = result.stdout[-100000:] if len(result.stdout) > 100000 else result.stdout
        stderr = result.stderr[-50000:] if len(result.stderr) > 50000 else result.stderr

        # Cache binaries if build succeeded
        key = cache_key(repo_url, commit)
        cache_dir = f"/cache/{key}"
        if os.path.exists("/tmp/repo/target/release/build_circuit"):
            os.makedirs(cache_dir, exist_ok=True)
            subprocess.run(["cp", "/tmp/repo/target/release/build_circuit", f"{cache_dir}/build_circuit"], check=True)
            subprocess.run(["cp", "/tmp/repo/target/release/eval_circuit", f"{cache_dir}/eval_circuit"], check=True)
            subprocess.run(["cp", "/tmp/repo/Cargo.toml", f"{cache_dir}/Cargo.toml"], check=True)
            build_cache.commit()

        jobs_dict[job_id] = json.dumps({
            "status": "completed",
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": output,
            "stderr": stderr,
            "duration_seconds": round(duration, 2),
            "build_key": key,
            "repo_url": repo_url,
            "commit": commit,
            "command": command,
        })

    except Exception as e:
        duration = time.time() - start
        jobs_dict[job_id] = json.dumps({
            "status": "error",
            "success": False,
            "error": str(e),
            "duration_seconds": round(duration, 2),
            "repo_url": repo_url,
            "commit": commit,
            "command": command,
        })


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-build")
def build_endpoint(request: dict):
    """Compile binaries for a repo+commit. Cache for fast repeated runs.
    
    Body: {"token": "...", "repo_url": "https://github.com/...", "commit": "sha"}
    Returns: {"job_id": "...", "build_key": "...", "status": "building"}
    """
    if err := check_auth(request):
        return {"error": err}

    repo_url = request.get("repo_url", "")
    commit = request.get("commit", "")
    if not repo_url or not commit:
        return {"error": "missing required fields: repo_url, commit"}
    if not repo_url.startswith("https://github.com/"):
        return {"error": "repo_url must be a public GitHub HTTPS URL"}

    job_id = str(uuid.uuid4())
    key = cache_key(repo_url, commit)

    jobs_dict[job_id] = json.dumps({"status": "queued", "build_key": key})
    do_build.spawn(job_id=job_id, repo_url=repo_url, commit=commit)

    return {"job_id": job_id, "build_key": key, "status": "queued"}


@app.function(image=api_image, secrets=[auth_secret], timeout=120, min_containers=1)
@modal.fastapi_endpoint(method="POST", label="ecdsafail-run")
def run_endpoint(request: dict):
    """Run benchmark using a pre-compiled build_key. Synchronous — returns result directly (~7s on 8 cores).
    
    Body: {"token": "...", "build_key": "...", "env_vars": {"DIALOG_REROLL": "123"}, "command": "./build_circuit && ./eval_circuit_par"}
    Returns: {"status": "completed", "success": true, "output": "...", "duration_seconds": 7.2, ...}
    
    For async (non-blocking) calls, pass "async": true to get a job_id and poll with /poll.
    """
    if err := check_auth(request):
        return {"error": err}

    build_key = request.get("build_key", "")
    if not build_key:
        return {"error": "missing build_key (run /build first)"}

    env_vars = request.get("env_vars", {})
    command = request.get("command", None)
    is_async = request.get("async", False)

    job_id = str(uuid.uuid4())
    jobs_dict[job_id] = json.dumps({"status": "queued", "build_key": build_key})

    if is_async:
        do_run.spawn(job_id=job_id, build_key=build_key, env_vars=env_vars, command=command)
        return {"job_id": job_id, "status": "queued"}

    # Synchronous: block until done and return result directly
    do_run.remote(job_id=job_id, build_key=build_key, env_vars=env_vars, command=command)
    try:
        return json.loads(jobs_dict[job_id])
    except KeyError:
        return {"error": "job result not found"}


@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-submit")
def submit_job(request: dict):
    """Full run: clone + compile + run in one shot (slower, ~66s first time).
    Automatically caches the build for future /run calls.
    
    Body: {"token": "...", "repo_url": "...", "commit": "...", "command": "...", "env_vars": {...}}
    Returns: {"job_id": "...", "build_key": "...", "status": "queued"}
    """
    if err := check_auth(request):
        return {"error": err}

    repo_url = request.get("repo_url", "")
    commit = request.get("commit", "")
    command = request.get("command", "")
    env_vars = request.get("env_vars", {})

    if not repo_url or not commit or not command:
        return {"error": "missing required fields: repo_url, commit, command"}
    if not repo_url.startswith("https://github.com/"):
        return {"error": "repo_url must be a public GitHub HTTPS URL"}

    job_id = str(uuid.uuid4())
    key = cache_key(repo_url, commit)

    jobs_dict[job_id] = json.dumps({"status": "queued", "build_key": key})
    do_full_run.spawn(job_id=job_id, repo_url=repo_url, commit=commit, command=command, env_vars=env_vars)

    return {"job_id": job_id, "build_key": key, "status": "queued"}


@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-batch")
def batch_endpoint(request: dict):
    """Launch many runs in parallel with different env vars. For seed search.
    
    Body: {"token": "...", "build_key": "...", "runs": [{"env_vars": {"DIALOG_REROLL": "0"}}, {"env_vars": {"DIALOG_REROLL": "1"}}, ...]}
    Returns: {"job_ids": ["uuid1", "uuid2", ...], "status": "queued"}
    
    Max 100 runs per batch.
    """
    if err := check_auth(request):
        return {"error": err}

    build_key = request.get("build_key", "")
    if not build_key:
        return {"error": "missing build_key (run /build first)"}

    runs = request.get("runs", [])
    if not runs:
        return {"error": "missing runs array"}
    if len(runs) > 100:
        return {"error": "max 100 runs per batch"}

    job_ids = []
    for run_spec in runs:
        env_vars = run_spec.get("env_vars", {})
        command = run_spec.get("command", None)
        job_id = str(uuid.uuid4())
        jobs_dict[job_id] = json.dumps({"status": "queued", "build_key": build_key})
        do_run.spawn(job_id=job_id, build_key=build_key, env_vars=env_vars, command=command)
        job_ids.append(job_id)

    return {"job_ids": job_ids, "count": len(job_ids), "status": "queued"}


@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-sweep")
def sweep_endpoint(request: dict):
    """Island search: spread MANY rerolls across containers, K rerolls per container.

    Unlike /batch (1 reroll per container), each container here loops over a chunk of
    rerolls, reusing its copied binaries — far less per-reroll overhead. Each shard
    short-circuits the moment it hits a clean island (any_fail==0) unless disabled.

    Body: {
      "token": "...", "build_key": "...",
      "env_vars": {"DIALOG_GCD_ACTIVE_ITERATIONS": "394"},  # base config, applied to all
      "rerolls": [0,1,2,...],            # explicit reroll values, OR
      "reroll_start": 0, "reroll_count": 1000,  # contiguous range
      "per_container": 50,               # rerolls handled by each function (default 50)
      "reroll_var": "DIALOG_REROLL",     # which nonce to vary (default)
      "stop_on_island": true             # short-circuit a shard on first island
    }
    Returns: {"job_ids": [...], "n_containers": M, "n_rerolls": N}
    Poll with /poll; each shard result has .sweep (per-reroll fails), .island, .total_failures.
    Max 100 containers per call.
    """
    if err := check_auth(request):
        return {"error": err}

    build_key = request.get("build_key", "")
    if not build_key:
        return {"error": "missing build_key (run /build first)"}

    rerolls = request.get("rerolls")
    if not rerolls:
        start = int(request.get("reroll_start", 0))
        count = int(request.get("reroll_count", 0))
        if count <= 0:
            return {"error": "provide rerolls[] or reroll_count"}
        rerolls = list(range(start, start + count))

    per = int(request.get("per_container", 50))
    if per < 1:
        return {"error": "per_container must be >= 1"}
    base_env = request.get("env_vars", {})
    reroll_var = request.get("reroll_var", "DIALOG_REROLL")
    stop_on_island = request.get("stop_on_island", True)

    chunks = [rerolls[i:i + per] for i in range(0, len(rerolls), per)]
    if len(chunks) > 100:
        return {"error": f"{len(chunks)} containers needed; max 100. Raise per_container or reduce rerolls."}

    job_ids = []
    for chunk in chunks:
        job_id = str(uuid.uuid4())
        jobs_dict[job_id] = json.dumps({"status": "queued", "build_key": build_key, "n_rerolls": len(chunk)})
        do_sweep.spawn(job_id=job_id, build_key=build_key, rerolls=chunk,
                       base_env=base_env, reroll_var=reroll_var, stop_on_island=stop_on_island)
        job_ids.append(job_id)

    return {"job_ids": job_ids, "n_containers": len(job_ids),
            "n_rerolls": len(rerolls), "per_container": per, "status": "queued"}


@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-search")
def search_endpoint(request: dict):
    """Dedicated island hunt with the optimized search_circuit binary.

    Partitions a contiguous nonce range across containers. Each container builds
    the circuit ONCE, pre-hashes the fixed prefix once, then for each nonce only
    re-absorbs the 96-op tail (DIALOG_TAIL_NONCE) + early-exits eval on first
    failing shot — ~1s/reroll vs the ~5s naive build_circuit+eval loop. Each
    container is internally multi-threaded across nonces and short-circuits the
    whole container on the first island.

    Body: {
      "token": "...", "build_key": "...",
      "env_vars": {"DIALOG_GCD_ACTIVE_ITERATIONS": "394"},  # base config (applied to all)
      "nonce_start": 0, "nonce_count": 306000,              # contiguous nonce range
      "per_container": 5000,                                # nonces per container (default 5000)
      "stop_on_island": true,                               # short-circuit a shard on first island
      "count_all": false,                                   # validation mode (full eval, no early-exit)
      "threads": null                                       # override worker threads (default = cores)
    }
    Returns: {"job_ids": [...], "n_containers": M, "nonce_count": N}
    Poll with /poll; each shard result has .islands (list of island nonces), .checked, .rate_per_s.
    Max 100 containers per call.
    """
    if err := check_auth(request):
        return {"error": err}

    build_key = request.get("build_key", "")
    if not build_key:
        return {"error": "missing build_key (run /build first)"}

    nonce_start = int(request.get("nonce_start", 0))
    nonce_count = int(request.get("nonce_count", 0))
    if nonce_count <= 0:
        return {"error": "provide nonce_count > 0"}
    per = int(request.get("per_container", 5000))
    if per < 1:
        return {"error": "per_container must be >= 1"}
    base_env = request.get("env_vars", {})
    stop_on_island = request.get("stop_on_island", True)
    count_all = request.get("count_all", False)
    threads = request.get("threads")

    max_containers = int(request.get("max_containers", 1000))
    starts = list(range(nonce_start, nonce_start + nonce_count, per))
    if len(starts) > max_containers:
        return {"error": f"{len(starts)} containers needed; max {max_containers}. Raise per_container or reduce nonce_count."}

    # NOTE: skip the per-job jobs_dict pre-write — do_search writes its own
    # "running" status on start. Pre-writing 1000 Dict entries serially made
    # large launches take ~100s; polling treats a not-yet-present job as pending.
    # Spawns are RPC-bound (~4/s serial); fan them out across threads so a
    # 1000-container launch returns in seconds instead of ~250s.
    from concurrent.futures import ThreadPoolExecutor
    specs = []
    for s in starts:
        c = min(per, nonce_start + nonce_count - s)
        specs.append((str(uuid.uuid4()), s, c))

    def _spawn(spec):
        job_id, s, c = spec
        do_search.spawn(job_id=job_id, build_key=build_key, start=s, count=c,
                        base_env=base_env, stop_on_island=stop_on_island,
                        count_all=count_all, threads=threads)
        return job_id

    with ThreadPoolExecutor(max_workers=64) as ex:
        job_ids = list(ex.map(_spawn, specs))

    return {"job_ids": job_ids, "n_containers": len(job_ids),
            "nonce_count": nonce_count, "per_container": per, "status": "queued"}


@app.function(image=api_image, secrets=[auth_secret])
@modal.fastapi_endpoint(method="POST", label="ecdsafail-poll")
def poll_job(request: dict):
    """Poll one or more job statuses.
    
    Body: {"token": "...", "job_id": "..."} or {"token": "...", "job_ids": ["...", "..."]}
    Returns: single job state, or {"results": {id: state, ...}} for batch
    """
    if err := check_auth(request):
        return {"error": err}

    # Support batch polling
    job_ids = request.get("job_ids", [])
    job_id = request.get("job_id", "")

    if job_ids:
        results = {}
        for jid in job_ids:
            try:
                results[jid] = json.loads(jobs_dict[jid])
            except KeyError:
                results[jid] = {"error": "job not found"}
        return {"results": results}

    if not job_id:
        return {"error": "missing job_id or job_ids"}

    try:
        result = jobs_dict[job_id]
        return json.loads(result)
    except KeyError:
        return {"error": "job not found"}
