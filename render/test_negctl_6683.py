# Negative-control for #6683 branch-protection verification. DELETE — never merge.
# Deliberately fails so the required `hb-unit-tests` check reds; the PR should then
# show mergeStateStatus=BLOCKED, falsifying "the gate is wired but doesn't block".
def test_deliberate_fail_6683():
    assert False, "negative-control: confirms hb-unit-tests reds and blocks merge (#6683)"


if __name__ == "__main__":
    test_deliberate_fail_6683()
