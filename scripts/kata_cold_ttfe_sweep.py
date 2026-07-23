#!/usr/bin/env python3
"""Minimal honest Kata cold step-up sweep -> true-TTFE stamp (hb#5396 box 4).

Fires real cold SandboxClaims (warmpool replicas=0, runtimeClassName=kata-clh) on
the persistent scenarios cluster, brackets each rung with controller
/metrics scrapes, and assembles the {pareto, true_ttfe_webhook_stamped_claims}
record via harness.ttfe_stamp. The record is written to a BENCH_SLO_SWEEP file the
`sandbox-kata` product then derives into scenarios[0].sla_metrics.thpt_slo_basis.

Honesty spine (inherited from ttfe_stamp / prom_ttfe, never bypassed here):
  - every ttfe_p95_ms is a controller-histogram INCREMENT delta for launch_type=cold
    (the asbx#761 webhook-stamped population), NOT a fabricated number;
  - ready_per_s is the MEASURED completion rate; offered_rate_per_s is the driver's
    real fire rate; neither is invented;
  - a rung whose cold launch_type did not measure is dropped from the pareto;
  - the stamped count is the summed webhook-stamped population, None iff the metric
    was absent in every rung (dead-by-construction).

A peer collision-ack is required before running (the cluster is shared).
Cleans up all created Template/WarmPool/Claims on exit.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request

# The scenario module reads these at IMPORT — set before importing it.
os.environ.setdefault("WARMPOOL_COLD_START_RUNTIME_CLASS", "kata-clh")
os.environ.setdefault("BENCH_CLUSTER_SUBSTRATE", "gke-kata")
os.environ.setdefault("BENCH_NAMESPACE", "default")

from kubernetes import client as k8s_client  # noqa: E402
from kubernetes import config as k8s_config  # noqa: E402

from harness.scenarios import warmpool_cold_start as wcs  # noqa: E402
from harness.ttfe_stamp import build_true_ttfe_stamp, rungs_from_boundary_scrapes  # noqa: E402

NAMESPACE = os.environ["BENCH_NAMESPACE"]
RUNTIME_CLASS = os.environ["WARMPOOL_COLD_START_RUNTIME_CLASS"]
CTRL_NS = "agent-sandbox-system"
CTRL_SVC = "agent-sandbox-controller"
CTRL_PORT = 8080
# Modest, honest sweep: rung sizes chosen to bound spend/time on a kata pool that
# autoscales 0->2. Rung 1 (1 claim) absorbs the cold node scale-up; rung 2 (2
# claims) measures with nodes present. Both rungs produce a real cold pareto point.
RUNG_SIZES = [1, 2]
# Cold-sweep provenance: the WarmPool is created at replicas=0 so every claim pays a
# real cold provision. Single-sourced here so the manifest build and the record's
# params.warmpool_size stamp can never disagree on what "cold" meant for this run.
WARMPOOL_SIZE = 0
BIND_TIMEOUT_S = int(os.environ.get("KATA_SWEEP_BIND_TIMEOUT_S", "900"))
OUT_FILE = os.environ.get("KATA_SWEEP_OUT", "/tmp/kata-cold-ttfe-sweep.json")


def log(msg):
    print(f"[kata-sweep] {msg}", flush=True)


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def scrape_metrics():
    """Fresh short-lived port-forward + fetch of the controller /metrics text.

    A per-scrape pf (not one long-lived tunnel) survives the multi-minute node
    scale-up waits between rungs without a dropped-tunnel failure mode.
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


def main():
    k8s_config.load_kube_config()
    custom = k8s_client.CustomObjectsApi()
    core_v1 = k8s_client.CoreV1Api()

    suffix = f"katattfe{random.randint(1000, 9999)}"
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

    try:
        log(f"creating Template {template_name} (runtime={RUNTIME_CLASS})")
        custom.create_namespaced_custom_object(
            group=tpl_g, version=tpl_v, namespace=NAMESPACE, plural=tpl_p,
            body=wcs._build_template_manifest(template_name),
        )
        log(f"creating WarmPool {pool_name} (replicas={WARMPOOL_SIZE} -> every claim is cold)")
        custom.create_namespaced_custom_object(
            group=swp_g, version=swp_v, namespace=NAMESPACE, plural=swp_p,
            body=wcs._build_warmpool_manifest(pool_name, template_name, WARMPOOL_SIZE),
        )

        boundary_texts = []
        rates = []

        log("boundary scrape 0 (pre-fire)")
        boundary_texts.append(scrape_metrics())

        claim_seq = 0
        for rung_idx, n in enumerate(RUNG_SIZES):
            log(f"=== rung {rung_idx} — firing {n} cold claim(s) ===")
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

        rungs = rungs_from_boundary_scrapes(boundary_texts, rates)
        stamp = build_true_ttfe_stamp(rungs)  # launch_type=cold, HEADLINE_METRIC
        log(f"assembled stamp: pareto_points={len(stamp['pareto'])} "
            f"true_ttfe_webhook_stamped_claims={stamp['true_ttfe_webhook_stamped_claims']}")
        for pt in stamp["pareto"]:
            log(f"  pareto: offered={pt.get('offered_rate_per_s'):.4f}/s "
                f"ready={pt.get('ready_per_s'):.4f}/s "
                f"ttfe_p95_ms={pt.get('ttfe_p95_ms')}")

        record = {
            "params": {
                "runtime_class": RUNTIME_CLASS,
                "cluster_nodes": node_count,
                "warmpool_size": WARMPOOL_SIZE,
            },
            "true_ttfe_webhook_stamped_claims": stamp["true_ttfe_webhook_stamped_claims"],
            "pareto": stamp["pareto"],
        }
        with open(OUT_FILE, "w") as f:
            json.dump(record, f, indent=2)
        log(f"wrote sweep record -> {OUT_FILE}")
        print(json.dumps(record, indent=2), flush=True)

        # Corroboration preview (the exact gate slo_rate applies).
        cnt = stamp["true_ttfe_webhook_stamped_claims"]
        corroborated = isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 1
        has_pareto = len(stamp["pareto"]) >= 1
        if corroborated and has_pareto:
            log("PREVIEW: true-TTFE corroborated (count>=1) AND >=1 pareto point "
                "-> derive should select basis=true_ttfe")
        else:
            log(f"PREVIEW: NOT true-TTFE-ready (corroborated={corroborated} "
                f"pareto_points={len(stamp['pareto'])}) — investigate before publish")
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
