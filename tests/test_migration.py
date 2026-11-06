"""Schema migration: a pre-channel database (subscriptions.phone NOT NULL) is
rebuilt so phone becomes channel="sms", recipient=<phone>, keeping ids and the
alert_log/observations links.
"""

from sqlalchemy import create_engine, inspect, text

from aurora.db import init_db

OLD_SCHEMA = """
CREATE TABLE subscriptions (
    id INTEGER NOT NULL PRIMARY KEY,
    phone VARCHAR NOT NULL,
    address VARCHAR NOT NULL,
    lat FLOAT NOT NULL,
    lon FLOAT NOT NULL,
    threshold FLOAT,
    active BOOLEAN,
    created_at DATETIME,
    last_alerted_at DATETIME,
    elevation_m FLOAT,
    horizon_deg FLOAT,
    bortle FLOAT
);
CREATE INDEX ix_subscriptions_id ON subscriptions (id);
CREATE INDEX ix_subscriptions_phone ON subscriptions (phone);
CREATE TABLE alert_log (
    id INTEGER NOT NULL PRIMARY KEY,
    subscription_id INTEGER REFERENCES subscriptions (id),
    checked_at DATETIME,
    ovation_prob FLOAT, kp_index FLOAT, cloud_cover FLOAT, aod FLOAT,
    elevation_m FLOAT, horizon_deg FLOAT, bortle FLOAT, moon_illumination FLOAT,
    pwv_mm FLOAT, visibility_score FLOAT, alerted BOOLEAN, backfilled BOOLEAN
);
CREATE TABLE observations (
    id INTEGER NOT NULL PRIMARY KEY,
    created_at DATETIME,
    observed_at DATETIME NOT NULL,
    saw_aurora BOOLEAN NOT NULL,
    intensity INTEGER,
    source VARCHAR NOT NULL,
    note VARCHAR,
    phone VARCHAR,
    lat FLOAT,
    lon FLOAT,
    subscription_id INTEGER REFERENCES subscriptions (id),
    alert_log_id INTEGER REFERENCES alert_log (id)
);
INSERT INTO subscriptions (id, phone, address, lat, lon, threshold, active, created_at,
                           last_alerted_at, elevation_m, horizon_deg, bortle)
VALUES (3, '+15551234567', 'N-Utah', 41.7, -111.8, 30.0, 1, '2026-10-01 00:00:00.000000',
        '2026-10-02 06:00:00.000000', 1400.0, 2.0, 4.0),
       (5, '+15557654321', 'Fairbanks', 64.8, -147.7, 50.0, 0, '2026-10-01 00:00:00.000000',
        NULL, NULL, NULL, NULL);
INSERT INTO alert_log (id, subscription_id, checked_at, visibility_score, alerted, backfilled)
VALUES (10, 3, '2026-10-02 06:00:00.000000', 61.0, 1, 0);
INSERT INTO observations (id, observed_at, saw_aurora, source, phone, subscription_id, alert_log_id)
VALUES (20, '2026-10-02 06:30:00.000000', 1, 'sms_reply', '+15551234567', 3, 10);
"""


def _old_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        for stmt in OLD_SCHEMA.split(";"):
            if stmt.strip():
                conn.exec_driver_sql(stmt)
    return engine


def test_migrates_phone_to_sms_recipient(tmp_path):
    engine = _old_db(tmp_path)
    init_db(engine)

    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("subscriptions")}
    assert "phone" not in cols
    assert {"channel", "recipient"} <= set(cols)
    assert cols["recipient"]["nullable"] is False
    index_names = {i["name"] for i in insp.get_indexes("subscriptions")}
    assert {"ix_subscriptions_id", "ix_subscriptions_recipient"} <= index_names
    assert "subscriptions_new" not in insp.get_table_names()
    assert "pending_pairings" in insp.get_table_names()

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, channel, recipient, address, threshold, active, last_alerted_at, bortle "
            "FROM subscriptions ORDER BY id"
        )).all()
        assert rows[0][:6] == (3, "sms", "+15551234567", "N-Utah", 30.0, 1)
        assert rows[0][6].startswith("2026-10-02 06:00") and rows[0][7] == 4.0
        assert rows[1][:6] == (5, "sms", "+15557654321", "Fairbanks", 50.0, 0)
        # Links to the rebuilt table still resolve.
        assert conn.execute(text(
            "SELECT s.recipient FROM alert_log a JOIN subscriptions s ON s.id = a.subscription_id"
        )).scalar_one() == "+15551234567"
        assert conn.execute(text(
            "SELECT recipient, phone FROM observations WHERE id = 20"
        )).one() == ("+15551234567", "+15551234567")
        # Foreign-key clauses still name "subscriptions", not a temp table.
        fk = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'alert_log'")).scalar_one()
        assert "subscriptions_new" not in fk


def test_migration_is_idempotent_and_orm_works(tmp_path):
    engine = _old_db(tmp_path)
    init_db(engine)
    init_db(engine)  # second run is a no-op

    from sqlalchemy.orm import sessionmaker

    from aurora.db import Subscription

    db = sessionmaker(bind=engine)()
    sub = db.get(Subscription, 3)
    assert sub.channel == "sms" and sub.phone == "+15551234567"
    db.add(Subscription(channel="inreach", recipient="0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9",
                        address="Algonquin", lat=45.8, lon=-78.4))
    db.commit()
    added = db.query(Subscription).filter(Subscription.channel == "inreach").one()
    assert added.phone is None and added.id == 6
    db.close()


def test_fresh_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("subscriptions")}
    assert {"channel", "recipient"} <= cols and "phone" not in cols
