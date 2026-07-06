"""Upstream-blocker link mapping (hb#181).

Loads render/upstream_links.json — the machine-readable mirror of the
hand-maintained UPSTREAM_BLOCKERS.md — and exposes two formatters:

- upstream_cell_refs(reason): compact per-cell suffix appended AFTER the
  link_pending-wrapped token in matrix data cells, e.g.
  " [#873](...)→[#893 in review](...)". Returns "" for unmapped/None
  reasons, so non-upstream pending classes (cluster-fire etc.) render
  unchanged.
- upstream_prose_refs(cls): fuller prose form for the matrix legend and
  the WORK_IN_PROGRESS.md entry, e.g.
  "[agent-sandbox#873](...) (issue, open) → fix [agent-sandbox#893](...)
  (PR, in review)".

Classes whose key matches a PENDING_REASONS member are consumed by the
matrix automatically; other classes are mapping-only seeds for future
renderers and the daily live-state verifier.

All refs point at public upstream OSS repos (kubernetes-sigs/agent-sandbox,
agent-substrate/substrate) — safe on the public rendered page. Validation
is fail-loud at import, mirroring wip.py's catalog assertion.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_LINKS_PATH = os.path.join(_HERE, "upstream_links.json")

_KINDS = {"issue", "pr"}
_ROLES = {"blocks", "fix-in-flight"}
_STATUSES = {"open", "in-review", "merged", "closed"}

# Public-safety allow-list, enforced at LOAD time (hb#182 follow-up): every
# ref must point at a public upstream OSS repo, so even a programmatically
# generated mapping can never put a non-public tracker on the rendered page.
# The shipped-data test asserts the same set; the loader is the runtime gate.
_PUBLIC_REPOS = {"kubernetes-sigs/agent-sandbox", "agent-substrate/substrate"}

# Statuses that read better with a space in rendered text.
STATUS_LABELS = {
    "open": "open",
    "in-review": "in review",
    "merged": "merged",
    "closed": "closed",
}
# _cell_token/_prose_token index STATUS_LABELS by validated status — keep the
# curated label map in lockstep with the enum or rendering KeyErrors.
assert set(STATUS_LABELS) == _STATUSES

# The classes the matrix consumes today; validation requires them so the
# rendered gVisor x Resume row (upstream-blocked) and the 07-06 honest-empty
# SLO-rate cluster cells (trust-gate warm / no-compliant-rung cold) can never
# silently lose their refs.
_REQUIRED_CLASSES = {"upstream-blocked", "trust-gate", "no-compliant-rung"}


def _load():
    with open(_LINKS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    classes = data.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise AssertionError("upstream_links.json: 'classes' must be a non-empty object")
    missing = _REQUIRED_CLASSES - set(classes)
    if missing:
        raise AssertionError(
            "upstream_links.json missing required class(es): %s" % ", ".join(sorted(missing))
        )
    for cls, entry in classes.items():
        refs = entry.get("refs")
        if not isinstance(refs, list) or not refs:
            raise AssertionError("upstream_links.json class %r: 'refs' must be a non-empty list" % cls)
        for ref in refs:
            repo = ref.get("repo")
            if not isinstance(repo, str) or repo.count("/") != 1:
                raise AssertionError("upstream_links.json class %r: bad repo %r" % (cls, repo))
            if repo not in _PUBLIC_REPOS:
                raise AssertionError(
                    "upstream_links.json class %r: repo %r not in the public-OSS "
                    "allow-list %s" % (cls, repo, sorted(_PUBLIC_REPOS))
                )
            if not isinstance(ref.get("number"), int):
                raise AssertionError("upstream_links.json class %r: bad number %r" % (cls, ref.get("number")))
            if ref.get("kind") not in _KINDS:
                raise AssertionError("upstream_links.json class %r: bad kind %r" % (cls, ref.get("kind")))
            if ref.get("role") not in _ROLES:
                raise AssertionError("upstream_links.json class %r: bad role %r" % (cls, ref.get("role")))
            if ref.get("status") not in _STATUSES:
                raise AssertionError("upstream_links.json class %r: bad status %r" % (cls, ref.get("status")))
    return data


_DATA = _load()
CLASSES = _DATA["classes"]
META = _DATA["_meta"]


def ref_url(ref):
    """GitHub URL for a ref dict."""
    path = "issues" if ref["kind"] == "issue" else "pull"
    return "https://github.com/%s/%s/%d" % (ref["repo"], path, ref["number"])


def _cell_token(ref):
    """Compact per-ref markdown link: [#873](url) or [#893 in review](url)."""
    label = "#%d" % ref["number"]
    if ref["role"] == "fix-in-flight":
        label += " %s" % STATUS_LABELS[ref["status"]]
    return "[%s](%s)" % (label, ref_url(ref))


def upstream_cell_refs(reason):
    """Compact refs suffix for a matrix data cell; "" when unmapped/None."""
    if not reason or reason not in CLASSES:
        return ""
    return " " + "→".join(_cell_token(r) for r in CLASSES[reason]["refs"])


def _prose_token(ref):
    short = ref["repo"].rsplit("/", 1)[-1]
    kind_label = "PR" if ref["kind"] == "pr" else "issue"
    link = "[%s#%d](%s)" % (short, ref["number"], ref_url(ref))
    tail = "(%s, %s)" % (kind_label, STATUS_LABELS[ref["status"]])
    if ref["role"] == "fix-in-flight":
        return "fix %s %s" % (link, tail)
    return "%s %s" % (link, tail)


def upstream_prose_refs(cls):
    """Fuller prose refs for legend / WIP entry; "" when unmapped/None."""
    if not cls or cls not in CLASSES:
        return ""
    return " → ".join(_prose_token(r) for r in CLASSES[cls]["refs"])
