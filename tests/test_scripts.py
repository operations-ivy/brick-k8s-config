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
trial = load("trial_branch", "trial-branch")


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
        self.assertEqual(len(set(prune.NODES)), len(prune.NODES))  # each once
        # Whether NODES matches the cluster is validate --cluster's job.

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
    def test_node_list_drift(self):
        self.assertEqual(validate.node_list_drift(["10.0.0.1", "10.0.0.2"], {"10.0.0.2", "10.0.0.1"}), [])
        self.assertEqual(validate.node_list_drift(["10.0.0.1", "10.0.0.9"], {"10.0.0.1", "10.0.0.2"}),
                         ["scripts/prune_images.py NODES: missing 10.0.0.2, not in the cluster 10.0.0.9"])

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

    def test_case_fan_appends_once_and_replaces_old_values(self):
        fan = join_worker.CASE_FANS["brick666"]
        stock = "arm_64bit=1\n[pi5]\ndtoverlay=nospi10\n[all]\n"
        out = join_worker.with_case_fan(stock, fan)
        self.assertTrue(out.startswith(stock.rstrip("\n")))
        self.assertEqual(out.splitlines()[-len(fan):], list(fan))
        self.assertEqual(join_worker.with_case_fan(out, fan), out)  # idempotent
        # A different old value is replaced, not duplicated.
        old = stock + "dtparam=fan_temp0_speed=75\n"
        self.assertEqual(join_worker.with_case_fan(old, fan).count("fan_temp0_speed"), 1)
        # Set by hand already (brick666, 2026-10-15): left alone.
        by_hand = stock + "\n[all]\n" + "\n".join(fan) + "\n"
        self.assertEqual(join_worker.with_case_fan(by_hand, fan), by_hand)
        # Under a section that isn't [all], it isn't in effect everywhere: move it.
        scoped = "[cm5]\n" + "\n".join(fan) + "\n"
        self.assertTrue(join_worker.with_case_fan(scoped, fan).endswith("\n".join(fan) + "\n"))
        self.assertIn("[all]", join_worker.with_case_fan(scoped, fan))

    def test_radio_rules_name_each_known_dongle_and_unmanage_it(self):
        rules = join_worker.radio_rules()
        udev, nm = rules[join_worker.UDEV_RULES], rules[join_worker.NM_UNMANAGED]
        for vendor, product in join_worker.MONITOR_RADIOS:
            self.assertIn(f'ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}", NAME="wlmon0"', udev)
        self.assertIn("unmanaged-devices=interface-name:wlmon*", nm)
        self.assertTrue(udev.endswith("\n") and nm.endswith("\n"))


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


class TrialBranchTests(unittest.TestCase):
    APP = {"metadata": {"name": "overwatch"}, "spec": {"source": {"repoURL": "https://github.com/x/overwatch.git"}}}
    HELM = {"metadata": {"name": "loki", "labels": {"a": "b"}}, "spec": {"sources": [
        {"repoURL": "https://grafana.github.io/helm-charts", "chart": "loki"},
        {"repoURL": "https://github.com/x/brick-k8s-config.git", "ref": "values"}]}}

    def test_a_branch_moves_the_git_source_and_labels_it(self):
        ops = trial.patch(self.APP, "fix-x")
        self.assertEqual(ops[0], {"op": "replace", "path": "/spec/source/targetRevision", "value": "fix-x"})
        self.assertIn({"op": "add", "path": "/metadata/labels/brick-trial", "value": "true"}, ops)
        self.assertIn({"op": "add", "path": "/metadata/annotations/brick-trial-branch", "value": "fix-x"}, ops)

    def test_a_helm_release_moves_its_values_not_its_chart(self):
        self.assertEqual(trial.patch(self.HELM, "b")[0]["path"], "/spec/sources/1/targetRevision")
        self.assertNotIn({"op": "add", "path": "/metadata/labels", "value": {}}, trial.patch(self.HELM, "b"))

    def test_main_removes_the_label_only_if_there(self):
        on = {"metadata": {"name": "o", "labels": {"brick-trial": "true"}, "annotations": {"brick-trial-branch": "b"}},
              "spec": self.APP["spec"]}
        self.assertEqual([o["op"] for o in trial.patch(on, "main")], ["replace", "remove", "remove"])
        self.assertEqual([o["op"] for o in trial.patch(self.APP, "main")], ["replace"])
