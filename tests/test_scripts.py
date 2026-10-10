"""Tests for scripts/prune_images.py and scripts/validate (stdlib unittest):
    python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str, filename: str):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPTS / filename))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


prune = load("prune_images", "prune_images.py")
validate = load("validate", "validate")


class PruneTests(unittest.TestCase):
    def test_versions_sort_as_numbers(self):
        self.assertGreater(prune.version_key("1.0.10"), prune.version_key("1.0.9"))

    def test_stale_keeps_the_newest_and_ignores_other_tags(self):
        newest = ["0.1.5", "0.1.4", "0.1.3", "0.1.2"]
        tags = {"0.1.5", "0.1.4", "0.1.3", "0.1.2", "latest", "native-test"}
        self.assertEqual(prune.stale(tags, 2, newest), ["0.1.2", "0.1.3"])

    def test_node_images_keep_only_our_repos(self):
        out = {"images": [{"repoTags": ["docker.io/whitepatrick/wigle-sync:0.1.7"]},
                          {"repoTags": ["docker.io/library/postgres:15"]},
                          {"repoTags": None}]}
        done = mock.Mock(returncode=0, stdout=json.dumps(out))
        with mock.patch.object(prune, "run", return_value=done):
            self.assertEqual(prune.node_images("n"), {"wigle-sync": {"0.1.7"}})

    def _main(self, argv, hub, nodes, token=None, rmi_error=None):
        tags = {r: hub.get(r, []) for r in prune.REPOS}
        out = io.StringIO()
        with (mock.patch("sys.argv", ["prune_images.py", *argv]),
              mock.patch.object(prune, "hub_tags", side_effect=lambda r: tags[r]),
              mock.patch.object(prune, "hub_token", return_value=token),
              mock.patch.object(prune, "hub_request") as hub_request,
              mock.patch.object(prune, "node_images", side_effect=nodes),
              mock.patch.object(prune, "node_rmi", return_value=rmi_error) as node_rmi,
              mock.patch.object(prune, "local_images", return_value={}),
              redirect_stdout(out)):
            code = prune.main()
        return code, out.getvalue(), hub_request, node_rmi

    def test_dry_run_deletes_nothing(self):
        code, out, hub_request, node_rmi = self._main(
            [], {"wigle-sync": ["0.1.7", "0.1.6", "0.1.5"]}, lambda n: {"wigle-sync": {"0.1.5", "0.1.7"}})
        self.assertEqual(code, 0)
        hub_request.assert_not_called()
        node_rmi.assert_not_called()
        self.assertIn("would delete wigle-sync:0.1.5", out)

    def test_every_node_is_visited(self):
        seen = []
        self._main([], {}, lambda n: seen.append(n) or {})
        self.assertEqual(seen, prune.NODES)
        self.assertEqual(len(prune.NODES), 4)

    def test_apply_without_hub_credentials_is_exit_2(self):
        code, out, hub_request, node_rmi = self._main(
            ["--apply"], {"wigle-sync": ["0.1.7", "0.1.6", "0.1.5"]}, lambda n: {"wigle-sync": {"0.1.5"}})
        self.assertEqual(code, 2)
        hub_request.assert_not_called()
        self.assertEqual(node_rmi.call_count, len(prune.NODES))  # the nodes are still pruned

    def test_an_unreachable_node_is_exit_1(self):
        def nodes(n):
            raise RuntimeError("no route to host")
        code, out, _, _ = self._main([], {}, nodes)
        self.assertEqual(code, 1)
        self.assertIn("unreachable", out)


class ValidateTests(unittest.TestCase):
    def test_unknown_keys_and_freeform_blocks(self):
        chart = {"a": {"b": 1, "c": {}}, "config": {"x": 1}}
        ours = {"a": {"b": 2, "typo": 3, "c": {"anything": 1}}, "config": {"free": 1}, "nope": 1}
        self.assertEqual(validate.unknown_keys(ours, chart, {"config"}), ["a.typo", "nope"])

    def test_deep_merge_keeps_subchart_keys_under_partial_overrides(self):
        base = {"service": {"port": 1, "type": "ClusterIP"}, "x": 1}
        merged = validate.deep_merge(base, {"service": {"port": 2}})
        self.assertEqual(merged, {"service": {"port": 2, "type": "ClusterIP"}, "x": 1})


if __name__ == "__main__":
    unittest.main()
