from allpath_trade.store.accounts import ACCOUNTS, DEFAULT_ACCOUNT, is_valid_account


def test_accounts_are_paper_and_shadow():
    assert ACCOUNTS == ("paper", "shadow")


def test_default_account_is_paper():
    assert DEFAULT_ACCOUNT == "paper"


def test_is_valid_account():
    assert is_valid_account("paper") is True
    assert is_valid_account("shadow") is True
    assert is_valid_account("bogus") is False
    assert is_valid_account("") is False
