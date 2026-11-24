"""SQLite persistence.

Schema
------
original_scores     original table, inserted once per upload and never
                    modified afterwards (requirement: kept as-is).
corrected_scores    working table; starts as a copy of the original and is
                    edited one household at a time.  Each row keeps its own
                    corrected id and score; ``orig_id`` is the verified
                    pairing to an original household (NULL = unpaired) and
                    ``weight_mode`` records where its bootstrap weights come
                    from.
orig_rounds / corr_rounds
                    per-round weighted summaries (sum of weight, sum of
                    weighted score) for the two tables.  Analysis reads only
                    these summary tables plus the ``meta`` totals, never any
                    per-household detail.
meta                 scalar key/value bookkeeping (plus a schema marker).

Weights
-------
A paired corrected household reuses its original partner's per-round weights
(``household_weights(orig_id)``).  An unpaired corrected household uses a
fixed independent stream keyed by its corrected id
(``independent_household_weights``) — a different namespace from the original
table, so even an identical id does not share the original's stream.
``legacy_same_id`` only exists for rows migrated from the previous schema and
keeps the old same-id stream so the old database's result is unchanged.

Transactions
------------
Every correction / pairing recomputes only the affected households' 1000
round rows and commits the household details together with ``corr_rounds`` in
a single transaction, so a failure leaves the previous correction and the
previous summary in place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .bootstrap import (
    ROUNDS,
    household_weights,
    independent_household_weights,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS original_scores (
    household_id TEXT PRIMARY KEY,
    score        INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10)
);
CREATE TABLE IF NOT EXISTS corrected_scores (
    household_id TEXT PRIMARY KEY,
    score        INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
    orig_id      TEXT,
    weight_mode  TEXT NOT NULL DEFAULT 'independent'
                 CHECK (weight_mode IN ('paired', 'independent',
                                        'legacy_same_id'))
);
CREATE TABLE IF NOT EXISTS orig_rounds (
    round_index      INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
    weight_total     INTEGER NOT NULL,
    weighted_sum     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS corr_rounds (
    round_index      INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
    weight_total     INTEGER NOT NULL,
    weighted_sum     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

META_ORIG_SUM = "orig_score_sum"
META_ORIG_N = "orig_count"
META_CORR_SUM = "corr_score_sum"
META_CORR_N = "corr_count"
META_UPLOADED_AT = "uploaded_at"
META_PAIRING_SCHEMA = "pairing_schema_v1"

MODE_PAIRED = "paired"
MODE_INDEPENDENT = "independent"
MODE_LEGACY = "legacy_same_id"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_pairing_schema(conn: sqlite3.Connection) -> None:
    """Add and backfill pairing columns on a database from the old schema.

    Backfill never touches the summary tables: a pre-existing database keeps
    the result it already had.  Corrected rows with a same-id original map to
    that original; rows without one keep their old same-id weight stream via
    the legacy mode.
    """
    columns = _column_names(conn, "corrected_scores")
    if "orig_id" not in columns:
        conn.execute("ALTER TABLE corrected_scores ADD COLUMN orig_id TEXT")
    if "weight_mode" not in columns:
        conn.execute(
            "ALTER TABLE corrected_scores ADD COLUMN weight_mode TEXT "
            "NOT NULL DEFAULT 'independent'"
        )

    marker = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_PAIRING_SCHEMA,)
    ).fetchone()
    if marker is None:
        # Old rows used household_weights(household_id) for both tables.
        # Same-id originals are exactly the paired case; the rest keep the
        # old stream through the legacy mode.  A fresh database is empty, so
        # this is a no-op there.  Running it whenever the marker is missing
        # also repairs a migration interrupted before its marker was set.
        conn.execute(
            """
            UPDATE corrected_scores
               SET orig_id = household_id, weight_mode = 'paired'
             WHERE household_id IN (
                   SELECT household_id FROM original_scores
             )
            """
        )
        conn.execute(
            """
            UPDATE corrected_scores
               SET weight_mode = 'legacy_same_id'
             WHERE orig_id IS NULL
            """
        )
        _set_meta(conn, META_PAIRING_SCHEMA, 1)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_corrected_orig_id "
        "ON corrected_scores(orig_id) WHERE orig_id IS NOT NULL"
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_pairing_schema(conn)
    conn.commit()


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _effective_weights(corrected_id: str, orig_id: str | None,
                       weight_mode: str) -> list[int]:
    """The per-round weight stream a corrected household contributes with."""
    if orig_id is not None:
        return household_weights(orig_id)
    if weight_mode == MODE_LEGACY:
        return household_weights(corrected_id)
    return independent_household_weights(corrected_id)


def _add_contribution(conn: sqlite3.Connection, weights: list[int],
                      score: int) -> None:
    conn.executemany(
        "UPDATE corr_rounds SET "
        "weight_total = weight_total + ?, "
        "weighted_sum = weighted_sum + ? "
        "WHERE round_index = ?",
        [(w, w * score, r) for r, w in enumerate(weights)],
    )


def _subtract_contribution(conn: sqlite3.Connection, weights: list[int],
                           score: int) -> None:
    conn.executemany(
        "UPDATE corr_rounds SET "
        "weight_total = weight_total - ?, "
        "weighted_sum = weighted_sum - ? "
        "WHERE round_index = ?",
        [(w, w * score, r) for r, w in enumerate(weights)],
    )


def _insert_corrected_row(conn: sqlite3.Connection, household_id: str,
                          score: int, orig_id: str | None,
                          weight_mode: str) -> None:
    conn.execute(
        "INSERT INTO corrected_scores(household_id, score, orig_id, "
        "weight_mode) VALUES(?, ?, ?, ?)",
        (household_id, score, orig_id, weight_mode),
    )
    _add_contribution(
        conn, _effective_weights(household_id, orig_id, weight_mode), score
    )


def replace_original(conn: sqlite3.Connection, rows: list[tuple[str, int]],
                     uploaded_at: str) -> None:
    """Atomically replace the whole dataset.

    The original table, the corrected copy (restored to same-id pairing),
    both round summaries and the scalar meta values are rebuilt in one
    transaction.
    """
    rounds_zero = [(r, 0, 0) for r in range(ROUNDS)]
    with conn:  # COMMIT on success, ROLLBACK on any exception
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM original_scores")
        conn.execute("DELETE FROM orig_rounds")
        conn.execute("DELETE FROM corr_rounds")
        conn.executemany(
            "INSERT INTO original_scores(household_id, score) VALUES(?, ?)", rows
        )
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score, orig_id, "
            "weight_mode) VALUES(?, ?, ?, 'paired')",
            [(household_id, score, household_id) for household_id, score in rows],
        )
        conn.executemany(
            "INSERT INTO orig_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            rounds_zero,
        )
        conn.executemany(
            "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            rounds_zero,
        )
        total_score = sum(score for _, score in rows)
        _set_meta(conn, META_ORIG_SUM, total_score)
        _set_meta(conn, META_ORIG_N, len(rows))
        _set_meta(conn, META_CORR_SUM, total_score)
        _set_meta(conn, META_CORR_N, len(rows))
        _set_meta(conn, META_UPLOADED_AT, uploaded_at)
        _set_meta(conn, META_PAIRING_SCHEMA, 1)
        for household_id, score in rows:
            household_rounds = [
                (r, w, w * score)
                for r, w in enumerate(household_weights(household_id))
            ]
            conn.executemany(
                "INSERT INTO orig_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                household_rounds,
            )
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                household_rounds,
            )


def _corrected_row(conn: sqlite3.Connection, household_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT household_id, score, orig_id, weight_mode "
        "FROM corrected_scores WHERE household_id = ?",
        (household_id,),
    ).fetchone()


def apply_correction(conn: sqlite3.Connection, household_id: str,
                     action: str, new_score: int | None) -> None:
    """Apply one add / score-change / removal, atomically with its summary.

    Score changes use the household's current effective pairing.  Removing a
    corrected household frees its original partner.  Adding one auto-pairs
    only with an idle same-id original; otherwise it stays unpaired with
    independent weights.
    """
    with conn:  # read and write happen in one transaction
        existing = _corrected_row(conn, household_id)
        if action == "remove":
            if existing is None:
                raise ValueError(f"住户 {household_id} 不在更正表中，无法移除")
            old_score = existing["score"]
            old_weights = _effective_weights(
                household_id, existing["orig_id"], existing["weight_mode"]
            )
            _subtract_contribution(conn, old_weights, old_score)
            conn.execute(
                "DELETE FROM corrected_scores WHERE household_id = ?",
                (household_id,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) - ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (old_score,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) - 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
            return

        if action not in ("add", "update"):
            raise ValueError(f"未知操作: {action}")
        if new_score is None or not 0 <= new_score <= 10:
            raise ValueError("评分必须是 0～10 的整数")

        if existing is None:
            if action == "update":
                raise ValueError(
                    f"住户 {household_id} 不在更正表中，无法改分；如需新增请选择补录"
                )
            orig = conn.execute(
                "SELECT 1 FROM original_scores WHERE household_id = ?",
                (household_id,),
            ).fetchone()
            orig_id: str | None = None
            weight_mode = MODE_INDEPENDENT
            if orig is not None:
                busy = conn.execute(
                    "SELECT 1 FROM corrected_scores WHERE orig_id = ?",
                    (household_id,),
                ).fetchone()
                if busy is None:  # idle same-id original -> auto-pair
                    orig_id = household_id
                    weight_mode = MODE_PAIRED
            _insert_corrected_row(
                conn, household_id, new_score, orig_id, weight_mode
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (new_score,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
            return

        if action == "add":
            raise ValueError(
                f"住户 {household_id} 已在更正表中（评分 {existing['score']}），"
                "如需修改请选择改分"
            )
        old_score = existing["score"]
        if old_score == new_score:
            return  # nothing to do; previous result stays in place
        # Pairing is unchanged, so the effective weights stay the same.
        weights = _effective_weights(
            household_id, existing["orig_id"], existing["weight_mode"]
        )
        _subtract_contribution(conn, weights, old_score)
        _add_contribution(conn, weights, new_score)
        conn.execute(
            "UPDATE corrected_scores SET score = ? WHERE household_id = ?",
            (new_score, household_id),
        )
        conn.execute(
            f"UPDATE meta SET value = CAST(value AS INTEGER) + ? "
            f"WHERE key = '{META_CORR_SUM}'",
            (new_score - old_score,),
        )
        # count unchanged on score update


def apply_pairings(conn: sqlite3.Connection,
                   pairs: list[tuple[str, str]]) -> dict:
    """Atomically establish/replace one-to-one pairings.

    ``pairs`` is ``(original_id, corrected_id)``.  The whole submission is
    validated first: unknown ids or duplicates on the same side reject it
    without any change.

    A corrected household that loses its partner because of this submission
    is unpaired unless it is assigned a new partner in the same submission;
    this is what makes an in-place swap (A-X, B-Y -> A-Y, B-X) work.  Only
    the corrected households whose pairing actually changes are read and
    recomputed, and their detail rows commit together with ``corr_rounds``.
    """
    if not pairs:
        raise ValueError("请至少填写一组合法的原表号与更正表号")

    normalized: list[tuple[str, str]] = []
    seen_orig: set[str] = set()
    seen_corr: set[str] = set()
    for raw_orig, raw_corr in pairs:
        orig_id = raw_orig.strip()
        corr_id = raw_corr.strip()
        if not orig_id or not corr_id:
            raise ValueError("原表号与更正表号都必须填写，整单已退回")
        if orig_id in seen_orig:
            raise ValueError(
                f"原表号 {orig_id} 在同一单里重复出现，整单已退回"
            )
        if corr_id in seen_corr:
            raise ValueError(
                f"更正表号 {corr_id} 在同一单里重复出现，整单已退回"
            )
        seen_orig.add(orig_id)
        seen_corr.add(corr_id)
        normalized.append((orig_id, corr_id))

    with conn:
        found_orig = {
            row["household_id"]
            for row in conn.execute(
                "SELECT household_id FROM original_scores WHERE household_id IN "
                f"({','.join('?' * len(seen_orig))})",
                tuple(seen_orig),
            )
        }
        missing_orig = seen_orig - found_orig
        if missing_orig:
            raise ValueError(
                f"原表号 {', '.join(sorted(missing_orig))} 查不到，整单已退回"
            )

        # Only pairing metadata is read first; a household's score detail is
        # fetched later, and only if its pairing actually changes.
        placeholders_c = ",".join("?" * len(seen_corr))
        placeholders_o = ",".join("?" * len(seen_orig))
        affected_rows = conn.execute(
            "SELECT household_id, orig_id, weight_mode "
            "FROM corrected_scores "
            f"WHERE household_id IN ({placeholders_c}) "
            f"   OR orig_id IN ({placeholders_o})",
            (*seen_corr, *seen_orig),
        ).fetchall()
        by_id = {row["household_id"]: row for row in affected_rows}
        missing_corr = seen_corr - set(by_id)
        if missing_corr:
            raise ValueError(
                f"更正表号 {', '.join(sorted(missing_corr))} 查不到，整单已退回"
            )

        desired = {corr_id: orig_id for orig_id, corr_id in normalized}
        changes: list[tuple[str, str | None, str]] = []  # (id, new_orig, mode)
        for row in affected_rows:
            corr_id = row["household_id"]
            old_orig = row["orig_id"]
            old_mode = row["weight_mode"]
            new_orig = desired.get(corr_id)  # None -> loses partner / unpaired
            if new_orig is None:
                new_mode = old_mode if old_orig is None else MODE_INDEPENDENT
            else:
                new_mode = MODE_PAIRED
            if old_orig == new_orig and old_mode == new_mode:
                continue
            changes.append((corr_id, new_orig, new_mode))

        # Free all affected pairings first so a swap cannot trip the unique
        # index on orig_id; then write the new pairings.
        conn.executemany(
            "UPDATE corrected_scores SET orig_id = NULL WHERE household_id = ?",
            [(corr_id,) for corr_id, _, _ in changes],
        )
        for corr_id, new_orig, new_mode in changes:
            old = by_id[corr_id]
            # Read this household's detail only now: it is a changed pair.
            score = conn.execute(
                "SELECT score FROM corrected_scores WHERE household_id = ?",
                (corr_id,),
            ).fetchone()["score"]
            old_weights = _effective_weights(
                corr_id, old["orig_id"], old["weight_mode"]
            )
            new_weights = _effective_weights(corr_id, new_orig, new_mode)
            if old_weights != new_weights:
                _subtract_contribution(conn, old_weights, score)
                _add_contribution(conn, new_weights, score)
            conn.execute(
                "UPDATE corrected_scores SET orig_id = ?, weight_mode = ? "
                "WHERE household_id = ?",
                (new_orig, new_mode, corr_id),
            )

        unpaired = sum(1 for _, new_orig, _ in changes if new_orig is None)
        return {
            "affected": len(changes),
            "paired": len(normalized),
            "unpaired": unpaired,
        }


def unpair_corrected(conn: sqlite3.Connection, corrected_id: str) -> bool:
    """Explicitly remove one corrected household's pairing.

    Returns False if it was already unpaired.  A de-paired household always
    switches to the fixed independent weight stream, even when its id equals
    an original household's id.
    """
    with conn:
        row = _corrected_row(conn, corrected_id)
        if row is None:
            raise ValueError(f"更正表号 {corrected_id} 查不到，整单已退回")
        if row["orig_id"] is None:
            return False
        score = row["score"]
        old_weights = _effective_weights(
            corrected_id, row["orig_id"], row["weight_mode"]
        )
        new_weights = independent_household_weights(corrected_id)
        _subtract_contribution(conn, old_weights, score)
        _add_contribution(conn, new_weights, score)
        conn.execute(
            "UPDATE corrected_scores SET orig_id = NULL, weight_mode = ? "
            "WHERE household_id = ?",
            (MODE_INDEPENDENT, corrected_id),
        )
        return True


def reset_corrected(conn: sqlite3.Connection) -> None:
    """Reset the correction table (and its summary) back to same-id pairing."""
    with conn:
        orig = conn.execute(
            "SELECT household_id, score FROM original_scores"
        ).fetchall()
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM corr_rounds")
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score, orig_id, "
            "weight_mode) VALUES(?, ?, ?, 'paired')",
            [(r["household_id"], r["score"], r["household_id"]) for r in orig],
        )
        conn.executemany(
            "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            [(r, 0, 0) for r in range(ROUNDS)],
        )
        for r in orig:
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                [
                    (rnd, w, w * r["score"])
                    for rnd, w in enumerate(
                        household_weights(r["household_id"])
                    )
                ],
            )
        total = conn.execute(
            f"SELECT value FROM meta WHERE key = '{META_ORIG_SUM}'"
        ).fetchone()
        count = conn.execute(
            f"SELECT value FROM meta WHERE key = '{META_ORIG_N}'"
        ).fetchone()
        _set_meta(conn, META_CORR_SUM, total["value"] if total else 0)
        _set_meta(conn, META_CORR_N, count["value"] if count else 0)


def get_household(conn: sqlite3.Connection, household_id: str) -> dict | None:
    """Single-household lookup; no other household's detail is read.

    For a corrected household the paired original id (or an explicit
    unpaired marker) is returned along with the paired original's score.
    """
    o = conn.execute(
        "SELECT score FROM original_scores WHERE household_id = ?",
        (household_id,),
    ).fetchone()
    c = conn.execute(
        "SELECT score, orig_id, weight_mode FROM corrected_scores "
        "WHERE household_id = ?",
        (household_id,),
    ).fetchone()
    if o is None and c is None:
        return None

    paired_score = None
    if c is not None and c["orig_id"] is not None:
        paired = conn.execute(
            "SELECT score FROM original_scores WHERE household_id = ?",
            (c["orig_id"],),
        ).fetchone()
        paired_score = paired["score"] if paired is not None else None

    return {
        "household_id": household_id,
        "original_score": o["score"] if o is not None else None,
        "corrected_score": c["score"] if c is not None else None,
        "paired_orig_id": c["orig_id"] if c is not None else None,
        "paired_original_score": paired_score,
        "weight_mode": c["weight_mode"] if c is not None else None,
    }


def has_dataset(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM meta WHERE key = '{META_ORIG_N}'"
    ).fetchone()
    return row is not None
