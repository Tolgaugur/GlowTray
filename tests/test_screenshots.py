"""The README captures must end at clean panel boundaries and be reproducible."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "glowtray_screenshots", Path(__file__).resolve().parents[1] / "tools" / "screenshots.py"
)
assert SPEC is not None and SPEC.loader is not None
shots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shots)


class ReadmeScreenshotTests(unittest.TestCase):
    def test_four_product_tabs_have_captures(self):
        self.assertEqual(set(shots.SHOTS), {"light", "auto", "stats", "settings"})
        self.assertEqual(len({name for name, _ in shots.SHOTS.values()}), 4)
        self.assertEqual(shots.SHOTS["stats"][1], 510)
        self.assertEqual(shots.SHOTS["settings"][1], 515)
        for _, crop_height in shots.SHOTS.values():
            self.assertLessEqual(crop_height, shots.H)
            self.assertGreater(crop_height, 0)

    def test_stats_tiles_shrink_inside_compact_panel(self):
        css = (Path(__file__).resolve().parents[1] / "app" / "ui" / "index.html").read_text()
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", css)

    def test_compact_tab_labels_and_timeout_unit_stay_on_one_line(self):
        ui = (Path(__file__).resolve().parents[1] / "app" / "ui" / "index.html").read_text()
        self.assertIn(".tabs button{flex:1;min-width:0;", ui)
        self.assertIn("font-size:12px", ui.split(".tabs button{", 1)[1].split("}", 1)[0])
        self.assertIn(".timeout-value{display:inline-flex;", ui)
        self.assertIn("white-space:nowrap;flex:none", ui.split(".timeout-value{", 1)[1].split("}", 1)[0])
        self.assertIn('class="timeout-value"><input type="number" id="idleSec"', ui)

    def test_readme_shows_all_four_static_captures(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        for name, _ in shots.SHOTS.values():
            self.assertIn(f"docs/{name}", readme)
        self.assertNotIn("feature-tour.mp4", readme)
        self.assertNotIn("feature-tour.gif", readme)

    def test_sample_stats_renders_usage_without_user_data(self):
        stats = shots.sample_stats()
        self.assertEqual(len(stats["series7"]), 7)
        self.assertEqual(len(stats["series30"]), 30)
        self.assertEqual(len(stats["month"]["hours"]), 24)
        self.assertTrue(stats["today"]["on"] > 0)
        self.assertEqual(stats["price"], 0)

    def test_stats_mock_is_connected_to_panel_api(self):
        page = shots.MOCK % (shots.W, shots.H, json.dumps(shots.sample_data()),
                             json.dumps(shots.sample_stats()))
        self.assertIn("stats: STATS", page)
        self.assertIn('"series7"', page)

    def test_browser_override_supports_non_windows_capture(self):
        with patch.dict(os.environ, {"GLOWTRAY_SCREENSHOT_BROWSER": "/bin/true"}):
            self.assertEqual(shots.find_browser(), "/bin/true")

    def test_browser_override_rejects_missing_binary(self):
        with patch.dict(os.environ, {"GLOWTRAY_SCREENSHOT_BROWSER": "/missing/browser"}):
            with self.assertRaises(SystemExit):
                shots.find_browser()


if __name__ == "__main__":
    unittest.main()
