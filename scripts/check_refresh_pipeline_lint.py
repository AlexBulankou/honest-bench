#!/usr/bin/env python3
"""Local preflight lint for the refresh-pipeline cloudbuild configs (hb#782).

Mirrors the private repo's `preflight-cd.sh`/`check-cloudbuild-arg-length.py`
pattern: catch, offline and pre-merge, the exact Cloud Build validation
failure classes that only surfaced at live-fire time during the 2026-08-27
fire-5 arc (hb#775-#779, fixed in commits 105eebf/f072566/9ce960a/88c26c4).
Each failure class cost a full live-fire round-trip (submit -> reject/fail
-> read the error -> fix -> resubmit) because none of the three bugs below
are visible to `bash -n` or a plain YAML parse -- they are all Cloud
Build-specific or shell-specific runtime behaviors.

Three checks, run against every cloudbuild*.yaml (checks 1-2) and every
scripts/*.sh plus every cloudbuild step script (check 3) in the repo:

1. Bare uppercase substitution refs (hb#775/#776). Cloud Build scans the
   full text of every `steps[].args` string for bare (single-`$`) uppercase
   `$VAR`/`${VAR}` references and rejects the ENTIRE build at submit time if
   the name isn't a recognized built-in or `_`-prefixed user substitution --
   before a single step runs. The fix idiom is `$$`-escaping (`$VAR` ->
   `$$VAR`, `${VAR}` -> `$${VAR}`); at render time CB collapses `$$` to a
   literal `$` and hands the script to bash, which then expands the var
   normally at runtime. Scoped to `steps[].args` string values specifically
   (parsed via PyYAML, not a whole-file text scan) -- CB's own scanner never
   sees a top-level YAML comment, since the YAML parser strips it before CB
   processes any string content, and a naive whole-file scan would false-flag
   comment-only mentions of a variable name.

2. Step arg length >10,000 chars (mirrors the private repo's
   check-cloudbuild-arg-length.py, ported here per hb#782 ask #2 to close
   the private-repo-only gap and cover honest-bench's own refresh configs).

3. `curl` calls on a bracket-bearing URL without `-g`/`--globoff` (hb#779).
   curl's default URL-globbing parser reads a literal `[...]` in a URL as a
   glob range and refuses the request ("bad range in URL") unless globbing
   is disabled. Flags (a) a literal `[`/`]` directly in a curl invocation's
   URL text, and (b) a curl call referencing a variable that was assigned
   from an expression containing the literal substring `[bot]` -- the one
   concrete bracket-bearing value in this repo's pipelines (a GitHub App's
   bot login, `<slug>[bot]`). This is deliberately narrower than "any
   variable whose assignment line contains a `[`/`]` character": several
   variables in this repo's scripts (e.g. `install_id`, `GITHUB_TOKEN`) pick
   up literal brackets purely from embedded python dict-subscript syntax
   (`d["id"]`, `...["token"]`) in a nested `python3 -c '...'` command
   substitution, even though their actual runtime values (numeric ids,
   opaque tokens) carry zero URL-glob risk -- a bracket-in-assignment-line
   heuristic would false-positive on those. Keying on the `[bot]` substring
   specifically catches the one real bug class without that false-positive
   surface.

Usage:
  python3 scripts/check-refresh-pipeline-lint.py            # lint the repo
  python3 scripts/check-refresh-pipeline-lint.py --self-test # hermetic self-test
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Cloud Build built-in substitutions that are legitimately referenced bare
# (single `$`, not `$$`-escaped) -- CB itself resolves these at build-submit
# scan time, so they never trip the "not a valid built-in substitution"
# rejection that #775/#776 hit for non-built-in names. Confirmed live in
# this repo: $BUILD_ID and $PROJECT_ID both appear unescaped in
# cloudbuild-refresh-gke-kata.yaml/cloudbuild-refresh-gke-sandbox.yaml. The
# rest of this list is the standard Cloud Build built-in substitution set
# (docs), included defensively even though not all are used here yet.
CB_BUILTIN_SUBSTITUTIONS = {
    "PROJECT_ID",
    "PROJECT_NUMBER",
    "BUILD_ID",
    "REPO_NAME",
    "BRANCH_NAME",
    "TAG_NAME",
    "REVISION_ID",
    "COMMIT_SHA",
    "SHORT_SHA",
    "REPO_FULL_NAME",
    "TRIGGER_NAME",
    "TRIGGER_BUILD_CONFIG_PATH",
    "TRIGGER_ID",
    "LOCATION",
    "SERVICE_ACCOUNT",
    "SERVICE_ACCOUNT_EMAIL",
}

FAIL_THRESHOLD = 10_000
WARN_THRESHOLD = 8_000

# Matches a bare (not `$$`-doubled) `$VAR` or `${VAR}` reference where VAR is
# uppercase, optionally `_`-prefixed. The negative lookbehind is load-bearing:
# without it, the second `$` of an already-escaped `$$VAR`/`$${VAR}` would
# itself be mis-read as the start of a fresh (unescaped) reference.
_BARE_SUBST_RE = re.compile(r"(?<!\$)\$\{?(_?[A-Z][A-Z0-9_]*)\}?")


def _join_continuations(text: str):
    """Join backslash-continued shell lines into single logical lines.

    Returns a list of (logical_line, first_source_line_number) pairs so
    callers can still report a useful line number. A real curl/assignment
    invocation in these scripts routinely spans multiple physical lines via
    trailing `\\` -- e.g. `BOT_LOGIN="$(curl ... \\` / `  https://... )"` --
    so any check that scans "the curl line" or "the assignment line" as a
    single physical line would miss the URL/value that lives on the
    continuation line.
    """
    lines = text.split("\n")
    logical = []
    buf = []
    start_no = None
    for i, line in enumerate(lines, start=1):
        if start_no is None:
            start_no = i
        stripped = line.rstrip("\n")
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        logical.append((" ".join(buf), start_no))
        buf = []
        start_no = None
    if buf:
        logical.append((" ".join(buf), start_no))
    return logical


def _iter_cloudbuild_step_args(yaml_content: str, filename: str):
    """Yield (step_id, arg_index, arg_text) for every string steps[].args entry."""
    import yaml

    try:
        data = yaml.safe_load(yaml_content)
    except Exception as e:
        print(f"Error parsing YAML from {filename}: {e}", file=sys.stderr)
        return
    if not isinstance(data, dict):
        return
    steps = data.get("steps")
    if not isinstance(steps, list):
        return
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id") or f"at index {idx}"
        args = step.get("args")
        if not isinstance(args, list):
            continue
        for arg_idx, arg in enumerate(args):
            if isinstance(arg, str):
                yield step_id, arg_idx, arg


def check_bare_substitution_refs(yaml_content: str, filename: str = "cloudbuild.yaml") -> bool:
    """Fail if any steps[].args string has a bare (non-`$$`-escaped) uppercase substitution ref."""
    ok = True
    for step_id, arg_idx, arg_text in _iter_cloudbuild_step_args(yaml_content, filename):
        for m in _BARE_SUBST_RE.finditer(arg_text):
            name = m.group(1)
            if name.startswith("_"):
                continue  # user-defined CB substitution -- legitimately bare
            if name in CB_BUILTIN_SUBSTITUTIONS:
                continue  # CB built-in -- legitimately bare
            print(
                f"{filename}: step \"{step_id}\" arg {arg_idx} has a bare, unescaped "
                f"reference to ${{{name}}} -- Cloud Build's substitution scanner will "
                f"reject the whole build at submit time unless this is a recognized "
                f"built-in or `_`-prefixed. Escape with `$$` (e.g. `${{{name}}}` -> "
                f"`$${{{name}}}`) if this is meant to be a runtime bash variable.",
                file=sys.stderr,
            )
            ok = False
    return ok


def check_cloudbuild_arg_length(yaml_content: str, filename: str = "cloudbuild.yaml") -> bool:
    """Fail if any steps[].args string entry exceeds Cloud Build's 10,000-char limit."""
    failed = False
    warned = False
    for step_id, arg_idx, arg_text in _iter_cloudbuild_step_args(yaml_content, filename):
        length = len(arg_text)
        if length > FAIL_THRESHOLD:
            print(
                f"{filename}: step \"{step_id}\" arg {arg_idx} is {length} chars, "
                f"exceeds Cloud Build's {FAIL_THRESHOLD}-char limit -- extract to a "
                f"scripts/*.sh file (see scripts/mint-gh-app-token.sh for the pattern)",
                file=sys.stderr,
            )
            failed = True
        elif length > WARN_THRESHOLD:
            print(
                f"{filename}: WARN step \"{step_id}\" arg {arg_idx} is {length} chars, "
                f"approaching Cloud Build's {FAIL_THRESHOLD}-char limit",
                file=sys.stderr,
            )
            warned = True
    if warned and not failed:
        print(f"{filename}: one or more steps are approaching the arg-length limit (see WARN lines above).")
    return not failed


# A curl short-option cluster that carries globoff, e.g. `-sSg`, `-gsS`,
# `-sSXg`. Curl's short flags used in this repo are all single letters with
# no argument of their own (s, S, g, X), so a token starting with one `-`
# whose letters include `g` is globoff. `--globoff` is the long form.
_CURL_GLOBOFF_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*g[A-Za-z]*|--globoff)(?:\s|$)")
_BOT_TAINT_MARKER = "[bot]"


def check_curl_url_globbing(script_text: str, filename: str = "script.sh") -> bool:
    """Fail if a `curl` call has a bracket-bearing URL (literal or via a tainted var) without -g."""
    logical_lines = _join_continuations(script_text)

    tainted_vars = set()
    assign_re = re.compile(r"\b([A-Z_][A-Z0-9_]*)=")
    for line, _ in logical_lines:
        if line.lstrip().startswith("#"):
            continue  # comment lines (e.g. explaining the -g fix) are not code
        if _BOT_TAINT_MARKER not in line:
            continue
        m = assign_re.search(line)
        if m:
            tainted_vars.add(m.group(1))

    ok = True
    for line, line_no in logical_lines:
        if line.lstrip().startswith("#"):
            continue  # a comment mentioning "curl" is not an actual invocation
        if not re.search(r"\bcurl\b", line):
            continue
        if _CURL_GLOBOFF_RE.search(line):
            continue  # already globoff-safe

        # Only the curl invocation's OWN arguments matter for URL-globbing --
        # text after the first unquoted `|` is a downstream command (e.g. a
        # `python3 -c '...'` parsing the response) and routinely contains
        # unrelated `[`/`]` characters (dict-subscript syntax) that must not
        # taint this curl call. Every curl invocation in this repo's scripts
        # places all of its own args before the first pipe.
        pipe_idx = line.find("|")
        curl_segment = line[:pipe_idx] if pipe_idx != -1 else line

        has_literal_bracket = "[" in curl_segment or "]" in curl_segment
        referenced_tainted = [
            v for v in tainted_vars if f"${v}" in curl_segment or f"${{{v}}}" in curl_segment
        ]

        if has_literal_bracket or referenced_tainted:
            reason = (
                "a literal `[`/`]` in the curl invocation"
                if has_literal_bracket
                else f"a reference to {', '.join(sorted(referenced_tainted))} (assigned a "
                f"'{_BOT_TAINT_MARKER}'-bearing value)"
            )
            print(
                f"{filename}:{line_no}: curl call has {reason} but no -g/--globoff -- "
                f"curl's URL-globbing parser will read the bracket as a glob range and "
                f"refuse the request (\"bad range in URL\"). Add -g (or --globoff).",
                file=sys.stderr,
            )
            ok = False
    return ok


def _extract_cloudbuild_scripts(yaml_content: str, filename: str):
    """Yield (pseudo-filename, arg_text) for every steps[].args string, for check 3's reuse."""
    for step_id, arg_idx, arg_text in _iter_cloudbuild_step_args(yaml_content, filename):
        yield f"{filename} step \"{step_id}\" arg {arg_idx}", arg_text


def lint_repo(root: Path = ROOT) -> bool:
    ok = True

    cloudbuild_files = sorted(root.glob("cloudbuild*.yaml"))
    if not cloudbuild_files:
        print(f"No cloudbuild*.yaml files found under {root}.", file=sys.stderr)
        return False

    for path in cloudbuild_files:
        content = path.read_text(encoding="utf-8")
        if not check_bare_substitution_refs(content, filename=path.name):
            ok = False
        if not check_cloudbuild_arg_length(content, filename=path.name):
            ok = False
        for pseudo_name, arg_text in _extract_cloudbuild_scripts(content, path.name):
            if not check_curl_url_globbing(arg_text, filename=pseudo_name):
                ok = False

    for path in sorted((root / "scripts").glob("*.sh")):
        content = path.read_text(encoding="utf-8")
        if not check_curl_url_globbing(content, filename=f"scripts/{path.name}"):
            ok = False

    return ok


def self_test() -> int:
    """Hermetic self-test: no cluster/network required."""

    # --- check 1: bare substitution refs ---
    ok_yaml = """
steps:
  - id: uses-builtin-and-user-sub
    args:
      - -c
      - |
        BRANCH="auto/refresh-$BUILD_ID"
        echo "${_MAX_NODES}"
        echo "$${GH_APP_ID}"
"""
    assert check_bare_substitution_refs(ok_yaml, "self-test") is True, "built-in/_-prefixed/escaped refs should pass"

    bad_yaml = """
steps:
  - id: bare-local
    args:
      - -c
      - |
        echo "$GH_APP_ID"
"""
    assert check_bare_substitution_refs(bad_yaml, "self-test") is False, "bare non-built-in ref should fail"

    bad_curly_yaml = """
steps:
  - id: bare-local-curly
    args:
      - -c
      - echo "${GH_APP_ID}"
"""
    assert check_bare_substitution_refs(bad_curly_yaml, "self-test") is False, "bare curly non-built-in ref should fail"

    # A top-level YAML comment mentioning an uppercase name must never be
    # scanned -- it lives outside every steps[].args string, exactly like
    # the real $MAX_NODES mentions in cloudbuild-refresh-gke-sandbox.yaml's
    # substitutions-block comment.
    comment_only_yaml = """
# mentions $SOME_UNESCAPED_NAME in a top-level comment, not inside any step
steps:
  - id: clean-step
    args:
      - -c
      - echo hello
"""
    assert check_bare_substitution_refs(comment_only_yaml, "self-test") is True, "top-level comment refs must not be scanned"

    # --- check 2: arg length (ported from the private repo's lint) ---
    warn_yaml = "steps:\n  - id: warn-step\n    args:\n      - -c\n      - |\n        " + ("x" * 8500)
    assert check_cloudbuild_arg_length(warn_yaml, "self-test") is True, "WARN-only should still pass"

    fail_yaml = "steps:\n  - id: too-long-step\n    args:\n      - -c\n      - |\n        " + ("x" * 10500)
    assert check_cloudbuild_arg_length(fail_yaml, "self-test") is False, "over-limit arg should fail"

    # --- check 3: curl url globbing ---
    clean_curl = 'curl -sS -H "Authorization: Bearer ${GITHUB_TOKEN}" "https://api.github.com/repos/foo/bar"\n'
    assert check_curl_url_globbing(clean_curl, "self-test") is True, "no-bracket curl should pass"

    literal_bracket_no_g = 'curl -sS "https://api.github.com/users/[bot]"\n'
    assert check_curl_url_globbing(literal_bracket_no_g, "self-test") is False, "literal bracket without -g should fail"

    literal_bracket_with_g = 'curl -sSg "https://api.github.com/users/[bot]"\n'
    assert check_curl_url_globbing(literal_bracket_with_g, "self-test") is True, "literal bracket WITH -g should pass"

    literal_bracket_globoff_long = 'curl -sS --globoff "https://api.github.com/users/[bot]"\n'
    assert check_curl_url_globbing(literal_bracket_globoff_long, "self-test") is True, "--globoff long form should pass"

    tainted_var_no_g = (
        'BOT_LOGIN="$(curl -sS https://api.github.com/app '
        '| python3 -c \'import json,sys; print(json.load(sys.stdin)["slug"] + "[bot]")\')"\n'
        'BOT_USER_ID="$(curl -sS -H "Accept: application/vnd.github+json" '
        '"https://api.github.com/users/${BOT_LOGIN}" | python3 -c \'import json,sys; '
        'print(json.load(sys.stdin)["id"])\')"\n'
    )
    assert check_curl_url_globbing(tainted_var_no_g, "self-test") is False, "curl on a [bot]-tainted var without -g should fail"

    tainted_var_with_g = tainted_var_no_g.replace('curl -sS -H "Accept', 'curl -sSg -H "Accept')
    assert check_curl_url_globbing(tainted_var_with_g, "self-test") is True, "curl on a [bot]-tainted var WITH -g should pass"

    # The false-positive guard this check exists to preserve: dict-subscript
    # brackets in an UNRELATED python one-liner (no "[bot]" substring) must
    # never taint the assigned variable, even though the assignment line
    # itself contains literal `[`/`]` characters.
    dict_subscript_no_taint = (
        'install_id=$(curl -sS https://api.github.com/app/installations '
        '| python3 -c \'import json,sys; d=json.load(sys.stdin); '
        'print(next(i["id"] for i in d if i["account"]["login"]==sys.argv[1]))\' "$GH_APP_ACCOUNT")\n'
        'GITHUB_TOKEN=$(curl -sS -X POST '
        '"https://api.github.com/app/installations/$install_id/access_tokens" '
        '| python3 -c \'import json,sys; print(json.load(sys.stdin)["token"])\')\n'
        'curl -sS -H "Authorization: Bearer ${GITHUB_TOKEN}" "https://api.github.com/repos/foo/bar"\n'
    )
    assert check_curl_url_globbing(dict_subscript_no_taint, "self-test") is True, (
        "dict-subscript brackets in an unrelated python one-liner must not taint "
        "install_id/GITHUB_TOKEN or flag the final clean curl call"
    )

    # Multi-line (backslash-continued) curl + assignment, mirroring the real
    # mint-gh-app-token.sh shape, must be joined into one logical line before
    # either the taint-scan or the curl-scan runs.
    multiline_real_shape = (
        'BOT_LOGIN="$(curl -sS -H "Authorization: Bearer $app_jwt" -H "Accept: application/vnd.github+json" \\\n'
        '    https://api.github.com/app | python3 -c \'import json,sys; print(json.load(sys.stdin)["slug"] + "[bot]")\')"\n'
        'BOT_USER_ID="$(curl -sSg -H "Accept: application/vnd.github+json" "https://api.github.com/users/${BOT_LOGIN}" \\\n'
        '    | python3 -c \'import json,sys; print(json.load(sys.stdin)["id"])\')"\n'
    )
    assert check_curl_url_globbing(multiline_real_shape, "self-test") is True, (
        "the real (already-fixed) mint-gh-app-token.sh shape should lint clean"
    )

    # A comment line that mentions "curl" and "[bot]" while explaining the fix
    # (the real mint-gh-app-token.sh:65-67 shape) must never be scanned as an
    # actual invocation -- `\bcurl\b` matches inside "curl's", and the comment
    # prose "-g/--globoff:" doesn't match the globoff-flag regex, so without a
    # comment-skip this is a false positive on real, already-fixed code.
    comment_mentioning_curl = (
        '  # -g/--globoff: BOT_LOGIN is "<slug>[bot]" -- curl\'s URL-globbing parser\n'
        '  # reads a literal `[...]` in the URL as a glob range ("bad range in URL")\n'
        '  BOT_USER_ID="$(curl -sSg -H "Accept: application/vnd.github+json" "https://api.github.com/users/${BOT_LOGIN}" \\\n'
        '    | python3 -c \'import json,sys; print(json.load(sys.stdin)["id"])\')"\n'
    )
    assert check_curl_url_globbing(comment_mentioning_curl, "self-test") is True, (
        "a comment mentioning curl/[bot] while explaining the fix must not be scanned as an invocation"
    )

    print("PASS: self-test (all substitution/arg-length/curl-globbing cases classified correctly)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight lint for the refresh-pipeline cloudbuild configs (hb#782).")
    parser.add_argument("--self-test", action="store_true", help="Run hermetic self-test and exit.")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    ok = lint_repo(ROOT)
    if not ok:
        print("\nFAIL: refresh-pipeline lint found issues (see above).", file=sys.stderr)
        return 1

    print("PASS: refresh-pipeline lint clean (substitution-escaping, arg-length, curl-globbing).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
