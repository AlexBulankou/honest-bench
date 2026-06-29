"""Shared kubeconfig loader for scenario modules.

Single source of truth for how every scenario obtains its cluster credentials, so
the precedence rule lives in one place instead of being copy-pasted into each
scenario's run() body.

Precedence:

  1. If ``KUBECONFIG`` is set in the environment, load THAT kubeconfig. An
     explicit ``KUBECONFIG`` is an operator's deliberate "talk to *this* cluster
     as *this* identity" override, so it wins even when the process happens to be
     running inside a pod. This matters when the runner is a pod on cluster A but
     the suite must target cluster B (the portable cross-cluster fire): without
     this, ``load_incluster_config()`` would silently bind to the *local* pod's
     API server + ServiceAccount and ignore the operator's KUBECONFIG entirely.
  2. Otherwise, in-cluster config when running as a pod (the customer's
     run-it-in-your-own-cluster default).
  3. Otherwise, the default kubeconfig (``~/.kube/config`` for a laptop run).

This mirrors the kubernetes client's own newer ``config.load_config()``
precedence (kubeconfig before in-cluster) while keeping the in-cluster default
for the no-KUBECONFIG pod case.
"""

from __future__ import annotations

import os


def load_cluster_config() -> None:
    """Load cluster credentials for the kubernetes client (see module docstring)."""
    from kubernetes import config as k8s_config

    if os.environ.get("KUBECONFIG"):
        k8s_config.load_kube_config()
        return
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
