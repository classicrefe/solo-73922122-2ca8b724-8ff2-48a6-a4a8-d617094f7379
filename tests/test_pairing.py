"""Pairing (改配) tests: swapped pairs, unpaired households, validation,
independent weights, legacy database migration and minimal recomputation."""

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import db as db_module  # noqa: E402
from app.bootstrap import (  # noqa: E402
    household_weights,
    independent_household_weights,
)
from app.db import (  # noqa: E402
    META_CORR_N,
    META_ORIG_N,
    apply_correction,
    apply_pairings,
    connect,
    get_household,
    init_db,
    replace_original,
    reset_corrected,
    unpair_corrected,
)
from app.stats import analyze  # noqa: E402


def _summaries(conn):
    return {
        r["round_index"]: (r["weight_total"], r["weighted_sum"])
        for r in conn.execute(
            "SELECT round_index, weight_total, weighted_sum FROM corr_rounds"
        )
    }


def _pairing_map(conn):
    return {
        r["household_id"]: (r["orig_id"], r["weight_mode"])
        for r in conn.execute(
            "SELECT household_id, orig_id, weight_mode FROM corrected_scores"
        )
    }


def _expected_rounds(entries):
    """entries: list of (weights, score); returns expected round summaries."""
    totals = [[0, 0] for _ in range(1000)]
    for weights, score in entries:
        for rnd, w in enumerate(weights):
            totals[rnd][0] += w
            totals[rnd][1] += w * score
    return {r: (w, ws) for r, (w, ws) in enumerate(totals)}


class PairingStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(os.path.join(self.tmp.name, "pair.db"))
        init_db(self.conn)
        self.rows = [(f"H{i:03d}", (i * 5) % 11) for i in range(8)]
        replace_original(self.conn, self.rows, "2026-11-23T00:00:00+00:00")
        self.score = dict(self.rows)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upload_starts_with_identity_pairing(self):
        pairing = _pairing_map(self.conn)
        for h, _ in self.rows:
            self.assertEqual(pairing[h], (h, "paired"))
        # identical tables -> identical summaries, as before
        orig = {
            r["round_index"]: (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, weight_total, weighted_sum FROM orig_rounds"
            )
        }
        self.assertEqual(_summaries(self.conn), orig)

    def test_pair_uses_partner_original_weights_and_keeps_scores(self):
        # H000 (corrected) is verified to be H003; its own id/score stay.
        summary = apply_pairings(self.conn, [("H003", "H000")])
        self.assertEqual(summary["affected"], 2)  # H000 gains, H003 loses
        pairing = _pairing_map(self.conn)
        self.assertEqual(pairing["H000"], ("H003", "paired"))
        # H003 lost its partner within this submission -> unpaired fixed
        self.assertEqual(pairing["H003"], (None, "independent"))
        self.assertEqual(self.score["H000"], dict(self.rows)["H000"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS k FROM original_scores"
            ).fetchone()["k"],
            len(self.rows),
        )

        entries = []
        for h, _ in self.rows:
            if h == "H000":
                entries.append(
                    (household_weights("H003"), self.score["H000"])
                )
            elif h == "H003":
                entries.append(
                    (independent_household_weights("H003"),
                     self.score["H003"])
                )
            else:
                entries.append(
                    (household_weights(h), self.score[h])
                )
        self.assertEqual(_summaries(self.conn), _expected_rounds(entries))

    def test_swap_within_one_submission(self):
        # initial H000->H000, H001->H001; submit a swapped pair in one shot
        summary = apply_pairings(self.conn, [("H000", "H001"),
                                             ("H001", "H000")])
        self.assertEqual(summary["affected"], 2)
        pairing = _pairing_map(self.conn)
        self.assertEqual(pairing["H000"], ("H001", "paired"))
        self.assertEqual(pairing["H001"], ("H000", "paired"))
        self.assertEqual(
            _summaries(self.conn),
            _expected_rounds([
                (household_weights("H001"), self.score["H000"]),
                (household_weights("H000"), self.score["H001"]),
                *[(household_weights(h), self.score[h])
                  for h, _ in self.rows if h not in ("H000", "H001")],
            ]),
        )

    def test_lost_partner_without_new_pair_is_unpaired(self):
        apply_pairings(self.conn, [("H002", "H000")])  # H002 frees, H000 takes
        # H002 is now unpaired; submit only (H004, H001) which does not
        # mention H002 -> H002 must stay unpaired
        apply_pairings(self.conn, [("H004", "H001")])
        pairing = _pairing_map(self.conn)
        self.assertEqual(pairing["H002"], (None, "independent"))
        self.assertEqual(pairing["H001"], ("H004", "paired"))

    def test_validation_rejects_whole_submission(self):
        before_pairs = _pairing_map(self.conn)
        before_sums = _summaries(self.conn)
        bad_submissions = [
            [("NOPE", "H000")],                    # unknown original id
            [("H001", "GHOST")],                   # unknown corrected id
            [("H001", "H000"), ("H001", "H002")],  # duplicate original side
            [("H001", "H000"), ("H002", "H000")],  # duplicate corrected side
            [("H001", "")],                        # half-filled row
            [],                                    # nothing filled
        ]
        for pairs in bad_submissions:
            with self.assertRaises(ValueError):
                apply_pairings(self.conn, pairs)
            self.assertEqual(_pairing_map(self.conn), before_pairs)
            self.assertEqual(_summaries(self.conn), before_sums)

    def test_unpaired_same_id_uses_fixed_independent_weights(self):
        apply_pairings(self.conn, [("H003", "H000")])  # unpaired H003 also
        unpair_corrected(self.conn, "H000")            # explicit unpaired
        pairing = _pairing_map(self.conn)
        self.assertEqual(pairing["H000"], (None, "independent"))

        indep = independent_household_weights("H000")
        shared = household_weights("H000")  # original H000's stream
        self.assertNotEqual(indep, shared)  # namespace-separated
        entries = [
            ((independent_household_weights(h) if h in ("H000", "H003")
              else household_weights(h)), self.score[h])
            for h, _ in self.rows
        ]
        self.assertEqual(_summaries(self.conn), _expected_rounds(entries))

    def test_score_change_uses_effective_pairing(self):
        apply_pairings(self.conn, [("H003", "H000")])
        apply_correction(self.conn, "H000", "update", 10)
        entries = []
        for h, _ in self.rows:
            if h == "H000":
                entries.append((household_weights("H003"), 10))
            elif h == "H003":
                entries.append(
                    (independent_household_weights("H003"), self.score[h])
                )
            else:
                entries.append((household_weights(h), self.score[h]))
        self.assertEqual(_summaries(self.conn), _expected_rounds(entries))
        n = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_ORIG_N}'").fetchone()["value"])
        cn = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_N}'").fetchone()["value"])
        self.assertEqual(n, cn)
        result = analyze(self.conn)
        self.assertEqual(result["valid_rounds"], 1000)

    def test_remove_frees_original_and_add_auto_pairs_only_when_idle(self):
        # Remove corrected H000 -> original H000 becomes idle.
        apply_correction(self.conn, "H000", "remove", None)
        self.assertIsNone(_pairing_map(self.conn).get("H000"))
        # Add H000 back -> auto-pair with the idle same-id original.
        apply_correction(self.conn, "H000", "add", 4)
        self.assertEqual(_pairing_map(self.conn)["H000"], ("H000", "paired"))
        self.assertEqual(
            _summaries(self.conn),
            _expected_rounds([
                (household_weights(h), 4 if h == "H000" else self.score[h])
                for h, _ in self.rows
            ]),
        )

        # Make original H000 busy: pair corrected H001 to it, freeing H001's.
        apply_pairings(self.conn, [("H000", "H001")])
        # Remove corrected H000 (now paired to H001's original H001? no:
        # H000 still paired to H000 originally, remove it to free H000?).
        # Instead demonstrate the busy case directly: remove corrected H001
        # (paired to H000), add H001 back -> its same-id original H001 is
        # now idle, so it re-pairs, which is correct.  For the busy case use
        # a fresh correction id that equals a paired original.
        apply_correction(self.conn, "H002", "remove", None)  # free orig H002
        apply_pairings(self.conn, [("H002", "H003")])        # H002 now busy
        apply_correction(self.conn, "H002", "add", 6)        # id == busy orig
        self.assertEqual(
            _pairing_map(self.conn)["H002"], (None, "independent")
        )

    def test_upload_and_reset_restore_identity_pairing(self):
        apply_pairings(self.conn, [("H003", "H000"), ("H004", "H001")])
        reset_corrected(self.conn)
        for h, _ in self.rows:
            self.assertEqual(_pairing_map(self.conn)[h], (h, "paired"))
        self.assertEqual(
            _summaries(self.conn),
            _expected_rounds([(household_weights(h), s) for h, s in self.rows]),
        )
        apply_pairings(self.conn, [("H003", "H000")])
        replace_original(self.conn, self.rows, "2026-11-24T00:00:00+00:00")
        for h, _ in self.rows:
            self.assertEqual(_pairing_map(self.conn)[h], (h, "paired"))

    def test_only_changed_households_are_recomputed(self):
        calls = []
        real = db_module.household_weights
        real_indep = db_module.independent_household_weights

        def spy(household_id, rounds=db_module.ROUNDS):
            calls.append(household_id)
            return real(household_id, rounds)

        def spy_indep(household_id, rounds=db_module.ROUNDS):
            calls.append(household_id)
            return real_indep(household_id, rounds)

        with mock.patch.object(db_module, "household_weights", spy), \
                mock.patch.object(db_module, "independent_household_weights",
                                  spy_indep):
            apply_pairings(self.conn, [("H003", "H000")])
        # changed: H000 (new pairing H003) and H003 (fell back to independent)
        self.assertEqual(set(calls), {"H000", "H003"})
        self.assertNotIn("H001", calls)

    def test_renumbered_corrected_household_takes_original_weights(self):
        # The corrected table uses a different number for the same household:
        # add it, then pair it to the original; the old same-id corrected row
        # loses its partner and becomes unpaired.
        apply_correction(self.conn, "C-NEW", "add", 6)
        apply_pairings(self.conn, [("H000", "C-NEW")])
        pairing = _pairing_map(self.conn)
        self.assertEqual(pairing["C-NEW"], ("H000", "paired"))
        self.assertEqual(pairing["H000"], (None, "independent"))
        # corrected ids and scores are both kept as entered
        self.assertEqual(get_household(self.conn, "C-NEW")["corrected_score"], 6)
        self.assertEqual(get_household(self.conn, "C-NEW")["original_score"], None)
        entries = []
        for h, _ in self.rows:
            if h == "H000":
                entries.append(
                    (independent_household_weights("H000"), self.score["H000"])
                )
            else:
                entries.append((household_weights(h), self.score[h]))
        entries.append((household_weights("H000"), 6))  # C-NEW, paired
        self.assertEqual(_summaries(self.conn), _expected_rounds(entries))

    def test_pairing_failure_rolls_back_details_and_summary(self):
        before_pairs = _pairing_map(self.conn)
        before_sums = _summaries(self.conn)
        with mock.patch.object(
            db_module, "_add_contribution",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                apply_pairings(self.conn, [("H003", "H000")])
        self.assertEqual(_pairing_map(self.conn), before_pairs)
        self.assertEqual(_summaries(self.conn), before_sums)
        # connection remains usable and a subsequent pairing succeeds
        apply_pairings(self.conn, [("H003", "H000")])
        self.assertEqual(_pairing_map(self.conn)["H000"], ("H003", "paired"))

    def test_lookup_reports_pairing_or_unpaired(self):
        apply_pairings(self.conn, [("H003", "H000")])
        paired = get_household(self.conn, "H000")
        self.assertEqual(paired["paired_orig_id"], "H003")
        self.assertEqual(paired["paired_original_score"], self.score["H003"])
        self.assertEqual(paired["weight_mode"], "paired")
        unp = get_household(self.conn, "H003")
        self.assertIsNone(unp["paired_orig_id"])
        self.assertEqual(unp["weight_mode"], "independent")


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """A database created by the previous schema keeps its old result."""

    def test_old_database_keeps_previous_result(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "legacy.db")
            conn = connect(path)
            # Recreate the old schema (no pairing columns / marker).
            conn.executescript(
                """
                CREATE TABLE original_scores (
                    household_id TEXT PRIMARY KEY,
                    score INTEGER NOT NULL
                );
                CREATE TABLE corrected_scores (
                    household_id TEXT PRIMARY KEY,
                    score INTEGER NOT NULL
                );
                CREATE TABLE orig_rounds (
                    round_index INTEGER PRIMARY KEY,
                    weight_total INTEGER NOT NULL,
                    weighted_sum INTEGER NOT NULL
                );
                CREATE TABLE corr_rounds (
                    round_index INTEGER PRIMARY KEY,
                    weight_total INTEGER NOT NULL,
                    weighted_sum INTEGER NOT NULL
                );
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            rows = [("A", 3), ("B", 7), ("C", 5)]
            conn.executemany(
                "INSERT INTO original_scores VALUES(?, ?)", rows)
            # old working table: A changed, B still same-id, NEW added
            conn.executemany(
                "INSERT INTO corrected_scores VALUES(?, ?)",
                [("A", 9), ("NEW", 4)],
            )
            orig_rounds = [[0, 0] for _ in range(1000)]
            corr_rounds = [[0, 0] for _ in range(1000)]
            for h, s in rows:
                for r, w in enumerate(household_weights(h)):
                    orig_rounds[r][0] += w
                    orig_rounds[r][1] += w * s
            for h, s in [("A", 9), ("NEW", 4)]:
                for r, w in enumerate(household_weights(h)):
                    corr_rounds[r][0] += w
                    corr_rounds[r][1] += w * s
            conn.executemany(
                "INSERT INTO orig_rounds VALUES(?, ?, ?)",
                [(r, w, ws) for r, (w, ws) in enumerate(orig_rounds)],
            )
            conn.executemany(
                "INSERT INTO corr_rounds VALUES(?, ?, ?)",
                [(r, w, ws) for r, (w, ws) in enumerate(corr_rounds)],
            )
            conn.executemany(
                "INSERT INTO meta VALUES(?, ?)",
                [("orig_count", "3"), ("orig_score_sum", "15"),
                 ("corr_count", "2"), ("corr_score_sum", "13"),
                 ("uploaded_at", "old")],
            )
            conn.commit()
            before_sums = _summaries(conn)
            before_result = analyze(conn)
            conn.close()

            conn = connect(path)
            init_db(conn)  # migration runs here
            self.assertEqual(_summaries(conn), before_sums)
            self.assertEqual(analyze(conn), before_result)
            pairing = _pairing_map(conn)
            self.assertEqual(pairing["A"], ("A", "paired"))
            # no same-id original for NEW: keep old same-id stream
            self.assertEqual(pairing["NEW"], (None, "legacy_same_id"))
            # score change after migration still uses old A stream
            apply_correction(conn, "A", "update", 10)
            expected = []
            for r in range(1000):
                w_a = household_weights("A")[r]
                w_new = household_weights("NEW")[r]
                expected.append((w_a + w_new, w_a * 10 + w_new * 4))
            after = _summaries(conn)
            self.assertEqual(
                {r: after[r] for r in range(1000)},
                {r: expected[r] for r in range(1000)},
            )
            conn.close()
        finally:
            tmp.cleanup()


class PairingWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(db_path=os.path.join(self.tmp.name, "web.db"))
        self.client = self.app.test_client()
        csv = "住户号,评分\n" + "\n".join(
            f"{h},{s}" for h, s in [("A", 1), ("B", 5), ("C", 8), ("D", 9)]
        )
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(csv.encode("utf-8")), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_web_pair_swap_lookup_and_unpair(self):
        # In-place swap A<->B: A takes B, B takes A.
        resp = self.client.post(
            "/pair",
            data={"original_id": ["B", "A"], "corrected_id": ["A", "B"]},
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        self.assertIn("改配已生效", body)
        self.assertIn("重算 2 户明细", body)

        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn("配对原表号", body)
        self.assertIn("<code>B</code>", body)
        self.assertIn("原表评分 5", body)

        # unpaired lookup marker
        self.client.post("/pair", data={
            "original_id": ["B", "A"], "corrected_id": ["C", "D"],
        }, follow_redirects=True)
        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn("未配对（使用固定独立权重）", body)

        # validation error is shown and keeps result
        bad = self.client.post("/pair", data={
            "original_id": ["NOPE"], "corrected_id": ["A"],
        }, follow_redirects=True)
        self.assertIn("整单退回", bad.get_data(as_text=True))
        self.assertIn("查不到", bad.get_data(as_text=True))

        # explicit unpair route
        r = self.client.post("/unpair", data={"household_id": "C"},
                             follow_redirects=True)
        self.assertIn("已解配", r.get_data(as_text=True))

    def test_web_reset_restores_same_id_pairing(self):
        self.client.post("/pair", data={
            "original_id": ["B"], "corrected_id": ["A"],
        }, follow_redirects=True)
        self.client.post("/reset", follow_redirects=True)
        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn("配对原表号 <code>A</code>", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
