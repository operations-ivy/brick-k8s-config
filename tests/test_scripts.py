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
join_worker = load("join_worker", "join-worker")
rebalance = load("rebalance", "rebalance")


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


class JoinWorkerTests(unittest.TestCase):
    def test_cgroups_needed_until_both_flags_are_on(self):
        self.assertTrue(join_worker.needs_cgroups("console=tty1 rootwait"))
        self.assertTrue(join_worker.needs_cgroups("rootwait cgroup_enable=memory"))
        self.assertFalse(join_worker.needs_cgroups("rootwait cgroup_enable=memory cgroup_memory=1"))

    def test_journald_cap_by_disk(self):
        self.assertEqual(join_worker.journald_cap("/dev/mmcblk0p2"), "1G")
        self.assertEqual(join_worker.journald_cap("/dev/nvme0n1p2"), "2G")

    def test_node_ip_keeps_the_rest_of_the_config(self):
        existing = 'node-ip: 10.0.0.9\nkubelet-arg:\n  - "system-reserved=cpu=500m,memory=1Gi"\n'
        out = join_worker.with_node_ip(existing, "192.0.2.5")
        self.assertIn("node-ip: 192.0.2.5", out)
        self.assertNotIn("10.0.0.9", out)
        self.assertIn('  - "system-reserved=cpu=500m,memory=1Gi"', out)
        self.assertEqual(out, join_worker.with_node_ip(out, "192.0.2.5"))  # idempotent
        self.assertTrue(join_worker.with_node_ip("", "192.0.2.5").startswith("# IPv4 only"))


def workload(kind, ns, name, claims=(), templates=False):
    w = {"kind": kind, "metadata": {"namespace": ns, "name": name},
         "spec": {"template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": c}} for c in claims]}}}}
    if templates:
        w["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"}}]
    return w


class RebalanceTests(unittest.TestCase):
    def test_newest_worker_never_the_control_plane(self):
        nodes = [{"metadata": {"name": "cp", "creationTimestamp": "2026-12-01T00:00:00Z",
                               "labels": {"node-role.kubernetes.io/control-plane": "true"}}},
                 {"metadata": {"name": "old", "creationTimestamp": "2026-09-01T00:00:00Z"}},
                 {"metadata": {"name": "new", "creationTimestamp": "2026-10-10T00:00:00Z"}}]
        self.assertEqual(rebalance.newest_node(nodes), "new")

    def test_local_volumes_and_blips_stay_put(self):
        pvs = [{"metadata": {"name": "pv1"}, "spec": {"storageClassName": "local-path"}}]
        pvcs = [{"metadata": {"namespace": "chuck", "name": "postgres-pvc"}, "spec": {"volumeName": "pv1"}}]
        claims = rebalance.local_claims(pvcs, pvs)
        plan = {(ns, name): why for _, ns, name, why in rebalance.movable([
            workload("Deployment", "chuck", "postgres", ["postgres-pvc"]),
            workload("Deployment", "chuck", "reader"),
            workload("StatefulSet", "monitoring", "loki", templates=True),
            workload("Deployment", "kube-system", "coredns"),
        ], claims, include_all=False)}
        self.assertEqual(plan[("chuck", "reader")], "")
        self.assertEqual(plan[("chuck", "postgres")], "local volume")
        self.assertEqual(plan[("monitoring", "loki")], "local volume")
        self.assertIn("--all", plan[("kube-system", "coredns")])
        with_all = {name: why for _, _, name, why in rebalance.movable(
            [workload("Deployment", "kube-system", "coredns")], claims, include_all=True)}
        self.assertEqual(with_all["coredns"], "")


if __name__ == "__main__":
    unittest.main()
