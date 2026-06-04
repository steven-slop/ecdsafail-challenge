//! search_circuit — dedicated Fiat-Shamir island hunter.
//!
//! Builds the circuit ONCE in-process (via quantum_ecc::point_add::build), then
//! for each candidate `tail nonce` only re-absorbs the fixed-length 96-op tail
//! block into a pre-hashed prefix sponge and runs the 9024-shot eval with
//! early-exit on the first failing shot. This avoids the ~1.66s full SHAKE256
//! re-hash and the ~711MB ops.bin regen per reroll that the naive loop pays.
//!
//! The tail nonce mechanism lives in point_add (DIALOG_TAIL_NONCE): a constant
//! 2*NONCE_BITS block of identity X;X pairs at the very end of the op stream,
//! each pair targeting tx[0] (bit 0) or tx[1] (bit 1). Identity => no circuit
//! behavior change; the per-op q_target bytes reseed the Fiat-Shamir inputs.
//!
//! Env:
//!   SEARCH_START   (u64)  first nonce to test                  (default 0)
//!   SEARCH_COUNT   (u64)  number of nonces to test             (default 1000)
//!   SEARCH_THREADS (usize) worker threads                      (default = cores)
//!   COUNT_ALL=1           validation mode: full eval per nonce, print per-nonce
//!                         any_fail counts (no early-exit), to confirm the search
//!                         binary reproduces the same physics as eval_circuit_par.
//!   STOP_ON_ISLAND=1     stop all workers after the first island (default on).

use alloy_primitives::U256;
use quantum_ecc::circuit::{analyze_ops, Op, QubitOrBit};
use quantum_ecc::sim::Simulator;
use quantum_ecc::weierstrass_elliptic_curve::WeierstrassEllipticCurve;
use sha3::{
    digest::{ExtendableOutput, Update, XofReader},
    Shake256,
};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Instant;

const NUM_TESTS: usize = 9024;
const BATCH: usize = 64;
const NONCE_BITS: u32 = 48; // MUST match point_add DIALOG_TAIL_NONCE block
const TAIL_OPS: usize = (NONCE_BITS as usize) * 2;
const FS_DOMAIN: &[u8] = b"quantum_ecc-fiat-shamir-v2";
const BATCH_DOMAIN: &[u8] = b"eval-parallel-batch-rng";

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

/// Pack one op into the 49-byte form fiat_shamir_seed uses (kind:u8 + 6*u64 LE).
#[inline]
fn pack_op(op: &Op, out: &mut [u8]) {
    out[0] = op.kind as u8;
    out[1..9].copy_from_slice(&op.q_control2.0.to_le_bytes());
    out[9..17].copy_from_slice(&op.q_control1.0.to_le_bytes());
    out[17..25].copy_from_slice(&op.q_target.0.to_le_bytes());
    out[25..33].copy_from_slice(&op.c_target.0.to_le_bytes());
    out[33..41].copy_from_slice(&op.c_condition.0.to_le_bytes());
    out[41..49].copy_from_slice(&op.r_target.0.to_le_bytes());
}

/// Build the prefix sponge: domain + total_len + all prefix ops absorbed.
/// Returns a clonable Shake256 hasher positioned right before the tail.
fn prefix_hasher(total_len: usize, prefix: &[Op]) -> Shake256 {
    let mut h = Shake256::default();
    h.update(FS_DOMAIN);
    h.update(&(total_len as u64).to_le_bytes());
    const CHUNK: usize = 1024;
    let mut buf = vec![0u8; CHUNK * 49];
    for chunk in prefix.chunks(CHUNK) {
        for (i, op) in chunk.iter().enumerate() {
            pack_op(op, &mut buf[i * 49..]);
        }
        h.update(&buf[..chunk.len() * 49]);
    }
    h
}

fn per_batch_xof(base_seed: &[u8; 32], batch_idx: usize) -> sha3::Shake256Reader {
    let mut hasher = Shake256::default();
    hasher.update(BATCH_DOMAIN);
    hasher.update(base_seed);
    hasher.update(&(batch_idx as u64).to_le_bytes());
    hasher.finalize_xof()
}

/// Result of evaluating one nonce. In hunt mode we early-exit, so `any_fail`
/// is only exact when 0 (island) or counts the failures up to early-exit.
struct NonceResult {
    nonce: u64,
    any_fail: usize,
    is_island: bool,
}

/// Evaluate a single nonce. `early_exit`: stop at first failing batch.
#[allow(clippy::too_many_arguments)]
fn eval_nonce(
    nonce: u64,
    base: &Shake256,
    x0packed: &[u8; 49],
    x1packed: &[u8; 49],
    prefix: &[Op],
    x0op: &Op,
    x1op: &Op,
    curve: &WeierstrassEllipticCurve,
    regs: &[Vec<QubitOrBit>],
    total_qubits: usize,
    num_bits: usize,
    early_exit: bool,
) -> NonceResult {
    // 1) incremental hash: clone prefix sponge, absorb the 96-op tail.
    let mut h = base.clone();
    let mut tbuf = [0u8; TAIL_OPS * 49];
    let mut off = 0;
    for i in 0..NONCE_BITS {
        let p = if (nonce >> i) & 1 == 1 { x1packed } else { x0packed };
        tbuf[off..off + 49].copy_from_slice(p);
        off += 49;
        tbuf[off..off + 49].copy_from_slice(p);
        off += 49;
    }
    h.update(&tbuf);
    let mut xof = h.finalize_xof();

    // 2) read raw_keys (9024 * 2 * 32 B) then base_seed (32 B), sequential.
    let mut raw_keys: Vec<([u8; 32], [u8; 32])> = Vec::with_capacity(NUM_TESTS);
    for _ in 0..NUM_TESTS {
        let mut a = [0u8; 32];
        let mut b = [0u8; 32];
        xof.read(&mut a);
        xof.read(&mut b);
        raw_keys.push((a, b));
    }
    let mut base_seed = [0u8; 32];
    xof.read(&mut base_seed);

    // 3) build the tail ops vec (96 ops) for this nonce.
    let mut tail: Vec<Op> = Vec::with_capacity(TAIL_OPS);
    for i in 0..NONCE_BITS {
        let op = if (nonce >> i) & 1 == 1 { *x1op } else { *x0op };
        tail.push(op);
        tail.push(op);
    }

    // 4) lazily evaluate batches with early-exit. Inputs computed per batch from
    //    raw_keys (None collisions ~never happen for random 256-bit scalars, so
    //    global index == valid index; the official eval re-validates the island).
    let num_batches = (NUM_TESTS + BATCH - 1) / BATCH;
    let mut any_fail = 0usize;
    let mut island = true;
    for batch in 0..num_batches {
        let bs = BATCH.min(NUM_TESTS - batch * BATCH);
        let cond_mask: u64 = if bs == 64 { u64::MAX } else { (1u64 << bs) - 1 };

        // compute this batch's EC inputs
        let mut tgt = [((U256::ZERO, U256::ZERO)); BATCH];
        let mut ofs = [((U256::ZERO, U256::ZERO)); BATCH];
        let mut exp = [((U256::ZERO, U256::ZERO)); BATCH];
        let mut bad = false;
        for shot in 0..bs {
            let i = batch * BATCH + shot;
            let k1 = U256::from_le_bytes(raw_keys[i].0);
            let k2 = U256::from_le_bytes(raw_keys[i].1);
            let t = curve.mul(curve.gx, curve.gy, k1);
            let o = curve.mul(curve.gx, curve.gy, k2);
            if t.0 == o.0
                || (t.0.is_zero() && t.1.is_zero())
                || (o.0.is_zero() && o.1.is_zero())
            {
                bad = true;
                break;
            }
            let e = curve.add(t.0, t.1, o.0, o.1);
            tgt[shot] = t;
            ofs[shot] = o;
            exp[shot] = e;
        }
        if bad {
            // collision: treat as non-island (astronomically rare).
            island = false;
            any_fail += 1;
            if early_exit {
                break;
            }
            continue;
        }

        let mut batch_xof = per_batch_xof(&base_seed, batch);
        let mut sim = Simulator::new(total_qubits, num_bits, &mut batch_xof);
        for shot in 0..bs {
            sim.set_register(&regs[0], tgt[shot].0, shot);
            sim.set_register(&regs[1], tgt[shot].1, shot);
            sim.set_register(&regs[2], ofs[shot].0, shot);
            sim.set_register(&regs[3], ofs[shot].1, shot);
        }

        sim.apply_iter(prefix.iter().chain(tail.iter()));

        // classical check (per-shot bitmask)
        let mut classical_mask: u64 = 0;
        for shot in 0..bs {
            let gx = sim.get_register(&regs[0], shot);
            let gy = sim.get_register(&regs[1], shot);
            if gx != exp[shot].0 || gy != exp[shot].1 {
                classical_mask |= 1u64 << shot;
            }
        }
        // phase check
        let phase_mask = sim.phase & cond_mask;
        // ancilla check: zero register qubits, then any remaining dirty qubit
        for register in regs {
            for qb in register {
                if let QubitOrBit::Qubit(q) = *qb {
                    *sim.qubit_mut(q) = 0;
                }
            }
        }
        let mut ancilla_mask: u64 = 0;
        for q in 0..(total_qubits as u64) {
            let v = sim.qubit(quantum_ecc::circuit::QubitId(q)) & cond_mask;
            if v != 0 {
                ancilla_mask |= v;
            }
        }

        let fails = (classical_mask | phase_mask | ancilla_mask).count_ones() as usize;
        if fails > 0 {
            island = false;
            any_fail += fails;
            if early_exit {
                break;
            }
        }
    }

    NonceResult {
        nonce,
        any_fail,
        is_island: island,
    }
}

fn main() {
    let t_total = Instant::now();
    let start: u64 = std::env::var("SEARCH_START").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    let count: u64 = std::env::var("SEARCH_COUNT").ok().and_then(|s| s.parse().ok()).unwrap_or(1000);
    let num_threads: usize = std::env::var("SEARCH_THREADS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| std::thread::available_parallelism().map(|p| p.get()).unwrap_or(8));
    let count_all = std::env::var("COUNT_ALL").ok().as_deref() == Some("1");
    let stop_on_island = std::env::var("STOP_ON_ISLAND").ok().as_deref() != Some("0");
    let early_exit = !count_all;

    println!("=== search_circuit ===");
    println!("start={start} count={count} threads={num_threads} count_all={count_all}");

    // Build the circuit twice to capture the tail x0/x1 ops + the fixed prefix.
    std::env::set_var("DIALOG_TAIL_NONCE", "0");
    let t0 = Instant::now();
    let ops0 = quantum_ecc::point_add::build();
    eprintln!("  build nonce=0: {:.2}s ({} ops)", t0.elapsed().as_secs_f64(), ops0.len());
    let nonce_max = (1u64 << NONCE_BITS) - 1;
    std::env::set_var("DIALOG_TAIL_NONCE", nonce_max.to_string());
    let t0 = Instant::now();
    let ops1 = quantum_ecc::point_add::build();
    eprintln!("  build nonce=max: {:.2}s ({} ops)", t0.elapsed().as_secs_f64(), ops1.len());

    assert_eq!(ops0.len(), ops1.len(), "tail nonce changed op count");
    let total_len = ops0.len();
    assert!(total_len > TAIL_OPS, "stream shorter than tail");
    let prefix_len = total_len - TAIL_OPS;
    // verify prefixes identical
    assert!(ops0[..prefix_len] == ops1[..prefix_len], "prefix differs between nonces");
    // verify tails are uniform (all tx[0] in ops0, all tx[1] in ops1)
    let x0op = ops0[prefix_len];
    let x1op = ops1[prefix_len];
    for i in 0..TAIL_OPS {
        assert!(ops0[prefix_len + i] == x0op, "ops0 tail not uniform at {i}");
        assert!(ops1[prefix_len + i] == x1op, "ops1 tail not uniform at {i}");
    }
    assert!(x0op != x1op, "x0 and x1 tail ops identical");
    eprintln!("  prefix_len={prefix_len} tail_ops={TAIL_OPS}");
    eprintln!("  x0 q_target={} x1 q_target={}", x0op.q_target.0, x1op.q_target.0);

    // drop ops1; keep ops0 and use its prefix slice.
    drop(ops1);
    let mut x0packed = [0u8; 49];
    let mut x1packed = [0u8; 49];
    pack_op(&x0op, &mut x0packed);
    pack_op(&x1op, &mut x1packed);

    // analyze for register layout (use full ops0; tail adds no registers).
    let (total_qubits, num_bits, _num_regs, regs) = analyze_ops(ops0.iter());
    assert_eq!(regs.len(), 4, "expected 4 registers");
    let total_qubits = total_qubits as usize;
    let num_bits = num_bits as usize;

    // pre-hash the fixed prefix once.
    let t0 = Instant::now();
    let base = prefix_hasher(total_len, &ops0[..prefix_len]);
    eprintln!("  prefix hash: {:.2}s", t0.elapsed().as_secs_f64());

    let curve = secp256k1();
    let prefix: &[Op] = &ops0[..prefix_len];

    // shared work counter + results
    let next = AtomicU64::new(start);
    let end = start + count;
    let checked = AtomicU64::new(0);
    let found = AtomicBool::new(false);
    let islands: Mutex<Vec<u64>> = Mutex::new(Vec::new());
    // for COUNT_ALL: accumulate failure stats
    let total_fail = AtomicU64::new(0);

    let t_search = Instant::now();
    std::thread::scope(|scope| {
        for tid in 0..num_threads {
            let next = &next;
            let checked = &checked;
            let found = &found;
            let islands = &islands;
            let total_fail = &total_fail;
            let base = &base;
            let x0packed = &x0packed;
            let x1packed = &x1packed;
            let x0op = &x0op;
            let x1op = &x1op;
            let curve = &curve;
            let regs = &regs;
            let t_search = &t_search;
            scope.spawn(move || loop {
                if stop_on_island && found.load(Ordering::Relaxed) {
                    break;
                }
                let nonce = next.fetch_add(1, Ordering::Relaxed);
                if nonce >= end {
                    break;
                }
                let r = eval_nonce(
                    nonce, base, x0packed, x1packed, prefix, x0op, x1op, curve, regs,
                    total_qubits, num_bits, early_exit,
                );
                let done = checked.fetch_add(1, Ordering::Relaxed) + 1;
                if count_all {
                    total_fail.fetch_add(r.any_fail as u64, Ordering::Relaxed);
                    println!("TRIAL nonce={} any_fail={}", r.nonce, r.any_fail);
                }
                if r.is_island {
                    found.store(true, Ordering::Relaxed);
                    islands.lock().unwrap().push(r.nonce);
                    println!("ISLAND nonce={}", r.nonce);
                    use std::io::Write;
                    std::io::stdout().flush().ok();
                }
                if tid == 0 && done % 200 == 0 {
                    let el = t_search.elapsed().as_secs_f64();
                    eprintln!(
                        "  PROGRESS checked={} elapsed={:.1}s rate={:.1}/s",
                        done, el, done as f64 / el
                    );
                }
            });
        }
    });

    let el = t_search.elapsed().as_secs_f64();
    let done = checked.load(Ordering::Relaxed);
    let isl = islands.lock().unwrap();
    let rate = if el > 0.0 { done as f64 / el } else { 0.0 };
    println!(
        "SEARCH_DONE start={} count={} checked={} islands={:?} elapsed={:.2}s rate={:.2}/reroll/s total={:.2}s",
        start, count, done, *isl, el, rate, t_total.elapsed().as_secs_f64()
    );
    if count_all {
        let tf = total_fail.load(Ordering::Relaxed);
        println!(
            "COUNT_ALL_SUMMARY checked={} total_fail={} avg_f={:.3}",
            done, tf, tf as f64 / done.max(1) as f64
        );
    }
}
