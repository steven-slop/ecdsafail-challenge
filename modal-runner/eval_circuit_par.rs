//! Parallel eval_circuit — splits 9024-shot simulation across all available cores.
//!
//! Uses per-batch XOF derivation (not pre-reading GB of XOF data).
//! Each batch gets an independent SHAKE256 reader seeded from
//! (ops_hash, batch_index), so results match serial for correct circuits.

use alloy_primitives::U256;
use quantum_ecc::circuit::{
    analyze_ops, BitId, Op, OperationType, QubitId, QubitOrBit, RegisterId,
};
use quantum_ecc::sim::Simulator;
use quantum_ecc::weierstrass_elliptic_curve::WeierstrassEllipticCurve;
use sha3::{
    digest::{ExtendableOutput, Update, XofReader},
    Shake256,
};
use std::fs;
use std::time::Instant;

const OPS_PATH: &str = "ops.bin";
const MAGIC: &[u8; 8] = b"QECCOPS1";
const FIELD_BYTES: usize = 8;
const OP_FIELDS: usize = 7;
const OP_BYTES: usize = OP_FIELDS * FIELD_BYTES;
const MAX_OPS: u64 = 4_000_000_000;
const NUM_TESTS: usize = 9024;
const BATCH: usize = 64;

// ─── ops.bin loader (identical to eval_circuit.rs) ─────────────────────────

fn op_kind_from_u32(v: u32) -> Option<OperationType> {
    Some(match v {
        0 => OperationType::Neg,
        1 => OperationType::Register,
        2 => OperationType::AppendToRegister,
        3 => OperationType::BitInvert,
        4 => OperationType::BitStore0,
        5 => OperationType::BitStore1,
        6 => OperationType::X,
        7 => OperationType::Z,
        8 => OperationType::CX,
        9 => OperationType::CZ,
        10 => OperationType::Swap,
        11 => OperationType::R,
        12 => OperationType::Hmr,
        13 => OperationType::CCX,
        14 => OperationType::CCZ,
        15 => OperationType::PushCondition,
        16 => OperationType::PopCondition,
        17 => OperationType::DebugPrint,
        _ => return None,
    })
}

fn read_u64(bytes: &[u8], off: usize) -> u64 {
    u64::from_le_bytes(bytes[off..off + 8].try_into().unwrap())
}

fn load_ops(path: &str) -> Result<Vec<Op>, String> {
    let bytes = fs::read(path).map_err(|e| format!("read {path}: {e}"))?;
    if bytes.len() < MAGIC.len() + 8 {
        return Err(format!("{path}: too short ({} bytes)", bytes.len()));
    }
    if &bytes[..MAGIC.len()] != MAGIC {
        return Err(format!("{path}: bad magic"));
    }
    let n = u64::from_le_bytes(bytes[MAGIC.len()..MAGIC.len() + 8].try_into().unwrap());
    if n > MAX_OPS {
        return Err(format!("{path}: op count {n} exceeds cap {MAX_OPS}"));
    }
    let n = n as usize;
    let need = MAGIC.len() + 8 + n.saturating_mul(OP_BYTES);
    if bytes.len() != need {
        return Err(format!(
            "{path}: length mismatch: got {} expected {need} for {n} ops",
            bytes.len()
        ));
    }
    let mut ops = Vec::with_capacity(n);
    let mut off = MAGIC.len() + 8;
    for i in 0..n {
        let kind_raw = u32::from_le_bytes(bytes[off..off + 4].try_into().unwrap());
        let kind = op_kind_from_u32(kind_raw)
            .ok_or_else(|| format!("op {i}: unknown kind {kind_raw}"))?;
        let q_control2 = QubitId(read_u64(&bytes, off + 8));
        let q_control1 = QubitId(read_u64(&bytes, off + 16));
        let q_target = QubitId(read_u64(&bytes, off + 24));
        let c_target = BitId(read_u64(&bytes, off + 32));
        let c_condition = BitId(read_u64(&bytes, off + 40));
        let r_target = RegisterId(read_u64(&bytes, off + 48));
        let op = Op {
            kind, q_control2, q_control1, q_target, c_target, c_condition, r_target,
        };
        let validated = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| op.validate()));
        if let Err(e) = validated {
            let msg = e
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| e.downcast_ref::<&'static str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "validation panic".to_string());
            return Err(format!("op {i}: {msg}"));
        }
        ops.push(op);
        off += OP_BYTES;
    }
    Ok(ops)
}

// ─── secp256k1 ─────────────────────────────────────────────────────────────

fn secp256k1() -> WeierstrassEllipticCurve {
    WeierstrassEllipticCurve {
        modulus: U256::from_str_radix(
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F", 16).unwrap(),
        a: U256::from(0),
        b: U256::from(7),
        gx: U256::from_str_radix(
            "79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798", 16).unwrap(),
        gy: U256::from_str_radix(
            "483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8", 16).unwrap(),
        order: U256::from_str_radix(
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16).unwrap(),
    }
}

// ─── Fiat-Shamir seed ──────────────────────────────────────────────────────

fn fiat_shamir_seed(ops: &[Op]) -> sha3::Shake256Reader {
    let mut hasher = Shake256::default();
    hasher.update(b"quantum_ecc-fiat-shamir-v2");
    hasher.update(&(ops.len() as u64).to_le_bytes());
    // Batch ops into larger buffers to reduce per-call overhead
    // (equivalent to individual update calls since sponge is streaming)
    const CHUNK: usize = 1024;
    let mut buf = vec![0u8; CHUNK * 49];
    for chunk in ops.chunks(CHUNK) {
        for (i, op) in chunk.iter().enumerate() {
            let off = i * 49;
            buf[off] = op.kind as u8;
            buf[off+1..off+9].copy_from_slice(&op.q_control2.0.to_le_bytes());
            buf[off+9..off+17].copy_from_slice(&op.q_control1.0.to_le_bytes());
            buf[off+17..off+25].copy_from_slice(&op.q_target.0.to_le_bytes());
            buf[off+25..off+33].copy_from_slice(&op.c_target.0.to_le_bytes());
            buf[off+33..off+41].copy_from_slice(&op.c_condition.0.to_le_bytes());
            buf[off+41..off+49].copy_from_slice(&op.r_target.0.to_le_bytes());
        }
        hasher.update(&buf[..chunk.len() * 49]);
    }
    hasher.finalize_xof()
}

/// Derive a per-batch XOF from a base seed + batch index.
/// Fast: just hashes 40 bytes per batch.
fn per_batch_xof(base_seed: &[u8; 32], batch_idx: usize) -> sha3::Shake256Reader {
    let mut hasher = Shake256::default();
    hasher.update(b"eval-parallel-batch-rng");
    hasher.update(base_seed);
    hasher.update(&(batch_idx as u64).to_le_bytes());
    hasher.finalize_xof()
}

// ─── Per-thread result ─────────────────────────────────────────────────────

struct ThreadResult {
    classical_failures: usize,
    phase_garbage_batches: usize,
    ancilla_garbage_batches: usize,
    // Per-shot failure counts (popcount over the 64-shot batch bitmask).
    classical_fail_shots: usize,
    phase_fail_shots: usize,
    ancilla_fail_shots: usize,
    // Shots failing ANY check (union of the three masks, no double counting).
    any_fail_shots: usize,
    toffoli_gates: u64,
    clifford_gates: u64,
    n_shots: usize,
    fail_reason: Option<String>,
}

// ─── main ──────────────────────────────────────────────────────────────────

fn main() {
    let note = {
        let mut args = std::env::args().skip(1);
        let mut n = String::new();
        while let Some(a) = args.next() {
            if a == "--note" { if let Some(v) = args.next() { n = v; } }
            else if let Some(rest) = a.strip_prefix("--note=") { n = rest.to_string(); }
        }
        n
    };

    println!("=== quantum_ecc: eval_circuit_par (parallel) ===\n");
    let t_total = Instant::now();

    // Load ops
    let t0 = Instant::now();
    let ops = match load_ops(OPS_PATH) {
        Ok(v) => v,
        Err(e) => { eprintln!("!! could not load {OPS_PATH}: {e}"); std::process::exit(1); }
    };
    eprintln!("  load ops: {:.2}s", t0.elapsed().as_secs_f64());
    println!("  loaded ops  : {}", ops.len());

    let t0 = Instant::now();
    let (total_qubits, num_bits, _num_regs, regs) = analyze_ops(ops.iter());
    eprintln!("  analyze_ops: {:.2}s", t0.elapsed().as_secs_f64());
    if regs.len() != 4 {
        eprintln!("expected 4 registers, got {}", regs.len());
        std::process::exit(1);
    }
    for (i, r) in regs.iter().enumerate() {
        if r.len() != 256 {
            eprintln!("register {i} should be 256 wide, got {}", r.len());
            std::process::exit(1);
        }
    }
    println!("  qubits      : {}", total_qubits);
    println!("  bits        : {}", num_bits);

    // Fiat-Shamir seed (hashing all ops)
    let t0 = Instant::now();
    let mut xof = fiat_shamir_seed(&ops);
    eprintln!("  fiat_shamir: {:.2}s", t0.elapsed().as_secs_f64());

    // Pre-read all XOF bytes for test input generation (sequential)
    let t0 = Instant::now();
    let mut raw_keys: Vec<([u8; 32], [u8; 32])> = Vec::with_capacity(NUM_TESTS);
    for _ in 0..NUM_TESTS {
        let mut rb = [[0u8; 32]; 2];
        xof.read(&mut rb[0]);
        xof.read(&mut rb[1]);
        raw_keys.push((rb[0], rb[1]));
    }
    // Derive base seed for per-batch XOFs
    let mut base_seed = [0u8; 32];
    xof.read(&mut base_seed);
    eprintln!("  xof read: {:.2}s", t0.elapsed().as_secs_f64());

    // Determine parallelism early
    let num_threads: usize = std::env::var("EVAL_THREADS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(|p| p.get())
                .unwrap_or(8)
        });

    // Generate test inputs in parallel (EC scalar muls are the bottleneck)
    let t0 = Instant::now();
    let curve = secp256k1();
    let test_inputs: Vec<Option<((U256, U256), (U256, U256), (U256, U256))>> =
        std::thread::scope(|scope| {
            let chunk_size = (raw_keys.len() + num_threads - 1) / num_threads;
            let handles: Vec<_> = raw_keys
                .chunks(chunk_size)
                .map(|chunk| {
                    let curve = &curve;
                    scope.spawn(move || {
                        chunk.iter().map(|(rb0, rb1)| {
                            let k1 = U256::from_le_bytes(*rb0);
                            let k2 = U256::from_le_bytes(*rb1);
                            let t = curve.mul(curve.gx, curve.gy, k1);
                            let o = curve.mul(curve.gx, curve.gy, k2);
                            if t.0 == o.0 || (t.0.is_zero() && t.1.is_zero())
                                || (o.0.is_zero() && o.1.is_zero()) {
                                return None;
                            }
                            let e = curve.add(t.0, t.1, o.0, o.1);
                            Some((t, o, e))
                        }).collect::<Vec<_>>()
                    })
                })
                .collect();
            handles.into_iter().flat_map(|h| h.join().unwrap()).collect()
        });

    let mut targets = Vec::with_capacity(NUM_TESTS);
    let mut offsets = Vec::with_capacity(NUM_TESTS);
    let mut expected = Vec::with_capacity(NUM_TESTS);
    for item in test_inputs {
        if let Some((t, o, e)) = item {
            targets.push(t);
            offsets.push(o);
            expected.push(e);
        }
    }
    eprintln!("  test inputs: {:.2}s ({} threads, {} valid)", t0.elapsed().as_secs_f64(), num_threads, targets.len());

    let n = targets.len();
    let num_batches = (n + BATCH - 1) / BATCH;
    let num_threads = num_threads.min(num_batches);

    println!("\n-- correctness tests ({} shots, {} threads) --", NUM_TESTS, num_threads);
    let t_eval = Instant::now();

    // Run batches in parallel
    let results: Vec<ThreadResult> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(num_threads);

        for thread_id in 0..num_threads {
            // Round-robin batch assignment
            let my_batches: Vec<usize> = (thread_id..num_batches)
                .step_by(num_threads)
                .collect();

            let ops = &ops;
            let targets = &targets;
            let offsets = &offsets;
            let expected = &expected;
            let layout_regs = &regs;

            handles.push(scope.spawn(move || {
                let mut classical_failures = 0usize;
                let mut phase_garbage_batches = 0usize;
                let mut ancilla_garbage_batches = 0usize;
                let mut classical_fail_shots = 0usize;
                let mut phase_fail_shots = 0usize;
                let mut ancilla_fail_shots = 0usize;
                let mut any_fail_shots = 0usize;
                let mut total_shots = 0usize;
                let mut first_fail: Option<String> = None;
                let mut total_toffoli = 0u64;
                let mut total_clifford = 0u64;

                for &batch in &my_batches {
                    let bs = BATCH.min(n - batch * BATCH);
                    let cond_mask: u64 = if bs == 64 { u64::MAX } else { (1u64 << bs) - 1 };

                    // Create per-batch XOF (fast: just hash 40 bytes)
                    let mut batch_xof = per_batch_xof(&base_seed, batch);
                    let mut sim = Simulator::new(
                        total_qubits as usize,
                        num_bits as usize,
                        &mut batch_xof,
                    );

                    // Set inputs
                    for shot in 0..bs {
                        let i = batch * BATCH + shot;
                        sim.set_register(&layout_regs[0], targets[i].0, shot);
                        sim.set_register(&layout_regs[1], targets[i].1, shot);
                        sim.set_register(&layout_regs[2], offsets[i].0, shot);
                        sim.set_register(&layout_regs[3], offsets[i].1, shot);
                    }

                    // Simulate
                    sim.apply_iter(ops.iter());

                    // Check classical correctness (per-shot bitmask)
                    let mut classical_mask: u64 = 0;
                    for shot in 0..bs {
                        let i = batch * BATCH + shot;
                        let gx = sim.get_register(&layout_regs[0], shot);
                        let gy = sim.get_register(&layout_regs[1], shot);
                        if gx != expected[i].0 || gy != expected[i].1 {
                            classical_failures += 1;
                            classical_mask |= 1u64 << shot;
                            if first_fail.is_none() {
                                first_fail = Some(format!(
                                    "CLASSICAL MISMATCH shot {i}: got ({:#x},{:#x}) exp ({:#x},{:#x})",
                                    gx, gy, expected[i].0, expected[i].1
                                ));
                            }
                        }
                    }

                    // Check phase (per-shot: sim.phase is a 64-bit mask over the batch)
                    let phase_mask = sim.phase & cond_mask;
                    if phase_mask != 0 {
                        phase_garbage_batches += 1;
                        if first_fail.is_none() {
                            first_fail = Some(format!(
                                "PHASE GARBAGE: global_phase = {:#018x} ({} live shots)",
                                phase_mask, bs
                            ));
                        }
                    }

                    // Check ancilla cleanup
                    for register in layout_regs {
                        for qb in register {
                            if let QubitOrBit::Qubit(q) = *qb {
                                *sim.qubit_mut(q) = 0;
                            }
                        }
                    }
                    // Ancilla: union the per-shot garbage masks across ALL qubits
                    // (a shot is bad if any qubit is dirty; no double counting).
                    let mut ancilla_mask: u64 = 0;
                    let mut first_garbage_q: Option<u64> = None;
                    for q in 0..total_qubits {
                        let v = sim.qubit(QubitId(q)) & cond_mask;
                        if v != 0 {
                            ancilla_mask |= v;
                            if first_garbage_q.is_none() { first_garbage_q = Some(q); }
                        }
                    }
                    if let Some(q) = first_garbage_q {
                        ancilla_garbage_batches += 1;
                        if first_fail.is_none() {
                            let v = sim.qubit(QubitId(q)) & cond_mask;
                            first_fail = Some(format!("ANCILLA GARBAGE: qubit {} = {:#018x}", q, v));
                        }
                    }

                    classical_fail_shots += classical_mask.count_ones() as usize;
                    phase_fail_shots += phase_mask.count_ones() as usize;
                    ancilla_fail_shots += ancilla_mask.count_ones() as usize;
                    any_fail_shots += (classical_mask | phase_mask | ancilla_mask).count_ones() as usize;

                    total_toffoli += sim.stats.toffoli_gates;
                    total_clifford += sim.stats.clifford_gates;
                    total_shots += bs;
                }

                ThreadResult {
                    classical_failures,
                    phase_garbage_batches,
                    ancilla_garbage_batches,
                    classical_fail_shots,
                    phase_fail_shots,
                    ancilla_fail_shots,
                    any_fail_shots,
                    toffoli_gates: total_toffoli,
                    clifford_gates: total_clifford,
                    n_shots: total_shots,
                    fail_reason: first_fail,
                }
            }));
        }

        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });

    let eval_elapsed = t_eval.elapsed().as_secs_f64();

    // Probe mode: report per-shot failure frequency instead of pass/fail.
    // (Disables the non-zero exit on failure so the caller always gets metrics.)
    let probe_mode = std::env::var("COUNT_ALL_FAILURES").ok().as_deref() == Some("1");

    // Aggregate
    let mut ok = true;
    let mut tot_classical = 0usize;
    let mut tot_phase = 0usize;
    let mut tot_ancilla = 0usize;
    let mut tot_classical_shots = 0usize;
    let mut tot_phase_shots = 0usize;
    let mut tot_ancilla_shots = 0usize;
    let mut tot_any_shots = 0usize;
    let mut tot_toffoli = 0u64;
    let mut tot_clifford = 0u64;
    let mut tot_shots = 0usize;
    let mut first_fail: Option<String> = None;

    for r in &results {
        tot_classical += r.classical_failures;
        tot_phase += r.phase_garbage_batches;
        tot_ancilla += r.ancilla_garbage_batches;
        tot_classical_shots += r.classical_fail_shots;
        tot_phase_shots += r.phase_fail_shots;
        tot_ancilla_shots += r.ancilla_fail_shots;
        tot_any_shots += r.any_fail_shots;
        tot_toffoli += r.toffoli_gates;
        tot_clifford += r.clifford_gates;
        tot_shots += r.n_shots;
        if r.fail_reason.is_some() && first_fail.is_none() {
            first_fail = r.fail_reason.clone();
        }
    }
    if tot_classical > 0 || tot_phase > 0 || tot_ancilla > 0 { ok = false; }

    let denom = tot_shots.max(1) as f64;
    let avg_tof = tot_toffoli as f64 / denom;
    let avg_cliff = tot_clifford as f64 / denom;

    println!("  tested shots            : {}", tot_shots);
    println!("  classical mismatches    : {}", tot_classical);
    println!("  phase-garbage batches   : {}", tot_phase);
    println!("  ancilla-garbage batches : {}", tot_ancilla);

    // Machine-parseable probe summary: per-shot failure counts + would-be score.
    // p_hat = any/n ; expected rerolls to find a clean 9024-island = exp(9024*p_hat).
    let p_hat = tot_any_shots as f64 / denom;
    let island_ln = 9024.0 * (1.0 - p_hat).max(1e-300_f64).ln(); // ln P(clean)
    println!(
        "PROBE any_fail={} classical={} phase={} ancilla={} n={} p_hat={:.6e} ln_p_island={:.3} toffoli={:.3} qubits={} score={:.0}",
        tot_any_shots, tot_classical_shots, tot_phase_shots, tot_ancilla_shots,
        tot_shots, p_hat, island_ln, avg_tof, total_qubits, avg_tof * total_qubits as f64
    );

    if !ok && !probe_mode {
        let reason = first_fail.unwrap_or_else(|| "(no detail)".into());
        eprintln!("\n!! correctness FAILED: {reason}");
        std::process::exit(1);
    }

    if ok {
        println!("  all {} shots OK", tot_shots);
    }

    println!("\n=== circuit metrics (secp256k1, n=256) ===");
    println!("  avg executed Toffoli  : {:.3}", avg_tof);
    println!("  avg executed Clifford : {:.3}", avg_cliff);
    println!("  total Toffoli (sum)   : {} over {} shots", tot_toffoli, tot_shots);
    println!("  total Clifford (sum)  : {}", tot_clifford);
    println!("  emitted ops           : {}", ops.len());
    println!("  qubits                : {}", total_qubits);
    eprintln!("  eval phase: {:.2}s ({} threads)", eval_elapsed, num_threads);
    println!("  wall time: {:.2}s", t_total.elapsed().as_secs_f64());

    if ok {
        println!("\n=== experiment OK ===");
    } else {
        println!("\n=== experiment FAILED (probe mode: {} / {} shots failed) ===", tot_any_shots, tot_shots);
    }
}
