"""Offline tests for the closed-schema emitter — no cluster, no I/O.

Run with bare python3 (no pytest dependency, so the auto-refresh GH-runner needs
nothing extra):  python3 -m harness.test_results_schema
or directly:      python3 bench-repo/harness/test_results_schema.py

Each test asserts a public-safety property of build_results. The leak-suspenders
tests (excerpt-never-emitted, non-numeric-sla-dropped, unsafe-key-dropped,
non-schema-key-dropped) are the load-bearing ones: they prove an internal string
cannot reach the public results.json even if a scenario tries to surface it.
"""

from __future__ import annotations

# Make this file runnable BOTH as `python3 harness/test_x.py` and
# `python3 -m harness.test_x` by putting the repo root on sys.path before
# the absolute `from harness import ...` below (mirrors test_warm_vs_cold.py).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from harness import results_schema as rs

GEN_AT = "2026-06-28T00:00:00Z"


def _prov(**over):
    base = {
        "cluster_substrate": "kind",
        "controller_image": "registry.k8s.io/agent-sandbox/controller:latest-main",
        "controller_digest": "sha256:abc",
        "crd_version": "v1beta1",
        "suite_git_sha": "deadbeef",
        "run_id": "ignored-overwritten-by-runner",
        "node_count": 1,
    }
    base.update(over)
    return base


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_happy_path_minimal():
    r = rs.build_results(
        [{"name": "warmpool_cold_start", "outcome": "PASS",
          "n": 30, "sla_metrics": {"activation_ms": 1420.0}}],
        _prov(), GEN_AT,
    )
    _check(r["product"] == "sandbox", "product")
    _check(r["generated_at"] == GEN_AT, "generated_at")
    s = r["scenarios"][0]
    _check(s["name"] == "warmpool_cold_start", "name is render-canonical basename")
    _check(s["outcome"] == "PASS", "outcome canonical uppercase")
    _check(s["sla_metrics"] == {"activation_ms": 1420.0}, "underscore sla key kept")
    _check(s["n"] == 30, "n kept")


def test_outcome_case_canonicalized():
    # The scenario bodies return mixed case; the emitter canonicalizes to render's
    # exact forms (PASS/FAIL uppercase, pending lowercase) so every row renders.
    r = rs.build_results(
        [{"name": "a", "outcome": "pass"},
         {"name": "b", "outcome": "FAIL"},
         {"name": "c", "outcome": "Pending",
          "pending_reason": "requires-gvisor-runtime"}],
        _prov(), GEN_AT,
    )
    _check(r["scenarios"][0]["outcome"] == "PASS", "pass -> PASS")
    _check(r["scenarios"][1]["outcome"] == "FAIL", "FAIL stays FAIL")
    _check(r["scenarios"][2]["outcome"] == "pending", "Pending -> pending")


def test_excerpt_never_emitted():
    # A scenario tries to smuggle a raw failure excerpt as a top-level key AND
    # inside sla_metrics. Neither may appear in the output. The drop logic is
    # shape-agnostic (any non-numeric value goes), so a neutral sentinel proves
    # it without embedding a real leak shape that scanners would flag.
    sentinel = "LEAK-SENTINEL-MUST-NOT-APPEAR"
    r = rs.build_results(
        [{"name": "x", "outcome": "fail",
          "excerpt": sentinel,
          "failure_excerpt": sentinel,
          "sla_metrics": {"warmpool-cold-start": 1.0, "leak": sentinel}}],
        _prov(), GEN_AT,
    )
    s = r["scenarios"][0]
    _check("excerpt" not in s, "excerpt dropped")
    _check("failure_excerpt" not in s, "failure_excerpt dropped")
    _check(s["sla_metrics"] == {"warmpool-cold-start": 1.0},
           f"non-numeric sla value dropped, got {s.get('sla_metrics')}")
    # Belt: the sentinel appears nowhere in the emitted scenario.
    _check(sentinel not in repr(s), "no leaked string anywhere in emitted scenario")


def test_non_schema_keys_dropped():
    r = rs.build_results(
        [{"name": "x", "outcome": "pass", "internal_pod_ip": "10.4.2.7",
          "obs_observation_id": "01J...", "n": 5}],
        _prov(), GEN_AT,
    )
    s = r["scenarios"][0]
    _check(set(s.keys()) <= set(rs.SCENARIO_FIELDS), f"only schema keys, got {set(s.keys())}")
    _check("internal_pod_ip" not in s, "pod ip dropped")
    _check("obs_observation_id" not in s, "obs id dropped")


def test_provenance_allow_list():
    r = rs.build_results(
        [], _prov(kubeconfig_path="LEAK-SENTINEL-PATH", dsn="LEAK-SENTINEL-DSN"), GEN_AT,
    )
    p = r["provenance"]
    _check(set(p.keys()) <= set(rs.PROVENANCE_FIELDS), f"prov allow-list, got {set(p.keys())}")
    _check("kubeconfig_path" not in p and "dsn" not in p, "internal prov keys dropped")


def test_cluster_substrate_mandatory_and_closed():
    try:
        rs.build_results([], _prov(cluster_substrate=None), GEN_AT)
    except ValueError:
        pass
    else:
        raise AssertionError("missing cluster_substrate must raise")
    try:
        rs.build_results([], _prov(cluster_substrate="minikube"), GEN_AT)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown cluster_substrate must raise")


def test_pending_reason_enum_only():
    # pending requires an enum reason
    r = rs.build_results(
        [{"name": "g", "outcome": "pending", "pending_reason": "requires-gvisor-runtime"}],
        _prov(), GEN_AT,
    )
    _check(r["scenarios"][0]["pending_reason"] == "requires-gvisor-runtime", "enum reason kept")
    # free-text pending_reason rejected (could carry a leak)
    try:
        rs.build_results(
            [{"name": "g", "outcome": "pending",
              "pending_reason": "node 10.4.2.7 had no runsc"}],
            _prov(), GEN_AT,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("free-text pending_reason must raise")


def test_pending_reason_dropped_when_not_pending():
    r = rs.build_results(
        [{"name": "x", "outcome": "pass", "pending_reason": "requires-gvisor-runtime"}],
        _prov(), GEN_AT,
    )
    _check("pending_reason" not in r["scenarios"][0], "pending_reason only on pending cells")


def test_unknown_outcome_raises():
    try:
        rs.build_results([{"name": "x", "outcome": "FLAKY"}], _prov(), GEN_AT)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown outcome must raise (fail-closed)")


def test_sla_unsafe_keys_and_values_dropped():
    r = rs.build_results(
        [{"name": "x", "outcome": "pass", "sla_metrics": {
            "activation_ms": 1420.0,            # keep (underscore, render-canonical)
            "warmpool-cold-start": 1.0,         # keep (hyphen form still passes)
            "Bad Key": 1.0,                     # space -> drop
            "host:port": 1.0,                   # colon -> drop
            "a/b": 1.0,                         # slash -> drop
            "x.y": 1.0,                         # dot -> drop
            "flag": True,                       # bool -> drop
            "nan": float("nan"),                # NaN -> drop
            "inf": float("inf"),                # inf -> drop
            "str": "1.0",                       # string -> drop
        }}],
        _prov(), GEN_AT,
    )
    sla = r["scenarios"][0]["sla_metrics"]
    _check(sla == {"activation_ms": 1420.0, "warmpool-cold-start": 1.0},
           f"only safe numeric sla kept (underscore + hyphen), got {sla}")


def test_cold_start_mode_enum_only():
    # #3885: cold_start_mode is a CLOSED enum in provenance. A valid value is
    # kept; an out-of-set value fails closed (a mislabeled cold-start must never
    # publish); absent is simply dropped (optional field).
    for mode in rs.COLD_START_MODE_ENUM:
        r = rs.build_results([], _prov(cold_start_mode=mode), GEN_AT)
        _check(r["provenance"]["cold_start_mode"] == mode, f"{mode} kept")
    try:
        rs.build_results([], _prov(cold_start_mode="warm-cached-lie"), GEN_AT)
    except ValueError:
        pass
    else:
        raise AssertionError("non-enum cold_start_mode must raise (fail-closed)")
    # absent -> dropped, not an error (the base _prov() carries no mode)
    r = rs.build_results([], _prov(), GEN_AT)
    _check("cold_start_mode" not in r["provenance"], "absent mode dropped")


def test_product_enum_only():
    # #3868: product is a CLOSED enum. The default is sandbox (so existing callers
    # are unchanged); substrate is accepted; a non-enum product fails closed (a
    # misconfiguration, not a leak — must never publish under a bogus label).
    r = rs.build_results([], _prov(), GEN_AT, product="substrate")
    _check(r["product"] == "substrate", "substrate product kept")
    r = rs.build_results([], _prov(), GEN_AT)  # defaulted
    _check(r["product"] == "sandbox", "default product is sandbox")
    try:
        rs.build_results([], _prov(), GEN_AT, product="bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("non-enum product must raise (fail-closed)")


def test_badge_scope_enum_only():
    # #3905: badge_scope is a per-SCENARIO CLOSED enum. A valid value is kept; an
    # out-of-set value fails closed (a mislabeled isolation badge must never publish);
    # absent is simply dropped (optional, so non-isolation cells stay clean).
    for scope in rs.BADGE_SCOPE_ENUM:
        r = rs.build_results(
            [{"name": "cross_tenant_network_isolation", "outcome": "pass",
              "badge_scope": scope}],
            _prov(), GEN_AT,
        )
        _check(r["scenarios"][0]["badge_scope"] == scope, f"{scope} kept")
    try:
        rs.build_results(
            [{"name": "default_deny_egress", "outcome": "pass",
              "badge_scope": "fully-bulletproof-trust-me"}],
            _prov(), GEN_AT,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-enum badge_scope must raise (fail-closed)")
    # absent -> dropped, not an error
    r = rs.build_results([{"name": "a", "outcome": "pass"}], _prov(), GEN_AT)
    _check("badge_scope" not in r["scenarios"][0], "absent badge_scope dropped")


def test_n_coercion():
    r = rs.build_results(
        [{"name": "a", "outcome": "pass", "n": 30.0},
         {"name": "b", "outcome": "pass", "n": True},
         {"name": "c", "outcome": "pass", "n": "30"}],
        _prov(), GEN_AT,
    )
    _check(r["scenarios"][0]["n"] == 30, "float-int n coerced")
    _check("n" not in r["scenarios"][1], "bool n dropped")
    _check("n" not in r["scenarios"][2], "string n dropped")


def test_scale_proof_passthrough_valid():
    # A well-formed top-level scale_proof object survives _coerce_scale_proof and is
    # emitted at the top level (the Scale Proof / Linearity Check table source).
    sp = {
        "scale_points": [
            {"node_count": 1, "density": 1.88},
            {"node_count": 2, "density": 1.88},
            {"node_count": 4, "density": 1.88},
        ],
        "density_retention": 1.0,
        "thpt_retention": 1.0,
    }
    r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
    out = r["scale_proof"]
    _check(out["scale_points"] == sp["scale_points"], "scale_points kept verbatim")
    _check(out["density_retention"] == 1.0, "density_retention kept")
    _check(out["thpt_retention"] == 1.0, "thpt_retention kept")


def test_scale_proof_absent_emits_no_key():
    # Default callers pass no scale_proof — the top-level key must be omitted, not
    # emitted empty (the table renders nothing rather than a partial lie).
    r = rs.build_results([], _prov(), GEN_AT)
    _check("scale_proof" not in r, "no scale_proof key when none supplied")


def test_scale_proof_malformed_points_dropped():
    # A points list with any bad point fails closed -> the whole key is omitted
    # (no partial series). node_count out of (0,10000), bool, or non-int all fail.
    for bad_points in (
        [],                                                  # empty
        [{"node_count": 0, "density": 1.0}],                 # nc not > 0
        [{"node_count": True, "density": 1.0}],              # bool nc
        [{"node_count": 1, "density": -1.0}],                # negative density
        [{"node_count": 1, "density": float("inf")}],        # inf density
        [{"node_count": 1, "density": "1.0"}],               # non-numeric density
        "not-a-list",
    ):
        r = rs.build_results([], _prov(), GEN_AT, scale_proof={"scale_points": bad_points})
        _check("scale_proof" not in r, f"malformed points omits key: {bad_points!r}")


def test_scale_proof_thpt_retention_optional():
    # thpt_retention dropped when absent (render shows throughput pending — honest);
    # the rest of the object still emits. density_retention likewise optional.
    sp = {"scale_points": [{"node_count": 1, "density": 4.0},
                           {"node_count": 4, "density": 2.0}],
          "density_retention": 0.5}
    r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
    out = r["scale_proof"]
    _check("thpt_retention" not in out, "absent thpt_retention dropped, no fabrication")
    _check(out["density_retention"] == 0.5, "density_retention kept")
    _check(len(out["scale_points"]) == 2, "scale_points kept")


def test_scale_proof_unsafe_retention_dropped():
    # A non-finite or negative retention is dropped (not emitted as a lie); the
    # points still survive so the density column can render.
    sp = {"scale_points": [{"node_count": 1, "density": 4.0},
                           {"node_count": 2, "density": 4.0}],
          "density_retention": float("nan"),
          "thpt_retention": -0.5}
    r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
    out = r["scale_proof"]
    _check("density_retention" not in out, "NaN retention dropped")
    _check("thpt_retention" not in out, "negative retention dropped")
    _check(len(out["scale_points"]) == 2, "scale_points still emitted")


def test_scale_proof_per_point_throughput_survives_ingestion():
    # The producer (scale_slope._classify_scale_slope) emits per-point throughput so
    # render's per-step throughput convergence subline can show. _coerce_scale_proof
    # MUST carry it through — dropping it silently blanks that subline on every real
    # fire (the canonical {1,2,4} sweep is exactly the >=3-point case render activates
    # the subline on). Mirrors render/schema.py _scale_points_ok's optional throughput.
    sp = {
        "scale_points": [
            {"node_count": 1, "density": 1.88, "throughput": 10.0},
            {"node_count": 2, "density": 1.88, "throughput": 9.8},
            {"node_count": 4, "density": 1.88, "throughput": 9.5},
        ],
        "density_retention": 1.0,
        "thpt_retention": 0.95,
    }
    r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
    out = r["scale_proof"]
    _check([p.get("throughput") for p in out["scale_points"]] == [10.0, 9.8, 9.5],
           "per-point throughput carried through ingestion in order")


def test_scale_proof_invalid_per_point_throughput_fails_closed():
    # An invalid per-point throughput (bool / NaN / inf / negative / non-numeric) fails
    # the whole block closed — never a partial point — matching the density discipline.
    for bad in (True, float("nan"), float("inf"), -1.0, "9.5"):
        sp = {"scale_points": [{"node_count": 1, "density": 1.88, "throughput": bad},
                               {"node_count": 2, "density": 1.88, "throughput": 9.0}]}
        r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
        _check("scale_proof" not in r, f"bad throughput omits key: {bad!r}")


def test_scale_proof_throughput_absent_still_emits():
    # throughput is OPTIONAL: a points list without it still emits (density column
    # renders; the per-step throughput subline is honestly absent, not a fabrication).
    sp = {"scale_points": [{"node_count": 1, "density": 1.88},
                           {"node_count": 2, "density": 1.88}]}
    r = rs.build_results([], _prov(), GEN_AT, scale_proof=sp)
    out = r["scale_proof"]
    _check(all("throughput" not in p for p in out["scale_points"]),
           "absent throughput stays absent, no fabrication")
    _check(len(out["scale_points"]) == 2, "scale_points emitted")


def test_scale_proof_measured_at_passthrough_and_dropped():
    # measured_at (#3952): a non-empty string survives; anything else is dropped
    # (the carried-block date renders only when honestly present).
    base = {"scale_points": [{"node_count": 1, "density": 4.0},
                             {"node_count": 2, "density": 4.0}]}
    r = rs.build_results([], _prov(), GEN_AT,
                         scale_proof={**base, "measured_at": "2026-06-29T03:46:01Z"})
    _check(r["scale_proof"]["measured_at"] == "2026-06-29T03:46:01Z",
           "non-empty measured_at kept")
    for bad in ("", 1, True, None, ["x"]):
        r = rs.build_results([], _prov(), GEN_AT,
                             scale_proof={**base, "measured_at": bad})
        _check("measured_at" not in r["scale_proof"],
               f"bad measured_at dropped: {bad!r}")


def test_stepup_passthrough_valid():
    # A well-formed top-level stepup object survives _coerce_stepup and is emitted
    # at the top level (the Step-Up Pareto table source, a#3960 item 4).
    su = {
        "pareto_points": [
            {"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0, "ttfe_p50_ms": 120.0,
             "ttfe_p99_ms": 480.0, "ready_per_s": 9.6, "cost_usd_per_1k_ready": 0.42},
            {"offered_rate_per_s": 100, "ttfe_p95_ms": 2400.0},
        ],
        "verdict": "saturated",
        "north_star_breach_rate": 30,
        "saturation_rate": 100,
        "max_flat_rate": 10,
        "sld_s": 20.0,
        "wpr": 0.75,
        "node_count": 510,
        "machine_type": "e2-standard-16",
        "measured_at": "2026-06-29T07:40:00Z",
    }
    r = rs.build_results([], _prov(), GEN_AT, stepup=su)
    out = r["stepup"]
    _check(out["pareto_points"][0]["ttfe_p99_ms"] == 480.0, "optional percentile carried")
    _check(out["pareto_points"][1] == {"offered_rate_per_s": 100, "ttfe_p95_ms": 2400.0},
           "minimal point keeps only required spine")
    _check(out["verdict"] == "saturated", "verdict kept")
    _check(out["north_star_breach_rate"] == 30 and out["saturation_rate"] == 100,
           "characteristic rates kept")
    _check(out["sld_s"] == 20.0 and out["wpr"] == 0.75, "Little's-law params kept")
    _check(out["node_count"] == 510 and out["machine_type"] == "e2-standard-16",
           "public GCP shape kept")
    _check(out["measured_at"] == "2026-06-29T07:40:00Z", "measured_at kept")


def test_stepup_absent_emits_no_key():
    # Default callers pass no stepup — the top-level key must be omitted, not emitted
    # empty (the table renders nothing rather than a partial lie).
    r = rs.build_results([], _prov(), GEN_AT)
    _check("stepup" not in r, "no stepup key when none supplied")


def test_stepup_malformed_points_dropped():
    # A points list with any bad point fails closed -> the whole key omitted. Also a
    # missing/empty points list or a missing verdict fails closed.
    for bad in (
        {"pareto_points": [], "verdict": "saturated"},                         # empty points
        {"pareto_points": "x", "verdict": "saturated"},                        # non-list
        {"pareto_points": [{"offered_rate_per_s": 0, "ttfe_p95_ms": 1.0}],     # rate not > 0
         "verdict": "flat-through-sweep"},
        {"pareto_points": [{"offered_rate_per_s": True, "ttfe_p95_ms": 1.0}],  # bool rate
         "verdict": "flat-through-sweep"},
        {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": -1.0}],   # neg p95
         "verdict": "flat-through-sweep"},
        {"pareto_points": [{"offered_rate_per_s": 10}],                        # no p95 spine
         "verdict": "flat-through-sweep"},
        {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 1.0}]},   # no verdict
        {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 1.0}],    # unknown verdict
         "verdict": "exploded"},
    ):
        r = rs.build_results([], _prov(), GEN_AT, stepup=bad)
        _check("stepup" not in r, f"malformed stepup omits key: {bad!r}")


def test_stepup_optional_point_fields_dropped_on_bad_value():
    # An optional per-point field with a non-finite/negative value is dropped, but the
    # point (and the object) still emits on its required spine — honest-partial, never
    # a fabricated 0.
    su = {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0,
                             "ttfe_p50_ms": float("nan"), "ready_per_s": -1.0,
                             "cost_usd_per_1k_ready": float("inf")}],
          "verdict": "flat-through-sweep"}
    r = rs.build_results([], _prov(), GEN_AT, stepup=su)
    pt = r["stepup"]["pareto_points"][0]
    _check(pt == {"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0},
           "bad optional point fields dropped, spine intact")


def test_stepup_unsafe_sweep_scalars_dropped():
    # Sweep-level scalars are independently validated: a bad characteristic rate / wpr /
    # node_count / machine_type is dropped while the rest of the object emits.
    base = {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0}],
            "verdict": "flat-through-sweep"}
    r = rs.build_results([], _prov(), GEN_AT, stepup={
        **base,
        "north_star_breach_rate": 0,          # not > 0
        "saturation_rate": True,              # bool
        "max_flat_rate": "10",               # non-int
        "wpr": 1.5,                           # out of (0,1)
        "node_count": 0,                      # not > 0
        "machine_type": "sandbox-scenarios-cluster",  # not a GCP machine shape
    })
    out = r["stepup"]
    for k in ("north_star_breach_rate", "saturation_rate", "max_flat_rate",
              "wpr", "node_count", "machine_type"):
        _check(k not in out, f"unsafe sweep scalar dropped: {k}")
    _check(out["verdict"] == "flat-through-sweep", "object still emits on its spine")


def test_stepup_machine_type_internal_name_rejected():
    # PUBLIC hygiene: only a bounded GCP machine shape passes machine_type; an internal
    # cluster/namespace/project string is rejected (dropped), never carried to render.
    base = {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0}],
            "verdict": "flat-through-sweep"}
    _check(rs.build_results([], _prov(), GEN_AT,
                            stepup={**base, "machine_type": "e2-standard-16"}
                            )["stepup"]["machine_type"] == "e2-standard-16",
           "valid GCP shape kept")
    for leak in ("alexbu-gke-dev-d", "sandbox-scenarios", "postgres-obs-0", "E2-STANDARD-16"):
        r = rs.build_results([], _prov(), GEN_AT, stepup={**base, "machine_type": leak})
        _check("machine_type" not in r["stepup"], f"internal-ish machine_type dropped: {leak!r}")
        _check(leak not in repr(r["stepup"]), f"no leaked string in emitted stepup: {leak!r}")


def test_stepup_measured_at_passthrough_and_dropped():
    # measured_at: a non-empty string survives; anything else is dropped (mirrors scale_proof).
    base = {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0}],
            "verdict": "flat-through-sweep"}
    r = rs.build_results([], _prov(), GEN_AT,
                         stepup={**base, "measured_at": "2026-06-29T07:40:00Z"})
    _check(r["stepup"]["measured_at"] == "2026-06-29T07:40:00Z", "non-empty measured_at kept")
    for bad in ("", 1, True, None, ["x"]):
        r = rs.build_results([], _prov(), GEN_AT, stepup={**base, "measured_at": bad})
        _check("measured_at" not in r["stepup"], f"bad measured_at dropped: {bad!r}")


def test_stepup_controller_startup_proxy_passthrough():
    # The #3975 LOWER-BOUND proxy: a valid controller_startup block survives alongside a populated
    # true-TTFE pareto, carrying lower_bound + the proxy percentiles + an optional proxy verdict.
    su = {
        "pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0}],
        "verdict": "saturated",
        "controller_startup": {
            "lower_bound": True,
            "pareto_points": [
                {"offered_rate_per_s": 10, "controller_startup_p95_ms": 180.0,
                 "controller_startup_p50_ms": 90.0, "controller_startup_p99_ms": 360.0,
                 "controller_ready_per_s": 9.8},
                {"offered_rate_per_s": 100, "controller_startup_p95_ms": 1900.0},
            ],
            "verdict": "saturated",
        },
    }
    cs = rs.build_results([], _prov(), GEN_AT, stepup=su)["stepup"]["controller_startup"]
    _check(cs["lower_bound"] is True, "lower_bound True carried")
    _check(len(cs["pareto_points"]) == 2, "both proxy points carried")
    _check(cs["pareto_points"][0]["controller_startup_p99_ms"] == 360.0, "optional p99 carried")
    _check(cs["pareto_points"][0]["controller_ready_per_s"] == 9.8, "optional ready_per_s carried")
    _check(cs["pareto_points"][1] == {"offered_rate_per_s": 100, "controller_startup_p95_ms": 1900.0},
           "spine-only proxy point keeps just the required keys")
    _check(cs["verdict"] == "saturated", "optional proxy verdict carried")


def test_stepup_3975_gap_empty_ttfe_with_proxy_emits():
    # The #3975 gap shape: empty true-TTFE pareto + a valid proxy -> stepup emitted, pareto_points
    # OMITTED (never []), proxy carries the only table, verdict honestly no-measured-steps.
    su = {
        "pareto_points": [],
        "verdict": "no-measured-steps",
        "controller_startup": {
            "lower_bound": True,
            "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 180.0}],
        },
    }
    out = rs.build_results([], _prov(), GEN_AT, stepup=su)["stepup"]
    _check("pareto_points" not in out, "empty true-TTFE pareto omitted, not emitted as []")
    _check(out["verdict"] == "no-measured-steps", "honest no-measured-steps verdict")
    _check(out["controller_startup"]["lower_bound"] is True, "proxy block carried")


def test_stepup_controller_startup_caveat_never_carried():
    # PUBLIC-safety: the internal producer's free-text caveat is render-owned and must NEVER ride
    # the public schema even if it reaches the coercer — only lower_bound + measured numbers survive.
    leak = "INTERNAL-cluster-name caveat with project-id"
    su = {
        "pareto_points": [],
        "verdict": "no-measured-steps",
        "controller_startup": {
            "lower_bound": True,
            "caveat": leak,
            "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 180.0}],
        },
    }
    out = rs.build_results([], _prov(), GEN_AT, stepup=su)["stepup"]
    _check("caveat" not in out["controller_startup"], "free-text caveat dropped from proxy block")
    _check(leak not in repr(out), "no leaked caveat string anywhere in emitted stepup")


def test_stepup_controller_startup_malformed_dropped():
    # A malformed proxy block is dropped; combined with an empty true-TTFE pareto that drops the
    # whole stepup key (no valid table at all -> honest nothing).
    bad_blocks = [
        {"lower_bound": False, "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 1.0}]},
        {"pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 1.0}]},  # no lower_bound
        {"lower_bound": True, "pareto_points": []},                                          # empty proxy points
        {"lower_bound": True, "pareto_points": [{"offered_rate_per_s": 10}]},                # no p95 spine
        {"lower_bound": True, "pareto_points": [{"offered_rate_per_s": 0, "controller_startup_p95_ms": 1.0}]},
        {"lower_bound": True, "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": -1.0}]},
        {"lower_bound": 1, "pareto_points": [{"offered_rate_per_s": 10, "controller_startup_p95_ms": 1.0}]},  # truthy-not-True
    ]
    for blk in bad_blocks:
        su = {"pareto_points": [], "verdict": "no-measured-steps", "controller_startup": blk}
        r = rs.build_results([], _prov(), GEN_AT, stepup=su)
        _check("stepup" not in r, f"malformed proxy + empty TTFE drops stepup: {blk!r}")
    # But a malformed proxy alongside a VALID true-TTFE pareto keeps the true-TTFE table and just
    # omits the proxy (the true table is honest on its own).
    su = {"pareto_points": [{"offered_rate_per_s": 10, "ttfe_p95_ms": 240.0}],
          "verdict": "saturated",
          "controller_startup": {"lower_bound": True, "pareto_points": []}}
    out = rs.build_results([], _prov(), GEN_AT, stepup=su)["stepup"]
    _check("controller_startup" not in out, "malformed proxy omitted, true-TTFE table kept")
    _check(len(out["pareto_points"]) == 1, "valid true-TTFE pareto survives a bad proxy sibling")


# --- #3954 sibling: warm_vs_cold ingestion coercer --------------------------------------


def _wvc(**over):
    base = {
        "warm_p50_ms": 420.0,
        "cold_ms": 4200.0,
        "speedup": 10.0,
        "semantic": "ttfe",
        "runtime_class": "gvisor",
        "n_warm": 200,
    }
    base.update(over)
    return base


def test_warm_vs_cold_passthrough_valid():
    # A well-formed inner object (the classify_warm_vs_cold shape) survives intact and is
    # emitted at the top level (the warm-vs-cold headline source). Makes warm_vs_cold.py:38's
    # build_results(warm_vs_cold=...) contract real.
    r = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc())
    out = r["warm_vs_cold"]
    _check(out["warm_p50_ms"] == 420.0, "warm_p50_ms kept")
    _check(out["cold_ms"] == 4200.0, "cold_ms kept")
    _check(out["speedup"] == 10.0, "speedup kept")
    _check(out["semantic"] == "ttfe", "semantic kept")
    _check(out["runtime_class"] == "gvisor", "runtime_class kept")
    _check(out["n_warm"] == 200, "n_warm kept")


def test_warm_vs_cold_absent_emits_no_key():
    # Default callers pass no warm_vs_cold — the top-level key must be omitted, not emitted
    # empty (the block renders nothing rather than a partial lie).
    r = rs.build_results([], _prov(), GEN_AT)
    _check("warm_vs_cold" not in r, "no warm_vs_cold key when none supplied")


def test_warm_vs_cold_n_warm_optional():
    # n_warm absent -> dropped (render the bare headline); the five spine fields still emit.
    wvc = _wvc()
    del wvc["n_warm"]
    out = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=wvc)["warm_vs_cold"]
    _check("n_warm" not in out, "absent n_warm dropped, no fabrication")
    _check(out["speedup"] == 10.0, "spine kept without n_warm")
    # A bad n_warm value is dropped too, not propagated.
    for bad in (-1, True, 3.5, "200"):
        out2 = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(n_warm=bad))["warm_vs_cold"]
        _check("n_warm" not in out2, f"bad n_warm dropped: {bad!r}")


def test_warm_vs_cold_required_field_missing_omits_key():
    # Any missing required spine field fails closed -> the whole key is omitted.
    for drop in ("warm_p50_ms", "cold_ms", "speedup", "semantic", "runtime_class"):
        wvc = _wvc()
        del wvc[drop]
        r = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=wvc)
        _check("warm_vs_cold" not in r, f"missing {drop} omits key")
    # not-a-dict also omits.
    _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold="nope"),
           "non-dict warm_vs_cold omits key")


def test_warm_vs_cold_nonpositive_legs_dropped():
    # warm_p50_ms / cold_ms must be strictly > 0 (a 0 leg is a degenerate ratio — mirrors
    # render's _clean_warm_vs_cold positivity gate). 0, negative, inf, NaN, bool, non-numeric fail.
    for bad in (0, 0.0, -1.0, float("inf"), float("nan"), True, "420"):
        _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(warm_p50_ms=bad)),
               f"bad warm_p50_ms drops block: {bad!r}")
        _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(cold_ms=bad)),
               f"bad cold_ms drops block: {bad!r}")


def test_warm_vs_cold_speedup_nonneg_required():
    # speedup is non-negative (0 allowed — degenerate but not a leak); negative/inf/NaN/bool fail.
    for bad in (-0.1, float("inf"), float("nan"), True, "10"):
        _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(speedup=bad)),
               f"bad speedup drops block: {bad!r}")
    # 0.0 is accepted (nonneg).
    out = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(speedup=0.0))["warm_vs_cold"]
    _check(out["speedup"] == 0.0, "speedup 0.0 accepted (nonneg)")


def test_warm_vs_cold_semantic_enum_only():
    # semantic must be one of the two measured modes; anything else drops the block.
    for bad in ("startup", "ttfx", "", None, 1):
        _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(semantic=bad)),
               f"non-enum semantic drops block: {bad!r}")
    for ok in ("ttfi", "ttfe"):
        out = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(semantic=ok))["warm_vs_cold"]
        _check(out["semantic"] == ok, f"enum semantic kept: {ok!r}")


def test_warm_vs_cold_runtime_class_enum_fail_closed():
    # runtime_class is the fail-closed PII guard: the classifier only parity-checks it as a
    # non-empty string, so the coercer must enum-validate against the PUBLIC runtime set. An
    # out-of-enum or free-text runtime (a potential leak surface) drops the whole block.
    for bad in ("kata", "runsc", "internal-pool-name", "", None, "gVisor"):
        _check("warm_vs_cold" not in rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(runtime_class=bad)),
               f"non-enum runtime_class drops block: {bad!r}")
    for ok in ("gvisor", "kata-microvm"):
        out = rs.build_results([], _prov(), GEN_AT, warm_vs_cold=_wvc(runtime_class=ok))["warm_vs_cold"]
        _check(out["runtime_class"] == ok, f"enum runtime_class kept: {ok!r}")


def test_warm_vs_cold_extra_keys_dropped():
    # Closed-schema: only the contract field-names survive; an extra key (a leak surface) is
    # dropped on read, never emitted.
    out = rs.build_results([], _prov(), GEN_AT,
                           warm_vs_cold=_wvc(failure_excerpt="secret", cluster="internal"))["warm_vs_cold"]
    _check("failure_excerpt" not in out and "cluster" not in out, "extra keys dropped")
    _check(set(out) == {"warm_p50_ms", "cold_ms", "speedup", "semantic", "runtime_class", "n_warm"},
           f"only contract fields emitted, got {sorted(out)}")


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
