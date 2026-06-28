"""Offline tests for the closed-schema emitter — no cluster, no I/O.

Run with bare python3 (no pytest dependency, so the auto-refresh GH-runner needs
nothing extra):  python3 -m sandbox.harness.test_results_schema
or directly:      python3 bench-repo/sandbox/harness/test_results_schema.py

Each test asserts a public-safety property of build_results. The leak-suspenders
tests (excerpt-never-emitted, non-numeric-sla-dropped, unsafe-key-dropped,
non-schema-key-dropped) are the load-bearing ones: they prove an internal string
cannot reach the public results.json even if a scenario tries to surface it.
"""

from __future__ import annotations

from . import results_schema as rs

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
