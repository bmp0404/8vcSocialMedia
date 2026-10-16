"""Run with: python3 -m unittest discover -s .github/scripts"""

import unittest

from codeowner_check import owners_for, parse_codeowners, pending

CODEOWNERS = """
# Each folder has one owner; shared contracts have several.
/harness/minds/      @rushisy
/harness/qualify.py  @ATX24
docs/                @willatle
/data/*              @SamGu-NRX
*.md                 @bmp0404
/harness/cli.py      @rushisy @SamGu-NRX
**/fixtures          @rmbriggs
"""

RULES = parse_codeowners(CODEOWNERS)


class OwnersFor(unittest.TestCase):
    def test_rooted_folder_covers_nested_files(self):
        self.assertEqual(owners_for("harness/minds/prompts/stat_nerd.py", RULES), {"rushisy"})

    def test_later_line_wins(self):
        self.assertEqual(owners_for("harness/minds/prompts/stat_nerd.md", RULES), {"bmp0404"})

    def test_unrooted_folder_matches_at_any_depth(self):
        self.assertEqual(owners_for("eval/docs/design.txt", RULES), {"willatle"})

    def test_star_covers_direct_children_only(self):
        self.assertEqual(owners_for("data/sample.json", RULES), {"samgu-nrx"})
        self.assertEqual(owners_for("data/context/x.csv", RULES), set())

    def test_double_star_matches_root_and_nested(self):
        self.assertEqual(owners_for("fixtures/a.json", RULES), {"rmbriggs"})
        self.assertEqual(owners_for("eval/fixtures/a.json", RULES), {"rmbriggs"})

    def test_unowned_path(self):
        self.assertEqual(owners_for("harness/llm.py", RULES), set())

    def test_rooted_file_does_not_match_elsewhere(self):
        self.assertEqual(owners_for("eval/harness/qualify.py", RULES), set())


class Pending(unittest.TestCase):
    def test_own_folder_and_unowned_files_pass(self):
        files = ["harness/minds/__init__.py", "harness/llm.py"]
        self.assertEqual(pending(files, RULES, "rushisy", set()), {})

    def test_someone_elses_folder_waits_for_them(self):
        waiting = pending(["harness/qualify.py"], RULES, "rushisy", set())
        self.assertEqual(waiting, {"harness/qualify.py": ["atx24"]})

    def test_owner_approval_clears_it(self):
        self.assertEqual(pending(["harness/qualify.py"], RULES, "rushisy", {"atx24"}), {})

    def test_approval_from_a_non_owner_does_not_count(self):
        self.assertIn("harness/qualify.py", pending(["harness/qualify.py"], RULES, "rushisy", {"willatle"}))

    def test_shared_file_needs_another_owner_even_for_an_owner(self):
        self.assertEqual(pending(["harness/cli.py"], RULES, "rushisy", set()), {"harness/cli.py": ["samgu-nrx"]})
        self.assertEqual(pending(["harness/cli.py"], RULES, "rushisy", {"samgu-nrx"}), {})

    def test_author_login_is_case_insensitive(self):
        self.assertEqual(pending(["harness/qualify.py"], RULES, "atx24", set()), {})


if __name__ == "__main__":
    unittest.main()
