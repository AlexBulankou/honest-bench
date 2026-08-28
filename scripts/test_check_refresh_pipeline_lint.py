#!/usr/bin/env python3
"""Tests for the refresh-pipeline preflight lint (hb#782).

Guards the 3 Cloud Build validation classes hit during the 2026-08-27
fire-5 arc (hb#775-#779): bare-substitution escaping, per-step arg-length,
and curl URL-globbing. Also locks in the two false-positive fixes found
while building the lint (pipe-segment scoping, comment-line exclusion).

Dependency-free: `python3 scripts/test_check_refresh_pipeline_lint.py` (exit 0 = pass).
Auto-discovered by the offline unit-tests gate (find . -name 'test_*.py').
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_refresh_pipeline_lint as g  # noqa: E402


def main():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    # --- check_bare_substitution_refs ---------------------------------------

    clean_yaml = """
steps:
- id: safe
  args: ["bash", "-c", "echo $$SAFE_VAR and ${PROJECT_ID} and $_USER_SUB"]
"""
    expect(g.check_bare_substitution_refs(clean_yaml, "t.yaml") is True,
           "escaped/builtin/underscore-prefixed refs are clean")

    bare_yaml = """
steps:
- id: unsafe
  args: ["bash", "-c", "echo ${GH_APP_ID}"]
"""
    expect(g.check_bare_substitution_refs(bare_yaml, "t.yaml") is False,
           "a bare, unescaped uppercase ref fails")

    # hb#784: bare-substitution scope extends to env/dir, not just args.
    bare_env_yaml = """
steps:
- id: unsafe-env
  env: ["TOKEN=$GH_APP_ID"]
  args: ["bash", "-c", "echo hi"]
"""
    expect(g.check_bare_substitution_refs(bare_env_yaml, "t.yaml") is False,
           "a bare, unescaped ref in an env value fails")

    clean_env_yaml = """
steps:
- id: safe-env
  env: ["GH_APP_ID=$${GH_APP_ID}", "BUILD_TAG=$BUILD_ID"]
  args: ["bash", "-c", "echo hi"]
"""
    expect(g.check_bare_substitution_refs(clean_env_yaml, "t.yaml") is True,
           "an escaped env value passes, and the env KEY half is never scanned "
           "as a reference even when it looks like one")

    bare_dir_yaml = """
steps:
- id: unsafe-dir
  dir: "$GH_APP_ID/sub"
  args: ["bash", "-c", "echo hi"]
"""
    expect(g.check_bare_substitution_refs(bare_dir_yaml, "t.yaml") is False,
           "a bare, unescaped ref in dir fails")

    clean_dir_yaml = """
steps:
- id: safe-dir
  dir: "$BUILD_ID/sub"
  args: ["bash", "-c", "echo hi"]
"""
    expect(g.check_bare_substitution_refs(clean_dir_yaml, "t.yaml") is True,
           "a built-in ref in dir passes")

    # --- check_cloudbuild_arg_length -----------------------------------------

    short_yaml = """
steps:
- id: ok
  args: ["bash", "-c", "%s"]
""" % ("x" * 100)
    expect(g.check_cloudbuild_arg_length(short_yaml, "t.yaml") is True,
           "a short arg is clean")

    over_yaml = """
steps:
- id: too-long
  args: ["bash", "-c", "%s"]
""" % ("x" * 10_500)
    expect(g.check_cloudbuild_arg_length(over_yaml, "t.yaml") is False,
           "an arg over the 10k FAIL threshold fails")

    warn_yaml = """
steps:
- id: warn
  args: ["bash", "-c", "%s"]
""" % ("x" * 8_500)
    expect(g.check_cloudbuild_arg_length(warn_yaml, "t.yaml") is True,
           "an arg between the WARN and FAIL thresholds still passes (warn-only)")

    # --- check_curl_url_globbing ---------------------------------------------

    literal_bracket = 'curl -sS "https://api.github.com/search/issues?q=[test]"\n'
    expect(g.check_curl_url_globbing(literal_bracket, "t.sh") is False,
           "a literal bracket in a curl URL with no -g fails")

    globoff_ok = 'curl -sSg "https://api.github.com/search/issues?q=[test]"\n'
    expect(g.check_curl_url_globbing(globoff_ok, "t.sh") is True,
           "-g (short-flag globoff) makes a bracketed URL clean")

    globoff_long_ok = 'curl -sS --globoff "https://api.github.com/search/issues?q=[test]"\n'
    expect(g.check_curl_url_globbing(globoff_long_ok, "t.sh") is True,
           "--globoff (long-flag) makes a bracketed URL clean")

    tainted_var_no_g = (
        'BOT_LOGIN="$(echo "myapp[bot]")"\n'
        'curl -sS "https://api.github.com/users/${BOT_LOGIN}"\n'
    )
    expect(g.check_curl_url_globbing(tainted_var_no_g, "t.sh") is False,
           "a curl call referencing a '[bot]'-tainted var with no -g fails")

    tainted_var_with_g = (
        'BOT_LOGIN="$(curl -sS https://api.github.com/app '
        "| python3 -c 'import json,sys; print(json.load(sys.stdin)[\"slug\"] + \"[bot]\")')\"\n"
        'BOT_USER_ID="$(curl -sSg "https://api.github.com/users/${BOT_LOGIN}" '
        "| python3 -c 'import json,sys; print(json.load(sys.stdin)[\"id\"])')\"\n"
    )
    expect(g.check_curl_url_globbing(tainted_var_with_g, "t.sh") is True,
           "downstream python dict-subscript brackets after a pipe must not taint the "
           "curl call itself, and the second curl's own -g clears the tainted-var check")

    comment_mentioning_curl = (
        '  # -g/--globoff: BOT_LOGIN is "<slug>[bot]" -- curl\'s URL-globbing parser\n'
        '  # reads a literal `[...]` in the URL as a glob range ("bad range in URL")\n'
        '  BOT_USER_ID="$(curl -sSg -H "Accept: application/vnd.github+json" '
        '"https://api.github.com/users/${BOT_LOGIN}" \\\n'
        "    | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"id\"])')\"\n"
    )
    expect(g.check_curl_url_globbing(comment_mentioning_curl, "t.sh") is True,
           "a comment mentioning curl/[bot] while explaining the fix must not be "
           "scanned as an actual invocation")

    no_curl_at_all = 'echo "no network calls here"\n'
    expect(g.check_curl_url_globbing(no_curl_at_all, "t.sh") is True,
           "a script with no curl call is trivially clean")

    # --- module self-test (hermetic, exercises the full check set together) --

    expect(g.self_test() == 0, "the module's own bundled self-test passes")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("check_refresh_pipeline_lint: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
