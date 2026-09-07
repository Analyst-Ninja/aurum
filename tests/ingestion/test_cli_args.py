"""`--full_load` is an explicit `True|False`, not a flag.

It defaults to True, so an incremental run needs `-f False`; a typo has to be rejected
by the parser rather than quietly read as a truthy string and trigger a full re-pull.
"""

import pytest

from src.ingestion import cli


@pytest.mark.parametrize("value", ["True", "true", "  TRUE  ", "1", "yes"])
def test_the_documented_true_spellings_parse(value):
    assert cli.str_to_bool(value) is True


@pytest.mark.parametrize("value", ["False", "false", " FALSE ", "0", "no"])
def test_the_documented_false_spellings_parse(value):
    assert cli.str_to_bool(value) is False


@pytest.mark.parametrize("value", ["maybe", "", "T rue", "2"])
def test_anything_else_is_an_argument_error_not_a_silent_full_load(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="Expected True or False"):
        cli.str_to_bool(value)
