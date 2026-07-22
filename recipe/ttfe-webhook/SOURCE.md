# ttfe-webhook — vendored source provenance

These manifests are the **unmodified upstream example** from
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox),
`examples/webhook-inject-timestamp/`, merged as **PR #761**
("Add guide and example resources for Mutating Admission Webhook").

## Pinned commit

| field | value |
|---|---|
| upstream repo | `kubernetes-sigs/agent-sandbox` |
| PR | #761 |
| merge commit | `adfe540992a5bad92190284f458236dc566a2314` |
| merged_at | 2026-07-22T17:43:50Z |
| upstream path | `examples/webhook-inject-timestamp/` |

## What the webhook does

A Go HTTP mutating-admission webhook that stamps
`agents.x-k8s.io/webhook-first-observed-at` (ms-precision, RFC3339Nano) on
`sandboxclaims.extensions.agents.x-k8s.io/v1beta1` CREATE. This gives the
harness a ms-precision `t0` for ClaimStartupLatency, replacing the
second-truncated `creationTimestamp` basis (which is an upper bound). See
`recipe/deploy-ttfe-webhook.sh` header + honest-bench#5396.

## Local modification

The manifests are byte-for-byte upstream **except** `webhook-deployment.yaml`'s
container `image:`, rewritten from the upstream `kind.local/webhook-image:latest`
placeholder to our pre-built, digest-pinned image. The project segment is the
`__WEBHOOK_IMAGE_PROJECT__` placeholder — `deploy-ttfe-webhook.sh` renders it to
the GCP project id at apply time (default: `gcloud config get-value project`), so
the private project id never lives in this public tree:

```
us-central1-docker.pkg.dev/__WEBHOOK_IMAGE_PROJECT__/honest-bench/webhook-inject-timestamp:asbx-adfe5409@sha256:9177721d8244b7208472229fff31496f52bb1f76bc0f1aadf25903fed3548ac5
```

The image is built from the same pinned upstream source — no fork, no per-fire
build. `asbx-adfe5409` tag == upstream merge commit short-SHA. Only the tag +
digest are load-bearing for reproducibility; the project segment is a deploy-time
render target.

## Rebuild command (if the pinned image is ever lost)

Fetch the pinned upstream source and build via Cloud Build (from any a4s pod
with user ADC):

Supply the GCP project via `PROJECT` (defaults to the gcloud active project) —
it is not hardcoded here so the private project id stays out of this public tree:

```bash
SHA=adfe540992a5bad92190284f458236dc566a2314
PROJECT="${PROJECT:-$(gcloud config get-value project)}"
DIR=$(mktemp -d)
for f in Dockerfile go.mod go.sum main.go; do
  curl -fsSL \
    "https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/${SHA}/examples/webhook-inject-timestamp/${f}" \
    -o "${DIR}/${f}"
done
gcloud builds submit "${DIR}" \
  --project "${PROJECT}" \
  --tag "us-central1-docker.pkg.dev/${PROJECT}/honest-bench/webhook-inject-timestamp:asbx-adfe5409"
```

Then re-pin `webhook-deployment.yaml`'s `image:` to the new
`...:asbx-adfe5409@sha256:<digest>` printed by the build.
