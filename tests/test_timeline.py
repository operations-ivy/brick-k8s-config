"""timeline.brick.nozdormu.cloud: the feed job (front-door/timeline/feed.py), the
curated history, the generated ConfigMaps and their wiring into Caddy:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
TIMELINE = ROOT / "front-door/timeline"

spec = importlib.util.spec_from_file_location("timeline_feed", TIMELINE / "feed.py")
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


def pr(number, merged, title="A change"):
    return {"number": number, "title": title, "html_url": f"https://github.com/x/pull/{number}",
            "merged_at": merged}


class FeedTest(unittest.TestCase):
    def test_only_merged_pull_requests_and_every_page(self):
        pages = {1: [pr(i, "2026-11-03T10:00:00Z") for i in range(99)] + [pr(99, None)],
                 2: [pr(100, "2026-11-04T10:00:00Z")]}
        calls = []

        def fetch(url):
            calls.append(url)
            return pages[int(url.rsplit("page=", 1)[1])]

        found = feed.merged_pulls("brick-k8s-config", fetch)
        self.assertEqual(len(found), 100)  # 99 + 1; the closed-unmerged one is left out
        self.assertEqual(len(calls), 2)  # a full page means there may be another
        self.assertEqual(found[0]["merged"], datetime(2026, 11, 3, 10, tzinfo=timezone.utc))

    def test_a_slide_per_repo_per_week_newest_first_and_escaped(self):
        merged = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        pulls = {"presto": [{"number": 1, "title": "Old <b>", "url": "u1", "merged": merged("2026-11-03T09:00")},
                            {"number": 2, "title": "New", "url": "u2", "merged": merged("2026-11-08T23:00")},
                            {"number": 3, "title": "Next week", "url": "u3", "merged": merged("2026-11-09T01:00")}],
                 "wigle-sync": [{"number": 7, "title": "One", "url": "u7", "merged": merged("2026-11-05T12:00")}]}
        events = feed.slides(pulls)
        self.assertEqual([e["unique_id"] for e in events],
                         ["pr-presto-2026-11-02", "pr-wigle-sync-2026-11-02", "pr-presto-2026-11-09"])
        first = events[0]
        self.assertEqual(first["start_date"], {"year": 2026, "month": 11, "day": 2})
        self.assertEqual(first["text"]["headline"], "presto: 2 pull requests merged")
        self.assertLess(first["text"]["text"].index("#2"), first["text"]["text"].index("#1"))
        self.assertIn("Old &lt;b&gt;", first["text"]["text"])
        self.assertEqual(events[1]["text"]["headline"], "wigle-sync: 1 pull request merged")
        self.assertEqual({e["group"] for e in events}, {feed.GROUP})

    def test_week_of_is_the_monday(self):
        self.assertEqual(feed.week_of(datetime(2026, 11, 8, 23, tzinfo=timezone.utc)), date(2026, 11, 2))
        self.assertEqual(feed.week_of(datetime(2026, 11, 9, 0, tzinfo=timezone.utc)), date(2026, 11, 9))

    def test_github_unreadable_writes_nothing(self):
        with mock.patch.object(feed, "feed", side_effect=OSError("no route")), \
                mock.patch.object(feed, "write_configmap") as write, \
                mock.patch.object(feed, "push_metrics") as push, \
                mock.patch.object(sys, "argv", ["feed.py"]):
            self.assertEqual(feed.main(), 1)
        write.assert_not_called()
        push.assert_called_once_with({}, ok=False)

    def test_a_good_run_writes_the_configmap_and_pushes(self):
        data = {"generated": "x", "pull_requests": {"presto": 2}, "events": [{"a": 1}]}
        with mock.patch.object(feed, "feed", return_value=data), \
                mock.patch.object(feed, "write_configmap") as write, \
                mock.patch.object(feed, "push_metrics") as push, \
                mock.patch.object(sys, "argv", ["feed.py"]):
            self.assertEqual(feed.main(), 0)
        self.assertEqual(json.loads(write.call_args.args[0]), data)
        push.assert_called_once_with(data, ok=True)

    def test_only_public_repos(self):
        # The job reads GitHub anonymously: a private repo would fail every run.
        self.assertNotIn("homelab-context", feed.REPOS)
        self.assertNotIn("brick-k8s-secrets", feed.REPOS)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.history = yaml.safe_load((TIMELINE / "timeline-history.yaml").read_text())

    def test_every_milestone_is_complete_and_in_a_known_row(self):
        groups = set(self.history["groups"])
        for e in self.history["events"]:
            with self.subTest(e.get("headline")):
                self.assertIsInstance(e["date"], date)
                self.assertTrue(e["headline"])
                self.assertIn(e["group"], groups)

    def test_eras_run_in_order_without_overlapping(self):
        eras = self.history["eras"]
        for a, b in zip(eras, eras[1:]):
            self.assertLess(a["end"], b["start"])
        self.assertNotIn("end", eras[-1])  # the current era runs to today

    def test_it_starts_with_homelab_context_inside_its_era(self):
        starts = [e["date"] for e in self.history["events"] if e["headline"] == "homelab-context starts"]
        self.assertEqual(starts, [date(2026, 10, 10)])
        self.assertEqual(self.history["eras"][-1]["start"], date(2026, 10, 10))

    def test_no_addresses_or_private_repos(self):
        text = (TIMELINE / "timeline-history.yaml").read_text()
        self.assertNotRegex(text, r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for private in ("brick-k8s-secrets",):
            self.assertNotIn(private, text)


class WiringTest(unittest.TestCase):
    def test_the_generated_configmaps_are_up_to_date(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/build-timeline"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_yaml_line_breaks_in_text_data(self):
        # A raw U+0085 in a data value is turned into a space by the API server's
        # YAML parser: the live ConfigMap never matches git (Argo CD OutOfSync).
        for doc in yaml.safe_load_all((ROOT / "front-door/timeline-site.yaml").read_text()):
            for key, value in doc.get("data", {}).items():
                with self.subTest(key):
                    self.assertFalse(any(c in value for c in ("\x85", "\u2028", "\u2029")))
        vendor = next(d for d in yaml.safe_load_all((ROOT / "front-door/timeline-site.yaml").read_text())
                      if d["metadata"]["name"] == "timeline-vendor")
        self.assertIn("js-yaml-5.5.0.min.js", vendor["binaryData"])

    def test_caddy_serves_the_three_configmaps(self):
        docs = [d for d in yaml.safe_load_all((ROOT / "front-door/caddy.yaml").read_text()) if d]
        caddyfile = next(d for d in docs if d["kind"] == "ConfigMap")["data"]["Caddyfile"]
        self.assertIn("@timeline host timeline.brick.nozdormu.cloud", caddyfile)
        self.assertIn("root * /srv/timeline", caddyfile)
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "timeline")
        sources = {s["configMap"]["name"]: s["configMap"].get("optional", False) for s in volume["projected"]["sources"]}
        self.assertEqual(sources, {"timeline-site": False, "timeline-vendor": False, "timeline-feed": True})
        site = {d["metadata"]["name"] for d in yaml.safe_load_all((ROOT / "front-door/timeline-site.yaml").read_text())}
        self.assertLessEqual({"timeline-site", "timeline-vendor"}, site)

    def test_the_cronjob_runs_the_script_it_is_given(self):
        docs = {d["kind"]: d for d in yaml.safe_load_all((ROOT / "front-door/timeline.yaml").read_text()) if d}
        pod = docs["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], "timeline-feed-script")
        self.assertEqual(pod["containers"][0]["command"][-1], "/opt/timeline/feed.py")
        self.assertIn("@sha256:", pod["containers"][0]["image"])
        self.assertEqual(feed.CONFIGMAP, "timeline-feed")

    def test_the_catalog_has_a_tile(self):
        catalog = (ROOT / "front-door/catalog.yaml").read_text()
        self.assertIn('<symbol id="m-timeline"', catalog)
        self.assertIn('{id: "timeline", name: "Timeline"', catalog)

    def test_no_api_keys_in_the_vendored_code(self):
        # Knight Lab's demo keys are blanked (vendor/README.md); a new version
        # brings them back unless that's redone.
        import re
        for path in [TIMELINE / "vendor/timelinejs3-3.9.13.js", ROOT / "front-door/timeline-site.yaml"]:
            text = path.read_text()
            self.assertNotRegex(text, r"AIza[0-9A-Za-z_-]{35}", path.name)
            self.assertNotRegex(text, r'api_key_flickr:"[0-9a-f]+"', path.name)
            self.assertNotRegex(text, r"access_token=\d+\|", path.name)

    def test_the_page_uses_the_vendored_timelinejs3(self):
        page = (TIMELINE / "index.html").read_text()
        for src in ("timelinejs3-3.9.13.js", "timelinejs3-3.9.13.dark.css", "js-yaml-5.5.0.min.js"):
            self.assertIn(src, page)
            self.assertTrue((TIMELINE / "vendor" / src).exists())
        self.assertNotIn("cdn.knightlab.com", page)
        self.assertIn("new TL.Timeline(", page)


if __name__ == "__main__":
    unittest.main()
