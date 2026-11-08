"""Temporary intentional failure to verify the hosted CI dependency gate."""


def test_intentional_ci_gate_failure():
    assert False, "Intentional CI gate experiment; remove after verifying Docker is skipped"
