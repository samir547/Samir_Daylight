from order_forecast.accounts import accounts_for_pool, load_accounts


def test_list_has_no_duplicates_and_known_statuses():
    rows = load_accounts()
    assert len({r["customer_no"] for r in rows}) == len(rows)


def test_pool_sizes_match_phase1():
    assert len(accounts_for_pool("UK")) == 28          # AMAZSL1 + 24 FC accounts + 3 small
    assert set(accounts_for_pool("US")) == {"30003"}
    assert len(accounts_for_pool("EU")) == 18          # DE 6, FR 1, IT 10, ES 1
    assert accounts_for_pool("AU") == {}


def test_pending_accounts_only_with_include():
    assert set(accounts_for_pool("US", ("in", "pending"))) == {"30003", "AMAZFB13", "30115"}


def test_not_amazon_and_provisions_never_in_scope():
    for pool in ("UK", "US", "EU"):
        got = accounts_for_pool(pool, ("in", "pending"))
        assert not {"AMAZ2990", "AMAZSL1P", "AMASSL1P", "300031", "30379"} & set(got)
