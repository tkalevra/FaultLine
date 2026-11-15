"""Pins for DOCUMENT VERSIONED SUPERSESSION (migration 328, docs-storage lane).

THE CONTRACT UNDER TEST (the apache-versions discipline, documents inheriting
the facts lane's superseded_at contract):
  * a stable document_family identity derived from (user_id, normalized
    source_ref-or-filename) — derivation documented in
    corpus_store.derive_document_family and mirrored byte-for-byte by the 328
    SQL backfill;
  * same bytes re-ingested = sha256 DEDUPE (no new version); different bytes =
    a new version row (version_seq), the previous live row superseded_at=now();
  * live-default retrieval: the list AND the passage lane serve ONLY
    unsuperseded versions ("what's the latest format for X"); archives stay
    reachable ("what did it say in March") via the return verb ?version=N;
  * THE RETURN VERB: GET /corpus/documents/{id}/original serves the ORIGINAL
    bytes byte-for-byte with the original content-type/filename headers,
    re-hashed on read — corruption = refusal (HTTP 500 + loud log), NEVER
    corrupted bytes;
  * DOC_VERSIONING OFF = byte-parity with 327 (no family logic, ingest exactly
    as today);
  * LINEAGE: pages supersede WITH their doc (document-level cascade) — pages
    of a superseded version stop serving on live asks. The fact-level cascade
    is FILED as the follow-up (see DEV/ or the lane report), not forced.

Hermetic legs run everywhere. DB legs boot a THROWAWAY database with the real
migration corpus (the r6pass pattern, tests/test_doc_ingest_snapshot_store.py).
"""

import base64
import os

import pytest

from src.ingest import corpus_store as cs


# ── helpers ──────────────────────────────────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _fam_row(rid, sha, seq, superseded):
    return {"id": rid, "content_sha256": sha, "version_seq": seq,
            "superseded_at": superseded}


# ── hermetic: the family-derivation rule ─────────────────────────────────────────────

def test_family_derivation_rule():
    """source_ref wins when non-empty, else original_filename; the key is
    trimmed + lowercased + backslash-normalized; the family is md5 over
    user_id + NUL + key; user scoping means two users never share a family."""
    a = cs.derive_document_family("u1", "configs/httpd.conf", "")
    assert a == cs.derive_document_family("u1", "Configs\\HTTPD.CONF", "")
    assert a == cs.derive_document_family("u1", "", "configs/httpd.conf")
    assert a != cs.derive_document_family("u2", "configs/httpd.conf", "")
    # source_ref takes PRECEDENCE over a different filename
    assert cs.derive_document_family("u1", "a.txt", "b.txt") == \
        cs.derive_document_family("u1", "a.txt", "zzz")
    # unnamed artefacts have NO stable identity — None, never a fabricated one
    assert cs.derive_document_family("u1", "  ", "") is None
    assert cs.derive_document_family("u1", "", None) is None


def test_family_derivation_matches_the_328_sql_backfill():
    """The migration's SQL backfill must derive the SAME family as the app —
    md5(user_id || chr(0) || lower-trim backslash-normalized key). Pinned
    against a hand-computed Postgres-shaped expression so a drift on either
    side fails HERE, not in production."""
    import hashlib
    key = "Configs\\Apache\\HTTPD.Conf".lower().replace("\\", "/")
    assert cs.derive_document_family("seat-1", key, "") == hashlib.md5(
        b"seat-1\x00" + key.encode()).hexdigest()


# ── hermetic: dedupe-vs-mint ─────────────────────────────────────────────────────────

def test_same_bytes_as_live_is_dedupe_no_new_version():
    live = _fam_row(7, "sha-A", 1, None)
    out = cs.resolve_version_action([live], "sha-A")
    assert out["action"] == "duplicate" and out["duplicate_of"] == 7


def test_changed_bytes_mint_next_version_and_supersede_live():
    live = _fam_row(7, "sha-A", 1, None)
    out = cs.resolve_version_action([live], "sha-B")
    assert out == {"action": "mint", "next_version_seq": 2, "supersede_id": 7}


def test_first_ingest_of_a_family_mints_version_1():
    out = cs.resolve_version_action([], "sha-A")
    assert out == {"action": "mint", "next_version_seq": 1, "supersede_id": None}


def test_reingesting_archived_bytes_mints_a_new_version():
    """A genuine ROLLBACK is a real event in the artefact's history: v1 bytes
    re-ingested while v2 (different bytes) is live MINTS v3 — never silently
    deduped to a dead row."""
    v1 = _fam_row(7, "sha-A", 1, "2026-03-01T00:00:00Z")
    v2 = _fam_row(9, "sha-B", 2, None)
    out = cs.resolve_version_action([v2, v1], "sha-A")
    assert out["action"] == "mint"
    assert out["next_version_seq"] == 3 and out["supersede_id"] == 9


# ── hermetic: the return verb's corruption-refusal shape ─────────────────────────────

def test_verify_snapshot_roundtrip_and_corruption_refusal():
    """Good row → the EXACT original bytes. Corrupted blob / bad codec / hash
    mismatch → SnapshotIntegrityError — corrupted bytes are NEVER returned."""
    raw = b"ansible-playbook site.yml --check \x00\xff" * 64
    good = cs.compress_snapshot(raw)
    sha = cs.content_sha256(raw)
    assert cs.verify_snapshot(good, "zlib", sha) == raw

    tampered = bytearray(good)
    tampered[len(tampered) // 2] ^= 0xFF
    with pytest.raises(cs.SnapshotIntegrityError):
        cs.verify_snapshot(bytes(tampered), "zlib", sha)     # zlib stream broken
    with pytest.raises(cs.SnapshotIntegrityError):
        cs.verify_snapshot(good, "zlib", cs.content_sha256(b"other bytes"))
    with pytest.raises(cs.SnapshotIntegrityError):
        cs.verify_snapshot(good, "bogus-codec", sha)          # mis-stamped codec
    # a VALID stream of DIFFERENT bytes also refuses (hash mismatch, not just
    # decode failure — the gate re-hashes, it does not trust decompression)
    with pytest.raises(cs.SnapshotIntegrityError):
        cs.verify_snapshot(cs.compress_snapshot(b"silent substitute"), "zlib", sha)


# ── hermetic: OFF byte-parity (the pure half) ────────────────────────────────────────

def test_off_parity_no_family_for_unnamed_and_legacy_shape():
    """Flag OFF = 327 behavior exactly: no family logic anywhere. The pure
    marker: build_snapshot output carries no versioning fields and an
    unnamed/legacy ingest path never consults resolve_version_action."""
    snap = cs.build_snapshot(user_id="u1", data=b"body\n", filename="f.txt")
    assert not hasattr(snap, "document_family")   # the 327 dataclass, unchanged
    assert snap.decompressed() == b"body\n"


# ── DB-gated legs: throwaway database + the real migration corpus ─────────────────────

_DSN = os.environ.get("POSTGRES_DSN", "")
_ver_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the versioning DB gate stays dark")


@pytest.fixture(scope="module")
def ver_db():
    """Boot ONE throwaway database with the real migration corpus (incl. 327 +
    328), mint ONE fresh tenant schema, hand back (dsn, uid, schema)."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set")
    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")
    from urllib.parse import urlsplit, urlunsplit
    import uuid
    import psycopg2
    from src.provisioning import boot_migrations as bm
    from src.provisioning.schema_manager import create_user_schema

    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), \
        "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_docver{uuid.uuid4().hex[:6]}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    dsn = urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))
    saved = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    try:
        boot = bm.run_boot_migrations(dsn, mig_dir, echo=False)
        assert boot.fatal is None and boot.ledger_active, (boot.fatal,
                                                           boot.ledger_error)
        uid = str(uuid.uuid4())
        schema, st = create_user_schema(uid, uid.replace("-", "_"))
        assert st == "ready", st
        yield {"dsn": dsn, "uid": uid, "schema": schema, "mig_dir": mig_dir}
    finally:
        if saved is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = saved
        drop = psycopg2.connect(_DSN)
        drop.autocommit = True
        try:
            with drop.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            drop.close()


def _ingest(M, ver_db, raw: bytes, name: str, monkeypatch, **kw):
    monkeypatch.setattr(M, "DOC_VERSIONING", True, raising=False)
    return M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=ver_db["uid"], data_b64=_b64(raw),
        original_filename=name, source_ref=name, **kw))


@_ver_db
def test_db_migration_328_idempotent_columns_and_fan(ver_db):
    """328 ran inside the boot corpus (fresh tenant schema born with the
    columns via the TEMPLATE — the same-commit rule): columns + the unique
    (family, version_seq) index exist, and re-running the file is a no-op."""
    import psycopg2
    conn = psycopg2.connect(ver_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {ver_db['schema']}")
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'corpus_documents' "
                "AND column_name IN "
                "('document_family', 'version_seq', 'superseded_at')",
                (ver_db["schema"],))
            assert {r[0] for r in cur.fetchall()} == {
                "document_family", "version_seq", "superseded_at"}
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s "
                "AND indexname = 'idx_corpus_documents_family_version'",
                (ver_db["schema"],))
            assert "UNIQUE" in cur.fetchone()[0]
        # Idempotency: execute the whole migration file AGAIN — no error, and
        # the fan covers every schema holding corpus_documents.
        conn.commit()
        conn.autocommit = True
        with open(os.path.join(ver_db["mig_dir"],
                               "328_corpus_document_versioning.sql")) as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {ver_db['schema']}")
            cur.execute("SELECT count(*) FROM corpus_documents")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


@_ver_db
def test_db_version_lifecycle_e2e_three_versions(ver_db, monkeypatch):
    """THE APACHE CONTRACT, end to end across 3 versions of one family:
    dedupe on same bytes; mint + supersede on change; live-default list;
    archives reachable; byte-return per version; pages supersede with doc."""
    import psycopg2
    import src.api.main as M
    monkeypatch.setenv("POSTGRES_DSN", ver_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)

    v1_bytes = b"Listen 80\nServerName old.example.com\n"
    v2_bytes = b"Listen 8080\nServerName new.example.com\n"
    v3_bytes = b"Listen 443\nServerName final.example.com\n"

    out1 = _ingest(M, ver_db, v1_bytes, "httpd.conf", monkeypatch)
    assert out1["status"] == "ready" and out1["version_seq"] == 1
    assert out1["duplicate"] is False
    fam = out1["document_family"]
    assert fam == cs.derive_document_family(ver_db["uid"], "httpd.conf", "")

    # Same bytes: sha256 DEDUPE — no new row, no new version.
    out1b = _ingest(M, ver_db, v1_bytes, "httpd.conf", monkeypatch)
    assert out1b["duplicate"] is True
    assert out1b["document_id"] == out1["document_id"]

    out2 = _ingest(M, ver_db, v2_bytes, "httpd.conf", monkeypatch)
    assert out2["version_seq"] == 2 and out2["duplicate"] is False
    assert out2["document_family"] == fam
    out3 = _ingest(M, ver_db, v3_bytes, "httpd.conf", monkeypatch)
    assert out3["version_seq"] == 3

    conn = psycopg2.connect(ver_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {ver_db['schema']}")
            cur.execute(
                "SELECT id, version_seq, superseded_at IS NULL, "
                "content_sha256 FROM corpus_documents "
                "WHERE document_family = %s ORDER BY version_seq", (fam,))
            rows = cur.fetchall()
            assert len(rows) == 3                       # dedupe left no 4th row
            assert [r[1] for r in rows] == [1, 2, 3]
            assert [r[2] for r in rows] == [False, False, True]  # only v3 live
    finally:
        conn.close()

    # LIST default = live-only per family ("the latest format for X").
    listing = M.corpus_documents_status(user_id=ver_db["uid"])
    docs = listing["documents"]
    assert [d["version_seq"] for d in docs] == [3]
    assert all(d["superseded_at"] is None for d in docs)
    # versions=all reaches the archives — the full history with stamps.
    everything = M.corpus_documents_status(user_id=ver_db["uid"],
                                           versions="all")
    assert sorted(d["version_seq"] for d in everything["documents"]) == [1, 2, 3]
    v1_doc = next(d for d in everything["documents"]
                  if d["version_seq"] == 1)
    assert v1_doc["superseded_at"] is not None

    # THE RETURN VERB: byte-for-byte per version — id directly AND ?version=N.
    r1 = M.corpus_document_original(out1["document_id"], user_id=ver_db["uid"])
    assert r1.body == v1_bytes
    r2 = M.corpus_document_original(out3["document_id"], version=2,
                                    user_id=ver_db["uid"])
    assert r2.body == v2_bytes
    assert r2.headers["content-type"].startswith("text/plain")
    assert "httpd.conf" in r2.headers["content-disposition"]
    assert r2.headers["etag"] == f'"{cs.content_sha256(v2_bytes)}"'
    # A missing version 404s honestly.
    with pytest.raises(Exception) as ei:
        M.corpus_document_original(out3["document_id"], version=99,
                                   user_id=ver_db["uid"])
    assert "404" in str(ei.value.status_code) or ei.value.status_code == 404

    # LINEAGE: pages supersede WITH their doc — a query whose ONLY corpus home
    # is the superseded v2 page text serves NOTHING on the live ask. (Token
    # choice matters: the OR-token mechanics match ANY surviving token, so the
    # probe rides a token unique to the superseded version — "8080".)
    hits = M._document_hits_for_query(ver_db["uid"], "which port is 8080")
    corpus_hits = [h for h in hits if h.get("source") == "corpus_pages"]
    assert corpus_hits == []
    # …and the LIVE version's page still serves (443 lives only in v3).
    hits_live = M._document_hits_for_query(ver_db["uid"], "which port is 443")
    assert any(h.get("source") == "corpus_pages"
               and "final.example.com" in h["chunk_text"] for h in hits_live)


@_ver_db
def test_db_return_verb_refuses_corruption(ver_db, monkeypatch):
    """THE CORRUPTION-REFUSAL SHAPE: a tampered snapshot re-hashes to the
    WRONG digest → HTTP 500 with the loud detail — corrupted bytes are never
    served, not even partially."""
    import psycopg2
    import src.api.main as M
    monkeypatch.setenv("POSTGRES_DSN", ver_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    raw = b"the playbook bytes\n"
    out = _ingest(M, ver_db, raw, "site.yml", monkeypatch)
    assert out["status"] == "ready"

    conn = psycopg2.connect(ver_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {ver_db['schema']}")
            cur.execute(
                "SELECT raw_snapshot FROM corpus_documents WHERE id = %s",
                (out["document_id"],))
            blob = bytearray(bytes(cur.fetchone()[0]))
            blob[len(blob) // 2] ^= 0x5A                    # rot the stream
            cur.execute(
                "UPDATE corpus_documents SET raw_snapshot = %s WHERE id = %s",
                (psycopg2.Binary(bytes(blob)), out["document_id"]))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception) as ei:
        M.corpus_document_original(out["document_id"], user_id=ver_db["uid"])
    assert ei.value.status_code == 500
    assert "integrity" in str(ei.value.detail).lower()


@_ver_db
def test_db_flag_off_is_byte_parity_with_327(ver_db, monkeypatch):
    """DOC_VERSIONING OFF: no family logic — the same file ingested twice =
    TWO rows (no dedupe), no superseded_at is ever set, the list shows BOTH
    rows with NO version columns, and the response shape is the 327 shape."""
    import psycopg2
    import src.api.main as M
    monkeypatch.setenv("POSTGRES_DSN", ver_db["dsn"])
    monkeypatch.setattr(M, "_INGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(M, "DOC_VERSIONING", False, raising=False)
    raw = b"parity probe body\n"

    out1 = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=ver_db["uid"], data_b64=_b64(raw),
        original_filename="parity.txt", source_ref="parity.txt"))
    out2 = M.corpus_documents_store(M.CorpusDocumentRequest(
        user_id=ver_db["uid"], data_b64=_b64(raw),
        original_filename="parity.txt", source_ref="parity.txt"))
    assert out1["document_id"] != out2["document_id"]     # no dedupe OFF
    assert "document_family" not in out1 and "duplicate" not in out1  # 327 shape

    conn = psycopg2.connect(ver_db["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {ver_db['schema']}")
            cur.execute(
                "SELECT document_family, version_seq, superseded_at "
                "FROM corpus_documents WHERE id IN (%s, %s)",
                (out1["document_id"], out2["document_id"]))
            for fam, seq, sup in cur.fetchall():
                assert fam is None and seq is None and sup is None
    finally:
        conn.close()

    listing = M.corpus_documents_status(user_id=ver_db["uid"])
    ids = {d["document_id"] for d in listing["documents"]}
    assert {out1["document_id"], out2["document_id"]} <= ids   # unfiltered
    assert all("version_seq" not in d for d in listing["documents"])
