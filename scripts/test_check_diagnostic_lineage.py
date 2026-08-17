#!/usr/bin/env python3
"""Tests for the diagnostic-lineage merge guard (hb#646 root-cause follow-up).

Dependency-free: `python3 scripts/test_check_diagnostic_lineage.py` (exit 0 = pass).
Auto-discovered by the offline unit-tests gate (find . -name 'test_*.py').
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_diagnostic_lineage as g  # noqa: E402


def _prov(fork_sha=None, base=None, fix_count=None):
    prov = {}
    if fork_sha is not None:
        prov["fork_sha"] = fork_sha
    if base is not None:
        prov["fork_base_upstream_sha"] = base
    if fix_count is not None:
        prov["fork_fix_count"] = fix_count
    return prov


def _pr(provenance=None, *, product="sandbox", include_provenance=True):
    d = {"product": product}
    if include_provenance:
        d["provenance"] = provenance if provenance is not None else {}
    return d


def _write(tmp, name, obj):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        json.dump(obj, fh)
    return p


def _run(pr_obj, *, pr_malformed=False):
    with tempfile.TemporaryDirectory() as tmp:
        if pr_malformed:
            pr = os.path.join(tmp, "pr.json")
            with open(pr, "w") as fh:
                fh.write("{not valid json")
        else:
            pr = _write(tmp, "pr.json", pr_obj)
        return g.check(pr)


def main():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    # Signature detection -----------------------------------------------------
    # fork_fix_count == 0 alone (the actual hb#643/#644 shape)
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="aaa111", base="bbb222", fix_count=0))
    )
    expect(code == 3 and len(reasons) == 1 and "fork_fix_count == 0" in reasons[0],
           "fork_fix_count == 0 alone must block (exit 3)")

    # fork_sha == fork_base_upstream_sha alone (self-referential "fork")
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="ccc333", base="ccc333", fix_count=1))
    )
    expect(code == 3 and len(reasons) == 1 and "byte-identical" in reasons[0],
           "fork_sha == fork_base_upstream_sha alone must block (exit 3)")

    # Both signatures present simultaneously
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="ddd444", base="ddd444", fix_count=0))
    )
    expect(code == 3 and len(reasons) == 2,
           "both signatures present must report both reasons (exit 3)")

    # Neither signature present -> clean
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="eee555", base="fff666", fix_count=1))
    )
    expect(code == 0 and not reasons,
           "distinct shas + positive fix_count is a clean production pin (exit 0)")

    # provenance missing entirely -> clean (nothing to flag)
    code, reasons, _ = _run(_pr(include_provenance=False))
    expect(code == 0 and not reasons, "no provenance key at all -> clean (exit 0)")

    # provenance present but empty -> clean
    code, reasons, _ = _run(_pr({}))
    expect(code == 0 and not reasons, "empty provenance dict -> clean (exit 0)")

    # fork_fix_count present but bool (excluded by design, same as schema.py) -> clean on that axis
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="ggg777", base="hhh888", fix_count=False))
    )
    expect(code == 0 and not reasons,
           "bool fork_fix_count is not an int fix-count -> clean on that axis (exit 0)")

    # fork_fix_count > 0 (a real validated pin) -> clean
    code, reasons, _ = _run(
        _pr(_prov(fork_sha="iii999", base="jjj000", fix_count=1))
    )
    expect(code == 0 and not reasons, "fork_fix_count == 1 is a valid fix pin (exit 0)")

    # Error handling -----------------------------------------------------------
    code, _reasons, _ = _run(None, pr_malformed=True)
    expect(code == 2, "malformed PR json -> fail closed (exit 2)")

    # Override path (main() wiring) ---------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        pr = _write(tmp, "pr.json", _pr(_prov(fork_sha="kkk111", base="kkk111", fix_count=0)))
        expect(g.main(["--pr", pr]) == 3,
               "un-overridden diagnostic-lineage signature via main() exits 3")
        expect(g.main(["--pr", pr, "--allow-diagnostic"]) == 0,
               "[DIAGNOSTIC-LINEAGE-OK] override (--allow-diagnostic) downgrades to warn (exit 0)")

    with tempfile.TemporaryDirectory() as tmp:
        pr = _write(tmp, "pr.json", _pr(_prov(fork_sha="lll222", base="mmm333", fix_count=1)))
        expect(g.main(["--pr", pr]) == 0,
               "clean pin via main() exits 0 regardless of --allow-diagnostic")
        expect(g.main(["--pr", pr, "--allow-diagnostic"]) == 0,
               "--allow-diagnostic is a no-op on an already-clean pin")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("check_diagnostic_lineage: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
