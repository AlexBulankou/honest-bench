#!/usr/bin/env python3
"""Minimal honest gVisor WARM step-up sweep -> true-TTFE stamp (#4364 1s-bar residual).

Sibling of ``scripts/kata_cold_ttfe_sweep.py``, inverted along the warm/cold axis:
fires real WARM SandboxClaims (warmpool replicas>0, runtimeClassName=gvisor) on the
persistent scenarios cluster, brackets each rung with controller /metrics scrapes,
and assembles the ``{pareto, true_ttfe_webhook_stamped_claims}`` record via
harness.ttfe_stamp with ``launch_type="warm"``. The record is the webhook-corroborated
true-TTFE basis the gVisor warm-pool matrix cell's 1s bar needs: today that bar rests
on the ``acq_p95`` uncorroborated acquire-side floor (``***U``); a webhook-stamped warm
sweep is the only basis that promotes it to a bare (non-``***U``) rate.

## Why a warm sweep must RE-WARM between rungs (the cold-sweep has no analog)

Firing N claims drains N warm replicas from the pool; the controller then replenishes.
If rung K+1 fires before the pool re-reaches its warm target, those claims bind off a
COLD-provisioning slot and the controller stamps them ``launch_type="cold"`` — so the
warm pareto silently measures cold latency. Between every rung we therefore block on
``_wait_for_pool_warm(target_ready=WARMPOOL_SIZE, stability_polls>1)`` (hb#379: a single
instantaneous at-target poll is indistinguishable from a draining peak). The initial
pre-fire warm gate is the same call. This is the load-bearing honesty invariant of the
warm sweep — an unwaited rung is a cold measurement wearing a warm label.

Honesty spine (inherited from ttfe_stamp / prom_ttfe, never bypassed here):
  - every ttfe_p95_ms is a controller-histogram INCREMENT delta for launch_type=warm
    (the asbx#761 webhook-stamped population), NOT a fabricated number;
  - ready_per_s is the MEASURED completion rate; offered_rate_per_s is the driver's
    real fire rate; neither is invented;
  - a rung whose warm launch_type did not measure is dropped from the pareto;
  - the stamped count is the summed webhook-stamped population, None iff the metric
    was absent in every rung (dead-by-construction) -> the slo_rate read-back guard
    then discards the true-TTFE basis and falls to the literal bases.

A peer collision-ack is required before running (the cluster is shared, #4804) and the
fire is NON-REFLEXIVE. Cleans up all created Template/WarmPool/Claims on exit.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request

# The scenario module reads these at IMPORT — set before importing it. gVisor warm
# runs on the persistent scenarios cluster's gke-sandbox substrate (kata uses gke-kata).
os.environ.setdefault("WARMPOOL_COLD_START_RUNTIME_CLASS", "gvisor")
os.environ.setdefault("BENCH_CLUSTER_SUBSTRATE", "gke-sandbox")
os.environ.setdefault("BENCH_NAMESPACE", "default")

from kubernetes import client as k8s_client  # noqa: E402
from kubernetes import config as k8s_config  # noqa: E402

from harness.scenarios import warmpool_cold_start as wcs  # noqa: E402
from harness.ttfe_stamp import build_true_ttfe_stamp, rungs_from_boundary_scrapes  # noqa: E402

NAMESPACE = os.environ["BENCH_NAMESPACE"]
RUNTIME_CLASS = os.environ["WARMPOOL_COLD_START_RUNTIME_CLASS"]
# WARM launch_type is the whole point of this sweep — the stamp is assembled against
# the controller's launch_type="warm" histogram series, not the cold series the kata
# sibling uses. Single-sourced so the assemble helper and the fire path can't diverge.
LAUNCH_TYPE = "warm"
CTRL_NS = "agent-sandbox-system"
CTRL_SVC = "agent-sandbox-controller"
CTRL_PORT = 8080
# Modest, honest sweep. Each rung fires <= WARMPOOL_SIZE claims so every claim can bind
# off a warm slot; the pool re-warms to WARMPOOL_SIZE between rungs. Two rungs give two
# real warm pareto points at different offered rates.
RUNG_SIZES = [1, 2]
# Warm-sweep provenance: WARMPOOL_SIZE>0 so claims bind warm. Single-sourced here so the
# manifest build and the record's params.warmpool_size stamp can never disagree on what
# "warm" meant for this run. Sized to cover max(RUNG_SIZES) with no headroom waste — a
# larger gVisor warm pool is pure standing spend on sandbox nodes.
WARMPOOL_SIZE = max(RUNG_SIZES)
BIND_TIMEOUT_S = int(os.environ.get("GVISOR_SWEEP_BIND_TIMEOUT_S", "900"))
# Warm gate: reach WARMPOOL_SIZE ready replicas, sustained for STABILITY_POLLS
# consecutive 1s polls, within WARMUP_TIMEOUT_S. gVisor cold-provisions the pool's
# initial fill and each replenishment, so the timeout is generous.
WARMUP_TIMEOUT_S = int(os.environ.get("GVISOR_SWEEP_WARMUP_TIMEOUT_S", "600"))
WARMUP_STABILITY_POLLS = int(os.environ.get("GVISOR_SWEEP_WARMUP_STABILITY_POLLS", "3"))
OUT_FILE = os.environ.get("GVISOR_SWEEP_OUT", "/tmp/gvisor-warm-ttfe-sweep.json")


def log(msg):
    print(f"[gvisor-warm-sweep] {msg}", flush=True)


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def scrape_metrics():
    """Fresh short-lived port-forward + fetch of the controller /metrics text.

    A per-scrape pf (not one long-lived tunnel) survives the multi-minute re-warm
    waits between rungs without a dropped-tunnel failure mode.
    """
    port = _free_local_port()
    pf = subprocess.Popen(
        ["kubectl", "port-forward", "-n", CTRL_NS,
         f"svc/{CTRL_SVC}", f"{port}:{CTRL_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        last_err = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/metrics", timeout=5
                ) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001 — retry until pf is up
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"metrics scrape failed to connect: {last_err}")
    finally:
        pf.terminate()
        try:
            pf.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pf.kill()


def _sample_node_count(core_v1):
    try:
        return len((core_v1.list_node() or {}).items or [])
    except Exception as e:  # noqa: BLE001
        log(f"node-count sample failed: {e}")
        return None


def assemble_record(boundary_texts, rates, *, runtime_class, node_count,
                    warmpool_size, launch_type=LAUNCH_TYPE):
    """Pure offline assembly: boundary scrapes + per-rung rates -> the sweep record.

    Factored out of the fire path so the honesty-critical decision — that the stamp is
    built against ``launch_type="warm"`` (NOT the cold series) — is unit-testable with
    captured scrape text and no live cluster. Mirrors the kata sibling's record shape
    exactly (``{params, true_ttfe_webhook_stamped_claims, pareto}``) so the shared
    slo_rate read-back guard consumes both identically; only ``launch_type`` and the
    ``params`` values differ.
    """
    rungs = rungs_from_boundary_scrapes(boundary_texts, rates)
    stamp = build_true_ttfe_stamp(rungs, launch_type=launch_type)
    return {
        "params": {
            "runtime_class": runtime_class,
            "cluster_nodes": node_count,
            "warmpool_size": warmpool_size,
            "launch_type": launch_type,
        },
        "true_ttfe_webhook_stamped_claims": stamp["true_ttfe_webhook_stamped_claims"],
        "pareto": stamp["pareto"],
    }


def main():
    k8s_config.load_kube_config()
    custom = k8s_client.CustomObjectsApi()
    core_v1 = k8s_client.CoreV1Api()

    suffix = f"gvwttfe{random.randint(1000, 9999)}"
    template_name = f"tmpl-{suffix}"
    pool_name = f"pool-{suffix}"
    created_claims = []

    tpl_g, tpl_v, tpl_p = wcs._TPL_GVR
    swp_g, swp_v, swp_p = wcs._SWP_GVR
    clm_g, clm_v, clm_p = wcs._CLM_GVR

    def cleanup():
        log("cleanup: deleting claims, warmpool, template")
        for name in created_claims:
            try:
                custom.delete_namespaced_custom_object(
                    group=clm_g, version=clm_v, namespace=NAMESPACE,
                    plural=clm_p, name=name,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  claim {name} delete: {e}")
        for (g, v, p, name) in (
            (swp_g, swp_v, swp_p, pool_name),
            (tpl_g, tpl_v, tpl_p, template_name),
        ):
            try:
                custom.delete_namespaced_custom_object(
                    group=g, version=v, namespace=NAMESPACE, plural=p, name=name,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  {p}/{name} delete: {e}")

    def wait_warm(label):
        log(f"{label}: waiting for pool warm (readyReplicas>={WARMPOOL_SIZE}, "
            f"{WARMUP_STABILITY_POLLS} consecutive polls, timeout {WARMUP_TIMEOUT_S}s)")
        wcs._wait_for_pool_warm(
            custom, pool_name=pool_name, target_ready=WARMPOOL_SIZE,
            timeout_s=WARMUP_TIMEOUT_S, stability_polls=WARMUP_STABILITY_POLLS,
        )
        log(f"{label}: pool warm")

    try:
        log(f"creating Template {template_name} (runtime={RUNTIME_CLASS})")
        custom.create_namespaced_custom_object(
            group=tpl_g, version=tpl_v, namespace=NAMESPACE, plural=tpl_p,
            body=wcs._build_template_manifest(template_name),
        )
        log(f"creating WarmPool {pool_name} (replicas={WARMPOOL_SIZE} -> claims bind warm)")
        custom.create_namespaced_custom_object(
            group=swp_g, version=swp_v, namespace=NAMESPACE, plural=swp_p,
            body=wcs._build_warmpool_manifest(pool_name, template_name, WARMPOOL_SIZE),
        )

        # Initial warm gate BEFORE the first fire — the invariant that makes rung 0 warm.
        wait_warm("pre-fire")

        boundary_texts = []
        rates = []

        log("boundary scrape 0 (pre-fire, pool warm)")
        boundary_texts.append(scrape_metrics())

        claim_seq = 0
        for rung_idx, n in enumerate(RUNG_SIZES):
            if rung_idx > 0:
                # RE-WARM between rungs: the previous rung drained n slots; block until
                # the pool is warm again so this rung's claims bind warm, not cold.
                wait_warm(f"re-warm before rung {rung_idx}")

            log(f"=== rung {rung_idx} — firing {n} warm claim(s) ===")
            claim_names = []
            create_times = {}
            for _ in range(n):
                name = f"claim{claim_seq:03d}-{suffix}"
                claim_seq += 1
                custom.create_namespaced_custom_object(
                    group=clm_g, version=clm_v, namespace=NAMESPACE, plural=clm_p,
                    body=wcs._build_claim_manifest(name, pool_name),
                )
                create_times[name] = time.monotonic()
                claim_names.append(name)
                created_claims.append(name)
            fire_span = (
                create_times[claim_names[-1]] - create_times[claim_names[0]]
            ) or 1e-9
            offered_rate = n / fire_span
            log(f"fired {n} claim(s) in {fire_span:.3f}s; polling Ready+bound "
                f"(timeout {BIND_TIMEOUT_S}s)")

            bound_at, pending, _sbx, _ttfe = wcs._measure_claim_latencies(
                claim_names, timeout_s=BIND_TIMEOUT_S,
                ttfe_enabled=False, create_times=create_times,
            )
            n_ready = len(bound_at)
            if n_ready == 0:
                log(f"rung {rung_idx}: 0 claims bound within timeout — "
                    "no ready_per_s; rung will drop from pareto")
                ready_per_s = 0.0
            else:
                first_create = min(create_times[k] for k in bound_at)
                last_ready = max(bound_at.values())
                ready_span = (last_ready - first_create) or 1e-9
                ready_per_s = n_ready / ready_span
            log(f"rung {rung_idx}: {n_ready}/{n} ready; "
                f"offered_rate={offered_rate:.4f}/s ready_per_s={ready_per_s:.4f}/s "
                f"pending={sorted(pending)}")

            # Let the controller observe the histogram for the just-bound claims
            # before the boundary scrape (Observe fires at Ready processing).
            time.sleep(5)
            log(f"boundary scrape {rung_idx + 1} (post-rung {rung_idx})")
            boundary_texts.append(scrape_metrics())
            rates.append({
                "offered_rate_per_s": offered_rate,
                "ready_per_s": ready_per_s,
            })

        node_count = _sample_node_count(core_v1)
        log(f"node_count sampled: {node_count}")

        record = assemble_record(
            boundary_texts, rates,
            runtime_class=RUNTIME_CLASS, node_count=node_count,
            warmpool_size=WARMPOOL_SIZE, launch_type=LAUNCH_TYPE,
        )
        log(f"assembled stamp: pareto_points={len(record['pareto'])} "
            f"true_ttfe_webhook_stamped_claims={record['true_ttfe_webhook_stamped_claims']}")
        for pt in record["pareto"]:
            log(f"  pareto: offered={pt.get('offered_rate_per_s'):.4f}/s "
                f"ready={pt.get('ready_per_s'):.4f}/s "
                f"ttfe_p95_ms={pt.get('ttfe_p95_ms')}")

        with open(OUT_FILE, "w") as f:
            json.dump(record, f, indent=2)
        log(f"wrote sweep record -> {OUT_FILE}")
        print(json.dumps(record, indent=2), flush=True)

        # Corroboration preview (the exact gate slo_rate applies).
        cnt = record["true_ttfe_webhook_stamped_claims"]
        corroborated = isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 1
        has_pareto = len(record["pareto"]) >= 1
        if corroborated and has_pareto:
            log("PREVIEW: true-TTFE corroborated (count>=1) AND >=1 pareto point "
                "-> derive should select basis=true_ttfe (promotes the 1s bar off ***U)")
        else:
            log(f"PREVIEW: NOT true-TTFE-ready (corroborated={corroborated} "
                f"pareto_points={len(record['pareto'])}) — investigate before publish")
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
