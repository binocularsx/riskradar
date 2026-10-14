"""D75: BVN identity and the industry watch-list.

What is pinned: a BVN is learned at the boundary (from the payload or the
core), tokenised and never stored raw; a flag placed on a known BVN covers every
customer record with that BVN; every flag step reaches the outbox in the same
transaction; the outbox waits visibly when nothing is connected; and flags from
other institutions are tokenised on arrival.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.clocks import watchlist
from riskradar.config import settings
from riskradar.identity import adapters, boundary, industry_sync
from riskradar.security.tokens import bvn_token, subject_token

from conftest import login

BVN = "22123456789"


def fresh_bvn() -> str:
    """A BVN no other test or earlier run has used, so counts are this test's own."""
    return "22" + str(uuid.uuid4().int)[:9]


@pytest.fixture
def use(monkeypatch):
    """Choose adapters for one test."""
    def choose(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        adapters._cache.clear()
    yield choose
    adapters._cache.clear()


# ---------------------------------------------------------------- adapters


def test_bvn_tokens_have_their_own_namespace():
    assert bvn_token(BVN) != subject_token(BVN)
    assert bvn_token(BVN) == bvn_token(BVN)


def test_fixture_core_reloads_when_the_file_grows(tmp_path):
    core = tmp_path / "core.csv"
    core.write_text("customer_id,bvn,nin\nCIF1,22000000001,\n", encoding="utf-8")
    resolver = adapters.FixtureCoreResolver(core)
    assert resolver.resolve("CIF1").bvn == "22000000001"
    assert resolver.resolve("CIF2") is None
    with core.open("a", encoding="utf-8") as fh:
        fh.write("CIF2,22000000002,12345678901\n")
    assert resolver.resolve("CIF2").nin == "12345678901"
    assert adapters.FixtureCoreResolver(tmp_path / "missing.csv").resolve("CIF1") is None


def test_a_format_check_never_calls_a_bvn_valid():
    assert adapters.FormatRegistry().verify(BVN).status == "UNVERIFIED"
    assert adapters.FormatRegistry().verify("11111111111").status == "INVALID"
    assert adapters.NibssRegistry().verify(BVN).status == "UNAVAILABLE"


def test_real_connections_wait_rather_than_fail():
    with pytest.raises(adapters.NotConnected):
        adapters.FinacleCoreResolver().resolve("CIF1")
    with pytest.raises(adapters.NotConnected):
        adapters.NibssIndustryConnector().publish({"flag_id": 1, "operation": "PLACE"})


# ---------------------------------------------------------------- boundary


def test_stamp_from_payload_then_from_memory(conn, use):
    use(RISKRADAR_CORE_RESOLVER="none")
    subject = f"sub_test_{uuid.uuid4().hex[:12]}"
    token = boundary.stamp(conn, customer_id="CIF-x", subject_token=subject, bvn=BVN, nin=None)
    assert token == bvn_token(BVN)
    row = conn.execute("SELECT * FROM customer_identities WHERE subject_token = %s", (subject,)).fetchone()
    assert (row["source"], row["verification_status"]) == ("BOUNDARY", "UNVERIFIED")
    # Next payment carries no BVN: the stored token is used, no core call.
    assert boundary.stamp(conn, customer_id="CIF-x", subject_token=subject, bvn=None, nin=None) == token


def test_stamp_asks_the_core_and_survives_no_core(conn, use, tmp_path, monkeypatch):
    core = tmp_path / "core.csv"
    core.write_text(f"customer_id,bvn,nin\nCIF-core,{BVN},\n", encoding="utf-8")
    monkeypatch.setenv("RISKRADAR_CORE_FIXTURE", str(core))
    use(RISKRADAR_CORE_RESOLVER="fixture")
    subject = f"sub_test_{uuid.uuid4().hex[:12]}"
    assert boundary.stamp(conn, customer_id="CIF-core", subject_token=subject, bvn=None, nin=None) == bvn_token(BVN)
    assert conn.execute("SELECT source FROM customer_identities WHERE subject_token = %s",
                        (subject,)).fetchone()["source"] == "CORE_LOOKUP"

    use(RISKRADAR_CORE_RESOLVER="finacle")
    assert boundary.stamp(conn, customer_id="CIF-other", subject_token=f"sub_{uuid.uuid4().hex}",
                          bvn=None, nin=None) is None


def test_a_bvn_in_a_payment_is_stored_only_as_a_token(client, api_headers, sample_transaction):
    bvn = fresh_bvn()
    payload = sample_transaction(bvn=bvn, nin="12345678901")
    tx_id = client.post("/v1/transactions", json=payload, headers=api_headers).json()["transaction_id"]
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        try:
            tx = c.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,)).fetchone()
            ev = c.execute("SELECT * FROM events WHERE transaction_id = %s", (tx_id,)).fetchone()
            ident = c.execute("SELECT * FROM customer_identities WHERE subject_token = %s",
                              (tx["subject_token"],)).fetchone()
            assert tx["bvn_token"] == ev["bvn_token"] == ident["bvn_token"] == bvn_token(bvn)
            blob = " ".join(str(v) for row in (tx, ev, ident) for v in row.values())
            assert bvn not in blob and "12345678901" not in blob
        finally:
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
            c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
            c.execute("DELETE FROM customer_identities WHERE subject_token = %s", (tx["subject_token"],))
            c.commit()


def test_a_malformed_bvn_is_refused(client, api_headers, sample_transaction):
    r = client.post("/v1/transactions", json=sample_transaction(bvn="2212345"), headers=api_headers)
    assert r.status_code == 422


# ------------------------------------------------------ BVN-level flags


def _case(conn, subject):
    return conn.execute(
        "INSERT INTO cases (subject_token, risk_level, correlation_expires_at, state) "
        "VALUES (%s, 'HIGH', now() + interval '1 day', 'UNDER_REVIEW') RETURNING *", (subject,)
    ).fetchone()


def _identity(conn, subject, bvn):
    conn.execute("INSERT INTO customer_identities (subject_token, bvn_token, source) VALUES (%s, %s, 'BOUNDARY')",
                 (subject, bvn_token(bvn)))


@pytest.fixture
def analyst_id(conn):
    return conn.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]


def test_a_flag_on_a_bvn_covers_every_record_with_it(conn, analyst_id):
    first, second = f"sub_a_{uuid.uuid4().hex[:8]}", f"sub_b_{uuid.uuid4().hex[:8]}"
    bvn = fresh_bvn()
    _identity(conn, first, bvn)
    _identity(conn, second, bvn)
    case = _case(conn, first)

    flag = watchlist.place(conn, case=case, user_id=analyst_id, reason="one person, two records", hours=24)
    assert flag["scope"] == "BVN" and flag["records_covered"] == 2
    assert watchlist.active_subjects(conn, [first, second]) == {first, second}

    with pytest.raises(watchlist.WatchlistError) as again:
        watchlist.place(conn, case=_case(conn, second), user_id=analyst_id, reason="same BVN again", hours=6)
    assert again.value.status == 409


def test_every_flag_step_reaches_the_outbox(conn, analyst_id):
    known, unknown = f"sub_k_{uuid.uuid4().hex[:8]}", f"sub_u_{uuid.uuid4().hex[:8]}"
    bvn = fresh_bvn()
    _identity(conn, known, bvn)
    shared = watchlist.place(conn, case=_case(conn, known), user_id=analyst_id, reason="shareable flag", hours=1)
    local = watchlist.place(conn, case=_case(conn, unknown), user_id=analyst_id, reason="customer-level flag", hours=1)
    watchlist.lift(conn, flag_id=shared["id"], user_id=analyst_id, note="cleared")

    rows = conn.execute("SELECT flag_id, operation, status, payload FROM watchlist_outbox WHERE flag_id = ANY(%s) "
                        "ORDER BY id", ([shared["id"], local["id"]],)).fetchall()
    assert [(r["flag_id"], r["operation"], r["status"]) for r in rows] == [
        (shared["id"], "PLACE", "PENDING"), (local["id"], "PLACE", "NOT_SHAREABLE"), (shared["id"], "LIFT", "PENDING")]
    assert rows[0]["payload"]["bvn_token"] == bvn_token(bvn)
    assert bvn not in str(rows[0]["payload"])


def test_dispatch_waits_sends_and_backs_off(conn, analyst_id):
    subject = f"sub_d_{uuid.uuid4().hex[:8]}"
    _identity(conn, subject, fresh_bvn())
    flag = watchlist.place(conn, case=_case(conn, subject), user_id=analyst_id, reason="to be dispatched", hours=1)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    # The suite shares the demo database. Messages already waiting there would
    # fill dispatch's batch and leave this flag's message unpicked, so set them
    # aside for this test; the rolled-back transaction puts them back.
    conn.execute("UPDATE watchlist_outbox SET next_attempt_at = 'infinity' "
                 "WHERE status = 'PENDING' AND flag_id <> %s", (flag["id"],))

    def row():
        return conn.execute("SELECT * FROM watchlist_outbox WHERE flag_id = %s", (flag["id"],)).fetchone()

    industry_sync.dispatch(conn, connector=adapters.NoIndustryConnector(), now=now)
    waiting = row()
    assert waiting["status"] == "PENDING" and "not configured" in waiting["last_error"] or \
        "no industry" in waiting["last_error"]
    assert waiting["next_attempt_at"] > now

    class Broken:
        name = "broken"
        def publish(self, message):
            raise TimeoutError("hub timed out")
    later = waiting["next_attempt_at"] + timedelta(seconds=1)
    industry_sync.dispatch(conn, connector=Broken(), now=later)
    assert row()["attempts"] == 2 and row()["status"] == "PENDING"

    industry_sync.dispatch(conn, connector=adapters.LoopbackIndustryConnector(), now=row()["next_attempt_at"])
    sent = row()
    assert sent["status"] == "SENT" and sent["external_ref"] == f"LOOPBACK-{flag['id']}-PLACE"


# ------------------------------------------------------------ inbound


def test_flags_from_other_institutions_are_tokenised_and_shown(conn, use):
    use(RISKRADAR_INSTITUTION_CODE="OUR-BANK")
    subject = f"sub_i_{uuid.uuid4().hex[:8]}"
    _identity(conn, subject, "22999999999")
    now = datetime.now(timezone.utc)
    entry = {"external_ref": f"EXT-{uuid.uuid4().hex[:8]}", "bvn": "22999999999", "institution_code": "SIMBANK-044",
             "reason_code": "SUSPECTED_FRAUD", "flagged_at": now, "expires_at": now + timedelta(hours=24)}
    own = dict(entry, external_ref=f"EXT-{uuid.uuid4().hex[:8]}", institution_code="OUR-BANK")

    assert industry_sync.receive(conn, [entry, own], source="INBOUND_API") == {"stored": 1, "updated": 0, "ignored_own": 1}
    assert industry_sync.receive(conn, [dict(entry, lifted_at=now)], source="INBOUND_API")["updated"] == 1
    stored = conn.execute("SELECT * FROM industry_watchlist WHERE external_ref = %s", (entry["external_ref"],)).fetchone()
    assert stored["bvn_token"] == bvn_token("22999999999") and stored["lifted_at"] is not None
    assert "22999999999" not in " ".join(str(v) for v in stored.values())
    assert watchlist.industry_flags(conn, [subject], active_only=False)[subject][0]["institution_code"] == "SIMBANK-044"
    assert not watchlist.industry_flags(conn, [subject])


def test_inbound_needs_an_api_key_and_integrations_are_visible(client):
    assert client.post("/v1/industry-watchlist/inbound", json={"entries": []}).status_code in (401, 422)
    login(client, "lead@riskradar.local", "OpsLead#2026")
    body = client.get("/v1/admin/integrations").json()
    assert {"core_resolver", "identity_registry", "industry_connector"} <= set(body["adapters"])
    assert "items" in client.get("/v1/industry-watchlist").json()
