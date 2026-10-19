import unittest

from aggregate import aggregate, valid_vote


def vote(mode, keys, positions):
    return {"mode": mode, "songKeys": keys, "positions": positions, "trackCount": len(keys)}


class AggregateTests(unittest.TestCase):
    def test_straight_histogram(self):
        votes = [
            vote("straight", ["a", "b", "c"], [1, 2, 3]),
            vote("straight", ["a", "b", "c"], [1, 3, 2]),
            vote("straight", ["a", "b", "c"], [2, 1, 3]),
        ]
        stats = aggregate("album", "straight", votes)
        self.assertEqual(stats["voteCount"], 3)
        self.assertEqual(stats["bucketCount"], 3)
        self.assertEqual(stats["songKeys"], ["a", "b", "c"])
        # Row-major: one row of 3 buckets per song.
        self.assertEqual(stats["buckets"], [2, 1, 0, 1, 1, 1, 0, 1, 2])

    def test_tier_uses_six_buckets(self):
        stats = aggregate("album", "tier", [vote("tier", ["a", "b"], [1, 6])])
        self.assertEqual(stats["bucketCount"], 6)
        self.assertEqual(stats["buckets"], [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1])

    def test_invalid_votes_are_ignored(self):
        good = vote("straight", ["a", "b"], [1, 2])
        bad = [
            vote("straight", ["a", "b"], [1, 3]),       # position out of range
            vote("straight", ["a", "a"], [1, 2]),       # duplicate song
            {**good, "trackCount": 5},                  # count mismatch
            {**good, "mode": "unknown"},
            {"mode": "straight"},                       # missing fields
        ]
        self.assertIsNone(valid_vote(bad[0]))
        stats = aggregate("album", "straight", [good] + bad)
        self.assertEqual(stats["voteCount"], 1)

    def test_minority_track_count_is_dropped(self):
        votes = [vote("straight", ["a", "b"], [1, 2])] * 2 + [vote("straight", ["a", "b", "c"], [1, 2, 3])]
        stats = aggregate("album", "straight", votes)
        self.assertEqual(stats["voteCount"], 2)
        self.assertEqual(stats["songKeys"], ["a", "b"])

    def test_no_votes(self):
        self.assertIsNone(aggregate("album", "straight", []))


if __name__ == "__main__":
    unittest.main()
