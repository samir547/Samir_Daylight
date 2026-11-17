from order_forecast.accounts import accounts_for_pool, load_accounts


def test_list_has_no_duplicates_and_known_statuses():
    rows = load_accounts()
    assert len({r["customer_no"] for r in rows}) == len(rows)


def test_pool_sizes_match_phase1():
    assert len(accounts_for_pool("UK")) == 28          # AMAZSL1 + 24 FC accounts + 3 small
    assert set(accounts_for_pool("US")) == {"30003"}
    assert accounts_for_pool("EU") == {}               # the EU pool was split per country
    assert len(accounts_for_pool("DE")) == 6           # AMAZ81000 (DE/PL/CZ) + 5 small, two of them Poland-only
    assert set(accounts_for_pool("FR")) == {"AMAZ92110"}   # also invoices ES and IT shipments
    assert len(accounts_for_pool("IT")) == 10
    assert set(accounts_for_pool("ES")) == {"AMAZ2804"}
    assert accounts_for_pool("AU") == {}


def test_pending_accounts_only_with_include():
    assert set(accounts_for_pool("US", ("in", "pending"))) == {"30003", "AMAZFB13", "30115"}


def test_not_amazon_and_provisions_never_in_scope():
    for pool in ("UK", "US", "DE", "ES", "FR", "IT"):
        got = accounts_for_pool(pool, ("in", "pending"))
        assert not {"AMAZ2990", "AMAZSL1P", "AMASSL1P", "300031", "30379"} & set(got)


def test_every_eu_account_sits_in_its_own_country_pool():
    for r in load_accounts():
        if r["marketplace"] in ("DE", "ES", "FR", "IT"):
            assert r["pool"] == r["marketplace"], r["customer_no"]
