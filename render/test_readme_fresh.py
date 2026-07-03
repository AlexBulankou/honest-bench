"""CI guard: committed README.md must equal the FULL render/generate.py output.

Regression guard for the #106/#108 drift class (honest-bench #109): a PR that
changes render logic but forgets to run `python3 -m render.generate` ships a stale
public page silently. The existing render tests check render *function* output, and
the only pre-existing byte-parity check compared README against `_PREAMBLE` (the
preamble text ONLY, not the rendered matrix/blocks) — so a stale rendered table
sailed through CI (#106 landed the code, the public matrix stayed stale until #108).

This closes that gap: it regenerates the WHOLE README in-memory via build_readme()
and asserts it matches the committed artifact byte-for-byte. "Forgot to regen" is
now a hard PR failure, not a dormant public-page drift — the same honest-by-
construction discipline the matrix already aims for.
"""
import difflib
import os

from generate import build_readme, build_details, _repo_root
from wip import build_work_in_progress


def _committed_readme():
    with open(os.path.join(_repo_root(), "README.md")) as fh:
        return fh.read()


def _committed_details():
    with open(os.path.join(_repo_root(), "DETAILS.md")) as fh:
        return fh.read()


def _committed_wip():
    with open(os.path.join(_repo_root(), "WORK_IN_PROGRESS.md")) as fh:
        return fh.read()


def test_committed_readme_equals_full_generate_output():
    committed = _committed_readme()
    generated = build_readme()
    if committed != generated:
        diff = "\n".join(
            difflib.unified_diff(
                generated.splitlines(),
                committed.splitlines(),
                fromfile="render/generate.py output",
                tofile="committed README.md",
                lineterm="",
            )
        )
        raise AssertionError(
            "committed README.md is STALE vs render/generate.py output — run "
            "`python3 -m render.generate` and commit the result.\n" + diff
        )


def test_committed_details_equals_full_generate_output():
    # hb#134 deep-dive appendix: same freshness guard as the README, for DETAILS.md.
    committed = _committed_details()
    generated = build_details()
    if committed != generated:
        diff = "\n".join(
            difflib.unified_diff(
                generated.splitlines(),
                committed.splitlines(),
                fromfile="render/generate.py build_details output",
                tofile="committed DETAILS.md",
                lineterm="",
            )
        )
        raise AssertionError(
            "committed DETAILS.md is STALE vs render/generate.py output — run "
            "`python3 -m render.generate` and commit the result.\n" + diff
        )


def test_committed_wip_equals_full_generate_output():
    # hb#166: WORK_IN_PROGRESS.md is machine-rendered from the closed pending-reason enum
    # (wip.py), same generate-only freshness discipline as README/DETAILS.
    committed = _committed_wip()
    generated = build_work_in_progress()
    if committed != generated:
        diff = "\n".join(
            difflib.unified_diff(
                generated.splitlines(),
                committed.splitlines(),
                fromfile="render/wip.py build_work_in_progress output",
                tofile="committed WORK_IN_PROGRESS.md",
                lineterm="",
            )
        )
        raise AssertionError(
            "committed WORK_IN_PROGRESS.md is STALE vs render output — run "
            "`python3 -m render.generate` and commit the result.\n" + diff
        )


def test_build_wip_is_deterministic():
    assert build_work_in_progress() == build_work_in_progress()


def test_build_details_is_deterministic():
    assert build_details() == build_details()


def test_build_readme_is_deterministic():
    # The render path has no wall-clock input (generated_at/measured_at come from the
    # results JSON, not datetime.now()), so two consecutive renders must be byte-
    # identical. This guarantees the freshness check above can never flap on a
    # timestamp — a mismatch is always a real stale-artifact, never render nondeterminism.
    assert build_readme() == build_readme()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"test_readme_fresh: all {len(fns)} assertions passed")


if __name__ == "__main__":
    _run_all()
