#!/usr/bin/env python3
"""Honest Kata unique-image cold step-up sweep -> true-TTFE stamp.

Sibling of ``kata_cold_ttfe_sweep.py`` (hb#5396 box 4, which resolved
``warmpool_cold_start``'s Kata cold cell). That sweep proves the
cold-*provision* number: every claim pays a real cold provision, but all
claims share ONE already-cached ``busybox:1.36`` image, so no claim ever pays a
real image-layer *pull*. The outstanding "Kata + microVM · Unique-
image cold · Throughput @<5s/node" matrix cell needs the distinct cold-*pull*
semantic that ``harness/scenarios/native_digest_cold.py`` names but cannot
itself produce a throughput/pareto axis for: that scenario fires a bare
Sandbox CR (no Claim -> no true-TTFE webhook stamp) strictly serially (no
concurrent-rung throughput semantic) -- see its module docstring.

This script closes the gap by reusing warmpool_cold_start's Template/WarmPool/
Claim mechanics (same as kata_cold_ttfe_sweep.py, so the SAME webhook-stamped
true-TTFE corroboration path applies) but gives EVERY claim in the sweep its
OWN SandboxTemplate pinned to a DISTINCT, never-before-pulled image tag, so
each claim pays a real, unshared cold layer-pull -- not just a cold provision
of an already-cached image.

## Why per-claim Template/WarmPool, not one shared Template

A SandboxTemplate pins exactly one pod image; every Claim that binds through
a given WarmPool's Template shares that image. To guarantee N claims each
pay a genuinely independent cold pull, this script provisions N (Template,
WarmPool replicas=0) pairs -- one per claim -- each pinned to a different
image tag via a per-call monkeypatch of ``wcs._SANDBOX_IMAGE`` (the module-
global that ``wcs._build_template_manifest`` reads at call time; confirmed by
inspection, not assumed -- see the manifest builder's own body).

## Why distinct busybox *version* tags, not a self-built/pushed image

``native_digest_cold.py``'s own docstring: "We cannot push to a registry from
a portable harness." This script honors the same constraint by construction:
no image is ever built or pushed. Instead it selects public busybox tags
whose upstream manifest digest is independently confirmed (Docker Hub
registry API, checked at authoring time -- see KATA_UNIQUE_IMAGE_TAGS below)
to differ from `busybox:1.36` (this cluster's only-ever-pulled default) and
from each other. A tag with a distinct manifest digest is a distinct set of
layer blobs -- a node that has only ever pulled `busybox:1.36` has NOT cached
any of these layers, so the pull is genuinely cold. Every busybox variant
still ships the same `ash`-as-`/bin/sh` applet the pod command needs
(`sh -c sleep 600`), so swapping tags does not change what the container runs.

Confirmed-distinct amd64 manifest digests at authoring time (2026-08-26):
  busybox:1.36    -> sha256:b7f3d86d...9f  (already cached on this cluster -- NOT used here)
  busybox:1.35.0  -> sha256:584c3aa5...10
  busybox:1.34.1  -> sha256:51de9138...dd
  busybox:1.33.1  -> sha256:febcf61c...0b

## Honesty spine (inherited from ttfe_stamp / prom_ttfe, never bypassed here)

Same as kata_cold_ttfe_sweep.py: every ttfe_p95_ms is a controller-histogram
INCREMENT delta for launch_type=cold (the asbx#761 webhook-stamped
population), NOT a fabricated number; ready_per_s is the MEASURED completion
rate; a rung whose cold launch_type did not measure is dropped from the
pareto; the stamped count is the summed webhook-stamped population, None iff
the metric was absent in every rung.

## Provenance

The output record's ``params`` block additionally carries ``images`` (the
per-claim image tag actually used) and ``cold_start_mode: "cold-pull"`` so a
downstream reader can distinguish this cell's semantic from
kata_cold_ttfe_sweep.py's cold-provision record at a glance -- mirrors
native_digest_cold.py's own ``measured_with``/``cold_start_mode`` provenance
convention (hb#723 / #3885).

A peer collision-ack is required before running (the cluster is shared).
Cleans up all created Templates/WarmPools/Claims on exit.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request

# The scenario module reads these at IMPORT -- set before importing it.
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
# Same rung shape as kata_cold_ttfe_sweep.py: rung 0 (1 claim) absorbs the
# cold node scale-up, rung 1 (2 claims) measures with nodes present. One
# UNIQUE image per claim -- 3 claims total across the two rungs needs 3 tags.
RUNG_SIZES = [1, 2]
_DEFAULT_TAGS = "busybox:1.35.0,busybox:1.34.1,busybox:1.33.1"
UNIQUE_IMAGE_TAGS = [
    t.strip()
    for t in os.environ.get("KATA_UNIQUE_IMAGE_TAGS", _DEFAULT_TAGS).split(",")
    if t.strip()
]
BIND_TIMEOUT_S = int(os.environ.get("KATA_SWEEP_BIND_TIMEOUT_S", "900"))
OUT_FILE = os.environ.get(
    "KATA_UNIQUE_IMAGE_SWEEP_OUT", "/tmp/kata-unique-image-cold-ttfe-sweep.json"
)


def log(msg):
    print(f"[kata-unique-image-sweep] {msg}", flush=True)


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def scrape_metrics():
    """Fresh short-lived port-forward + fetch of the controller /metrics text."""
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
            except Exception as e:  # noqa: BLE001 -- retry until pf is up
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


def assemble_record(boundary_texts, rates, *, runtime_class, node_count, images):
    """Pure offline assembly: boundary scrapes + per-rung rates -> the sweep record."""
    rungs = rungs_from_boundary_scrapes(boundary_texts, rates)
    stamp = build_true_ttfe_stamp(rungs)  # launch_type=cold (default), HEADLINE_METRIC
    return {
        "params": {
            "runtime_class": runtime_class,
            "cluster_nodes": node_count,
            "warmpool_size": 0,
            "images": images,
            "cold_start_mode": "cold-pull",
        },
        "true_ttfe_webhook_stamped_claims": stamp["true_ttfe_webhook_stamped_claims"],
        "pareto": stamp["pareto"],
    }


def main():
    n_claims_needed = sum(RUNG_SIZES)
    if len(UNIQUE_IMAGE_TAGS) < n_claims_needed:
        log(f"FATAL: need {n_claims_needed} unique image tags "
            f"(RUNG_SIZES={RUNG_SIZES}), got {len(UNIQUE_IMAGE_TAGS)}: "
            f"{UNIQUE_IMAGE_TAGS}")
        return 1

    k8s_config.load_kube_config()
    custom = k8s_client.CustomObjectsApi()
    core_v1 = k8s_client.CoreV1Api()

    suffix = f"katauniq{random.randint(1000, 9999)}"
    tpl_g, tpl_v, tpl_p = wcs._TPL_GVR
    swp_g, swp_v, swp_p = wcs._SWP_GVR
    clm_g, clm_v, clm_p = wcs._CLM_GVR

    created_claims = []
    created_pools = []
    created_templates = []
    images_used = []

    def cleanup():
        log("cleanup: deleting claims, warmpools, templates")
        for name in created_claims:
            try:
                custom.delete_namespaced_custom_object(
                    group=clm_g, version=clm_v, namespace=NAMESPACE,
                    plural=clm_p, name=name,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  claim {name} delete: {e}")
        for name in created_pools:
            try:
                custom.delete_namespaced_custom_object(
                    group=swp_g, version=swp_v, namespace=NAMESPACE,
                    plural=swp_p, name=name,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  warmpool {name} delete: {e}")
        for name in created_templates:
            try:
                custom.delete_namespaced_custom_object(
                    group=tpl_g, version=tpl_v, namespace=NAMESPACE,
                    plural=tpl_p, name=name,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  template {name} delete: {e}")

    def provision_one_claim_pool(idx, image_tag):
        """Create one (Template, WarmPool replicas=0) pair pinned to image_tag.

        Returns the claim-ready manifest body (not yet created) plus the pool
        name it should bind against. Splitting provision from claim-create
        lets the caller fire all of a rung's claims back-to-back once every
        rung's pool is ready, instead of paying per-pool warmup serially.
        """
        template_name = f"tmpl-{suffix}-{idx}"
        pool_name = f"pool-{suffix}-{idx}"
        wcs._SANDBOX_IMAGE = image_tag  # noqa: SLF001 -- deliberate, see module docstring
        custom.create_namespaced_custom_object(
            group=tpl_g, version=tpl_v, namespace=NAMESPACE, plural=tpl_p,
            body=wcs._build_template_manifest(template_name),
        )
        created_templates.append(template_name)
        custom.create_namespaced_custom_object(
            group=swp_g, version=swp_v, namespace=NAMESPACE, plural=swp_p,
            body=wcs._build_warmpool_manifest(pool_name, template_name, 0),
        )
        created_pools.append(pool_name)
        images_used.append(image_tag)
        return pool_name

    try:
        boundary_texts = []
        rates = []

        log("boundary scrape 0 (pre-fire)")
        boundary_texts.append(scrape_metrics())

        claim_seq = 0
        img_idx = 0
        for rung_idx, n in enumerate(RUNG_SIZES):
            log(f"=== rung {rung_idx} -- provisioning {n} unique-image pool(s) ===")
            pool_names = []
            for _ in range(n):
                tag = UNIQUE_IMAGE_TAGS[img_idx]
                img_idx += 1
                pool_names.append(provision_one_claim_pool(claim_seq, tag))
                log(f"  pool for claim {claim_seq}: image={tag}")

            log(f"=== rung {rung_idx} -- firing {n} cold claim(s), "
                f"each against its own unique-image pool ===")
            claim_names = []
            create_times = {}
            for pool_name in pool_names:
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
                log(f"rung {rung_idx}: 0 claims bound within timeout -- "
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
            images=images_used,
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

        cnt = record["true_ttfe_webhook_stamped_claims"]
        corroborated = isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 1
        has_pareto = len(record["pareto"]) >= 1
        if corroborated and has_pareto:
            log("PREVIEW: true-TTFE corroborated (count>=1) AND >=1 pareto point "
                "-> derive should select basis=true_ttfe")
        else:
            log(f"PREVIEW: NOT true-TTFE-ready (corroborated={corroborated} "
                f"pareto_points={len(record['pareto'])}) -- investigate before publish")
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
