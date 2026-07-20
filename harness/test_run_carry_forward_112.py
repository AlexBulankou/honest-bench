"""Offline tests for the #112 daily-refresh carry-forward of the six producer-less
blocks — kata_activation (#3942), concurrent_burst (#4021), warm_pool_acquisition
(#4083), at_scale_contention (#810), cluster_saturation (hb#132),
provisioning_rate_sweep (#4086). No cluster, no I/O
beyond self-managed tempfiles.

Run with bare python3 (the auto-refresh GH-runner needs nothing extra):
  python3 -m harness.test_run_carry_forward_112
or directly:
  python3 harness/test_run_carry_forward_112.py

Why this file exists (#112 structural gap): unlike scale_proof / stepup / warm_vs_cold,
these blocks have NO in-process producer — they are written only by manual
data-only fires straight into latest.json. Before #112, run.main() passed only
scale_proof/stepup/warm_vs_cold into build_results and build_results had no
warm_pool_acquisition param at all, so the daily `harness.run --product sandbox`
refresh did a wholesale write that SILENTLY DROPPED these blocks from the public
table. The fix mirrors the carry_prior_* posture for each: fresh is always None here
(no producer), so the prior committed block is carried forward verbatim (honest, no
auto-decay). These tests lock that carry + read + the build_results wiring. The
matching emitter⇄renderer convergence lock (a carried warm_pool_acquisition block
round-tripping through render's cleaner) lives in render/test_cross_contract.py,
which can import both sides across the render/harness sys.path split.
"""

from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import pathlib
import tempfile

from harness import results_schema as rs
from harness import run


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


_GEN_AT = "2026-07-01T12:00:00Z"

# Representative prior blocks (the shape a manual data-only fire committed). Each
# carries its own measured_at so the carried-block-keeps-its-date assertion is real.
_PRIOR_KATA = {
    "runtime_class": "kata-microvm",
    "microvm_activation_ms": 2000,
    "warm_ready_ms": 3000,
    "cold_ready": [
        {"image": "debian:12", "image_pull_ms": 900, "ready_ms": 3000},
        {"image": "ubuntu:24.04", "image_pull_ms": 887, "ready_ms": 5000},
    ],
    "guest_kernel": "6.18.35",
    "host_kernel": "6.8.0-1054-gke",
    "hypervisor": "Cloud Hypervisor",
    "kata_version": "3.32.0",
    "warm_image": "ubuntu:24.04",
    "resume_status": "upstream-blocked",
    "n": 3,
    "measured_at": "2026-06-30T02:00:00Z",
}
_PRIOR_CB = {
    "legs": [
        {"n": 300, "mode": "warm", "ttfe_p50_ms": 6874.3, "ttfe_p95_ms": 9393.0,
         "exec_success_rate": 1.0},
        {"n": 300, "mode": "cold", "ttfe_p50_ms": 56029.4, "ttfe_p95_ms": 58412.4,
         "exec_success_rate": 1.0},
    ],
    "node_count": 20,
    "machine_type": "e2-standard-16",
    "measured_at": "2026-06-30T03:00:00Z",
}
_PRIOR_WPA = {
    "runtime_class": "gvisor",
    "acq_p50_ms": 2939.65,
    "acq_p95_ms": 3878.44,
    "acq_p99_ms": 4009.62,
    "n": 600,
    "offered_rate_per_s": 300,
    "warmpool_size": 600,
    "machine_type": "n2-standard-16",
    "node_count": 24,
    "measured_at": "2026-07-01T01:03:12Z",
}
_PRIOR_ASC = {
    "runtime_class": "gvisor",
    "pool_size": 30,
    "claim_count": 60,
    "ttfe_p50_ms": 1658.9,
    "ttfe_p95_ms": 2016.9,
    "bind_p50_ms": 1384.0,
    "bind_p95_ms": 1700.1,
    "exec_p50_ms": 279.9,
    "exec_p95_ms": 323.7,
    "exec_success_rate": 1.0,
    "node_count": 1,
    "machine_type": "e2-standard-16",
    "measured_at": "2026-07-01",
}
_PRIOR_CS = {
    "runtime_class": "gvisor",
    "pool_size": 600,
    "claim_count": 600,
    "node_count": 40,
    "ttfe_p50_ms": 8630.8,
    "ttfe_p95_ms": 12610.3,
    "bind_p50_ms": 8191.6,
    "bind_p95_ms": 12137.2,
    "exec_p50_ms": 467.0,
    "exec_p95_ms": 608.0,
    "exec_success_rate": 1.0,
    "thpt_under_5s_per_node": 0.064,
    "thpt_under_1s_per_node": 0.0,
    "thpt_under_5s_per_cluster": 2.558,
    "thpt_under_1s_per_cluster": 0.0,
    "thpt_cluster_node_count": 40,
    "outcome": "FAIL",
    "run_id": "c50e1c51a4c441f3a0705a2426a9a93c",
    "machine_type": "n2-standard-16",
    "measured_at": "2026-07-02",
}
_PRIOR_PRS = {
    "runtime_class": "gvisor",
    "ceiling_low_per_s": 100,
    "ceiling_high_per_s": 150,
    "rate_points": [
        {"offered_rate_per_s": 100, "warmpool_size": 1500, "converged": True,
         "ready_pct": 100.0, "elapsed_s": 301.0},
        {"offered_rate_per_s": 150, "warmpool_size": 2250, "converged": False,
         "ready_pct": 42.0, "timeout_s": 1125.0},
        {"offered_rate_per_s": 200, "warmpool_size": 3000, "converged": False,
         "ready_pct": 21.0, "timeout_s": 1880.0},
    ],
    "measured_at": "2026-07-01",
}

_BLOCKS = (
    ("kata_activation", run.carry_prior_kata_activation,
     run._read_prior_kata_activation, _PRIOR_KATA),
    ("concurrent_burst", run.carry_prior_concurrent_burst,
     run._read_prior_concurrent_burst, _PRIOR_CB),
    ("warm_pool_acquisition", run.carry_prior_warm_pool_acquisition,
     run._read_prior_warm_pool_acquisition, _PRIOR_WPA),
    ("at_scale_contention", run.carry_prior_at_scale_contention,
     run._read_prior_at_scale_contention, _PRIOR_ASC),
    ("cluster_saturation", run.carry_prior_cluster_saturation,
     run._read_prior_cluster_saturation, _PRIOR_CS),
    ("provisioning_rate_sweep", run.carry_prior_provisioning_rate_sweep,
     run._read_prior_provisioning_rate_sweep, _PRIOR_PRS),
)


# --- carry_prior_*: fresh-wins-else-prior, with the producer-less None path ---

def test_carry_no_fresh_carries_prior_unchanged():
    # The daily-refresh path: fresh=None (no producer) ⇒ carry the committed block verbatim,
    # keeping its ORIGINAL measured_at (not generated_at). This is the #112 core guarantee.
    for key, carry, _read, prior in _BLOCKS:
        out = carry(None, prior, generated_at=_GEN_AT)
        _check(out == prior, f"{key}: no fresh ⇒ carry prior verbatim, got {out!r}")
        _check(out["measured_at"] == prior["measured_at"],
               f"{key}: carried block keeps its original measured_at")


def test_carry_empty_fresh_carries_prior():
    for key, carry, _read, prior in _BLOCKS:
        out = carry({}, prior, generated_at=_GEN_AT)
        _check(out == prior, f"{key}: empty fresh {{}} ⇒ carry prior, got {out!r}")


def test_carry_no_fresh_no_prior_is_none():
    # First-ever run with neither a producer nor a prior committed block ⇒ absent (no key).
    for key, carry, _read, _prior in _BLOCKS:
        out = carry(None, None, generated_at=_GEN_AT)
        _check(out is None, f"{key}: no fresh + no prior ⇒ None (cell absent), got {out!r}")


def test_carry_fresh_wins_and_is_stamped():
    # Not the daily-refresh path today (no producer), but the carry contract is symmetric with
    # the other blocks: a hypothetical fresh dict wins and is stamped measured_at=generated_at.
    for key, carry, _read, prior in _BLOCKS:
        fresh = dict(prior)
        fresh.pop("measured_at", None)
        out = carry(fresh, prior, generated_at=_GEN_AT)
        _check(out["measured_at"] == _GEN_AT,
               f"{key}: fresh stamps measured_at=generated_at, got {out.get('measured_at')!r}")
        _check("measured_at" not in fresh, f"{key}: must not mutate the input fresh dict")


def test_carry_fresh_preexisting_measured_at_respected():
    for key, carry, _read, prior in _BLOCKS:
        fresh = dict(prior)
        fresh["measured_at"] = "2025-01-01T00:00:00Z"
        out = carry(fresh, prior, generated_at=_GEN_AT)
        _check(out["measured_at"] == "2025-01-01T00:00:00Z",
               f"{key}: a fresh dict already carrying measured_at keeps it (setdefault)")


# --- _read_prior_*: best-effort top-level read ---

def _read_with(reader, file_content):
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with _os.fdopen(fd, "w") as fh:
            if isinstance(file_content, str):
                fh.write(file_content)
            elif file_content is not None:
                json.dump(file_content, fh)
        if file_content is None:
            pathlib.Path(tmp).unlink(missing_ok=True)
        return reader(pathlib.Path(tmp))
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def test_read_prior_missing_file_is_none():
    for key, _carry, reader, _prior in _BLOCKS:
        _check(_read_with(reader, None) is None, f"{key}: missing file ⇒ None")


def test_read_prior_malformed_is_none():
    for key, _carry, reader, _prior in _BLOCKS:
        _check(_read_with(reader, "{not valid json") is None, f"{key}: malformed JSON ⇒ None")


def test_read_prior_absent_key_is_none():
    for key, _carry, reader, _prior in _BLOCKS:
        _check(_read_with(reader, {"scenarios": []}) is None, f"{key}: absent key ⇒ None")


def test_read_prior_present_returns_block():
    for key, _carry, reader, prior in _BLOCKS:
        out = _read_with(reader, {key: prior, "scenarios": []})
        _check(out == prior, f"{key}: present block returned, got {out!r}")


def test_read_prior_non_dict_block_is_none():
    # A present-but-non-dict value (a stringly-typed corruption) reads as None, not a crash.
    for key, _carry, reader, _prior in _BLOCKS:
        _check(_read_with(reader, {key: "corrupt", "scenarios": []}) is None,
               f"{key}: non-dict block value ⇒ None")


# --- build_results wiring: the daily-refresh write must EMIT all six carried blocks ---

def _prov():
    return {"cluster_substrate": "gke-sandbox", "commit": "abc1234", "runner": "test"}


def test_build_results_emits_all_six_carried_blocks():
    # The #112 regression lock: a wholesale build_results write with all six blocks passed
    # must EMIT all six top-level keys (before #112 they were silently dropped).
    out = rs.build_results(
        [], _prov(), _GEN_AT, product="sandbox",
        kata_activation=_PRIOR_KATA,
        concurrent_burst=_PRIOR_CB,
        warm_pool_acquisition=_PRIOR_WPA,
        at_scale_contention=_PRIOR_ASC,
        cluster_saturation=_PRIOR_CS,
        provisioning_rate_sweep=_PRIOR_PRS,
    )
    for key, _carry, _read, _prior in _BLOCKS:
        _check(key in out, f"{key} emitted")


def test_build_results_absent_omits_all_six_keys():
    # Default callers (a first-ever run with no priors) pass None ⇒ no keys, no fabrication.
    out = rs.build_results([], _prov(), _GEN_AT, product="sandbox")
    for key, _carry, _read, _prior in _BLOCKS:
        _check(key not in out, f"{key} omitted when not supplied")


def test_build_provenance_runtime_product_derived():
    # #3942/#830: build_provenance derives provenance.runtime product-side so
    # render's matrix flips that runtime's rows to measured. sandbox -> gvisor,
    # sandbox-kata -> kata-microvm, substrate -> omitted (no matrix runtime).
    saved = _os.environ.pop("BENCH_MATRIX_RUNTIME", None)
    try:
        _check(run.build_provenance("kind", "sandbox")["runtime"] == "gvisor",
               "sandbox derives gvisor")
        _check(run.build_provenance("gke-kata", "sandbox-kata")["runtime"] == "kata-microvm",
               "sandbox-kata derives kata-microvm")
        _check("runtime" not in run.build_provenance("kind", "substrate"),
               "substrate carries no matrix runtime")
        # env override wins over the product default (a fire that pins a
        # runtimeClassName off the product default).
        _os.environ["BENCH_MATRIX_RUNTIME"] = "kata-microvm"
        _check(run.build_provenance("kind", "sandbox")["runtime"] == "kata-microvm",
               "BENCH_MATRIX_RUNTIME override wins")
    finally:
        _os.environ.pop("BENCH_MATRIX_RUNTIME", None)
        if saved is not None:
            _os.environ["BENCH_MATRIX_RUNTIME"] = saved


def test_build_provenance_runtime_round_trips_through_emitter():
    # The derived runtime must survive _coerce_provenance (it is in PROVENANCE_FIELDS
    # + passes the closed enum guard), so the emitted results carry it end-to-end.
    saved = _os.environ.pop("BENCH_MATRIX_RUNTIME", None)
    try:
        prov = run.build_provenance("gke-kata", "sandbox-kata")
        r = rs.build_results([], prov, _GEN_AT, product="sandbox-kata")
        _check(r["provenance"]["runtime"] == "kata-microvm",
               "kata runtime survives the emitter coercion")
    finally:
        if saved is not None:
            _os.environ["BENCH_MATRIX_RUNTIME"] = saved


def test_build_provenance_machine_type_and_prior_stamp():
    # PR#313 review: BENCH_MACHINE_TYPE stamps machine_type; prior_machine_type
    # is stamped ONLY when the caller-supplied prior differs from the current run's value
    # (absent env -> no machine_type at all -> never a spurious prior comparison either).
    saved = _os.environ.pop("BENCH_MACHINE_TYPE", None)
    try:
        _check("machine_type" not in run.build_provenance("kind", "sandbox"),
               "no BENCH_MACHINE_TYPE -> machine_type omitted")
        _os.environ["BENCH_MACHINE_TYPE"] = "e2-standard-16"
        same_rig = run.build_provenance("kind", "sandbox", prior_machine_type="e2-standard-16")
        _check(same_rig.get("machine_type") == "e2-standard-16",
               "machine_type stamped from env")
        _check("prior_machine_type" not in same_rig,
               "same-rig prior == current -> no prior_machine_type stamped")
        changed_rig = run.build_provenance("kind", "sandbox", prior_machine_type="n2-standard-16")
        _check(changed_rig.get("prior_machine_type") == "n2-standard-16",
               "differing prior stamped as prior_machine_type")
        no_prior = run.build_provenance("kind", "sandbox")
        _check("prior_machine_type" not in no_prior,
               "no prior supplied -> no prior_machine_type stamped")
    finally:
        _os.environ.pop("BENCH_MACHINE_TYPE", None)
        if saved is not None:
            _os.environ["BENCH_MACHINE_TYPE"] = saved


def test_build_provenance_machine_type_round_trips_through_emitter():
    # Both machine_type and prior_machine_type must survive _coerce_provenance (they are in
    # PROVENANCE_FIELDS and pass the generic non-empty-string branch), so a machine-class
    # change reaches the renderer's closed-schema-validated caveat, not just latest.json.
    saved = _os.environ.pop("BENCH_MACHINE_TYPE", None)
    try:
        _os.environ["BENCH_MACHINE_TYPE"] = "e2-standard-16"
        prov = run.build_provenance("gke-sandbox", "sandbox", prior_machine_type="n2-standard-16")
        r = rs.build_results([], prov, _GEN_AT, product="sandbox")
        _check(r["provenance"]["machine_type"] == "e2-standard-16",
               "machine_type survives the emitter coercion")
        _check(r["provenance"]["prior_machine_type"] == "n2-standard-16",
               "prior_machine_type survives the emitter coercion")
    finally:
        _os.environ.pop("BENCH_MACHINE_TYPE", None)
        if saved is not None:
            _os.environ["BENCH_MACHINE_TYPE"] = saved


def test_build_provenance_node_image_and_runsc_version():
    # hb#317 (mirrors machine_type's hb#313 pattern): BENCH_NODE_IMAGE /
    # BENCH_RUNSC_VERSION stamp node_image/runsc_version — sandbox-family only
    # (same `if runtime:` gate as sandbox_cpu_request_m), absent env -> omitted.
    saved_img = _os.environ.pop("BENCH_NODE_IMAGE", None)
    saved_runsc = _os.environ.pop("BENCH_RUNSC_VERSION", None)
    try:
        no_env = run.build_provenance("kind", "sandbox")
        _check("node_image" not in no_env, "no BENCH_NODE_IMAGE -> node_image omitted")
        _check("runsc_version" not in no_env,
               "no BENCH_RUNSC_VERSION -> runsc_version omitted")

        _os.environ["BENCH_NODE_IMAGE"] = "v1.31.1-gke.1846000"
        _os.environ["BENCH_RUNSC_VERSION"] = "release-20260715.0"
        prov = run.build_provenance("gke-sandbox", "sandbox")
        _check(prov.get("node_image") == "v1.31.1-gke.1846000",
               "node_image stamped from env")
        _check(prov.get("runsc_version") == "release-20260715.0",
               "runsc_version stamped from env")

        # substrate (non-sandbox-family product) carries no matrix runtime, so
        # neither field is stamped even with the env vars present.
        substrate_prov = run.build_provenance("kind", "substrate")
        _check("node_image" not in substrate_prov,
               "non-sandbox-family product omits node_image")
        _check("runsc_version" not in substrate_prov,
               "non-sandbox-family product omits runsc_version")
    finally:
        _os.environ.pop("BENCH_NODE_IMAGE", None)
        _os.environ.pop("BENCH_RUNSC_VERSION", None)
        if saved_img is not None:
            _os.environ["BENCH_NODE_IMAGE"] = saved_img
        if saved_runsc is not None:
            _os.environ["BENCH_RUNSC_VERSION"] = saved_runsc


def test_build_provenance_node_image_and_runsc_version_round_trip_through_emitter():
    # Both fields must survive _coerce_provenance (generic non-empty-string branch
    # in PROVENANCE_FIELDS), so they reach latest.json for a future regression
    # investigation to read back historically (no caveat-rendering needed, hb#317
    # non-goal).
    saved_img = _os.environ.pop("BENCH_NODE_IMAGE", None)
    saved_runsc = _os.environ.pop("BENCH_RUNSC_VERSION", None)
    try:
        _os.environ["BENCH_NODE_IMAGE"] = "v1.31.1-gke.1846000"
        _os.environ["BENCH_RUNSC_VERSION"] = "release-20260715.0"
        prov = run.build_provenance("gke-sandbox", "sandbox")
        r = rs.build_results([], prov, _GEN_AT, product="sandbox")
        _check(r["provenance"]["node_image"] == "v1.31.1-gke.1846000",
               "node_image survives the emitter coercion")
        _check(r["provenance"]["runsc_version"] == "release-20260715.0",
               "runsc_version survives the emitter coercion")
    finally:
        _os.environ.pop("BENCH_NODE_IMAGE", None)
        _os.environ.pop("BENCH_RUNSC_VERSION", None)
        if saved_img is not None:
            _os.environ["BENCH_NODE_IMAGE"] = saved_img
        if saved_runsc is not None:
            _os.environ["BENCH_RUNSC_VERSION"] = saved_runsc


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for t in _all_tests():
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
