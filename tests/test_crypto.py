import sqlite3

import pytest

from app import config, crypto, datasources, history


def test_connection_password_encrypted_at_rest():
    history.add_connection("enc_test", "Enc", dialect="postgres", host="h", port=5432, database="d",
                           user="u", password="p@ss w0rd", db_schema="")
    try:
        raw = sqlite3.connect(config.APP_DB).execute(
            "SELECT password FROM connections WHERE id = 'enc_test'").fetchone()[0]
        assert raw.startswith(crypto.PREFIX) and "p@ss" not in raw
        assert history.get_connection("enc_test")["password"] == "p@ss w0rd"
        assert all("password" not in r for r in history.list_connections())
        assert datasources.exists("enc_test") and datasources.dialect("enc_test") == "postgres"
    finally:
        history.delete_connection("enc_test")


def test_legacy_plaintext_rows_are_migrated():
    conn = sqlite3.connect(config.APP_DB)
    conn.execute("INSERT INTO connections (id, name, dialect, host, port, database, user, password) "
                 "VALUES ('legacy', 'L', 'mysql', 'h', 3306, 'd', 'u', 'plain')")
    conn.commit()
    conn.close()
    try:
        assert history.get_connection("legacy")["password"] == "plain"  # readable before migration
        history.init()
        raw = sqlite3.connect(config.APP_DB).execute("SELECT password FROM connections WHERE id='legacy'").fetchone()[0]
        assert crypto.is_encrypted(raw) and history.get_connection("legacy")["password"] == "plain"
    finally:
        history.delete_connection("legacy")


def test_wrong_key_is_reported():
    token = crypto.encrypt("x")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(token[:-4] + "AAAA")
