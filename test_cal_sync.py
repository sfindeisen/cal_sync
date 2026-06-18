import unittest

import cal_sync as cs


class TestClassify(unittest.TestCase):
    def test_prefix_matches_count_as_marker_with_warning_flag(self):
        self.assertEqual(cs.classify("FD"), ("FD", True))
        self.assertEqual(cs.classify("FD (zamiana z B.Cao)"), ("FD", False))
        self.assertEqual(cs.classify("SD - note"), ("SD", False))
        self.assertEqual(cs.classify("Dentist"), (None, None))


class TestComputeIntervals(unittest.TestCase):
    def test_spec_example_single_block(self):
        # Normal availability 09:00-18:00 on a Wednesday.
        normal = {2: [(540, 1080)]}
        fd = cs.desired_intervals({"2026-07-01": {"FD"}}, normal)
        sd = cs.desired_intervals({"2026-07-01": {"SD"}}, normal)
        self.assertEqual(fd["2026-07-01"], [(1020, 1080)])  # 17:00-18:00
        self.assertEqual(sd["2026-07-01"], [(540, 780)])    # 09:00-13:00

    def test_spec_example_split_day(self):
        normal = {2: [(480, 720), (840, 1200)]}  # 08:00-12:00, 14:00-20:00
        fd = cs.desired_intervals({"2026-07-01": {"FD"}}, normal)
        sd = cs.desired_intervals({"2026-07-01": {"SD"}}, normal)
        self.assertEqual(fd["2026-07-01"], [(1020, 1200)])  # 17:00-20:00
        self.assertEqual(sd["2026-07-01"], [(480, 720)])    # 08:00-12:00

    def test_fd_and_sd_conflict_yields_empty(self):
        normal = {2: [(0, 1440)]}
        result = cs.desired_intervals({"2026-07-01": {"FD", "SD"}}, normal)
        self.assertEqual(result["2026-07-01"], [])


class TestDiffAndMerge(unittest.TestCase):
    def test_fully_unavailable_includes_placeholder_times(self):
        # Cal.com rejects override entries without string startTime/endTime,
        # even when isUnavailable is set (confirmed via a live 400 response).
        entries = cs.to_override_entries("2026-06-22", [])
        self.assertEqual(
            entries, [{"date": "2026-06-22", "startTime": "00:00", "endTime": "00:00", "isUnavailable": True}]
        )

    def test_unavailable_matches_existing_zero_length_range(self):
        # Cal.com's GET response for a fully-unavailable date apparently
        # omits the isUnavailable flag, returning just a zero-length range.
        # That must still compare as unchanged, or every run would re-send it.
        existing = [{"date": "2026-06-22", "startTime": "00:00", "endTime": "00:00"}]
        desired = {"2026-06-22": []}
        _, created, updated, unchanged = cs.diff_and_merge(existing, desired)
        self.assertEqual((created, updated), ([], []))
        self.assertEqual(unchanged, ["2026-06-22"])

    def test_create_update_unchanged(self):
        existing = [{"date": "2026-07-02", "startTime": "09:00", "endTime": "10:00"}]
        desired = {
            "2026-07-01": [(1020, 1080)],  # not in existing -> create
            "2026-07-02": [(1020, 1080)],  # differs from existing -> update
        }
        final, created, updated, unchanged = cs.diff_and_merge(existing, desired)
        self.assertEqual(created, ["2026-07-01"])
        self.assertEqual(updated, ["2026-07-02"])
        self.assertEqual(unchanged, [])

    def test_unmanaged_dates_survive_and_rerun_is_a_noop(self):
        existing = [{"date": "2026-12-25", "isUnavailable": True}]
        desired = {"2026-07-01": [(1020, 1080)]}

        final, created, _, _ = cs.diff_and_merge(existing, desired)
        self.assertIn({"date": "2026-12-25", "isUnavailable": True}, final)
        self.assertEqual(created, ["2026-07-01"])

        _, created2, updated2, unchanged2 = cs.diff_and_merge(final, desired)
        self.assertEqual((created2, updated2), ([], []))
        self.assertEqual(unchanged2, ["2026-07-01"])


if __name__ == "__main__":
    unittest.main()
