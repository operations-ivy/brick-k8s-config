"""node-health/wifi-watchdog.yaml: the DaemonSet and its inline script (stdlib unittest)."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "node-health" / "wifi-watchdog.yaml"

FAKE_IP = '#!/bin/sh\n[ -e "$FAKE/no-route" ] || echo "default via 192.168.1.1 dev wlan0 proto dhcp"\n'
# Each ping call takes the next line of ping-results (0 answers, 1 doesn't);
# past the end, the gateway answers.
FAKE_PING = """#!/bin/sh
n=$(cat "$FAKE/ping-calls" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FAKE/ping-calls"
code=$(sed -n "${n}p" "$FAKE/ping-results")
exit ${code:-0}
"""
FAKE_MODPROBE = '#!/bin/sh\necho "modprobe $*" >> "$FAKE/modprobe.log"\n'


def daemonset() -> dict:
    [ds] = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return ds


def container() -> dict:
    [c] = daemonset()["spec"]["template"]["spec"]["containers"]
    return c


def script() -> str:
    return container()["args"][0]


def run(ping_results: list[int], checks: int, holders: tuple[str, ...] = ("brcmfmac_cyw",),
        no_route: bool = False) -> tuple[list[str], str]:
    """Run the script for `checks` checks; return the modprobe calls and its log."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        for name, body in (("ip", FAKE_IP), ("ping", FAKE_PING), ("modprobe", FAKE_MODPROBE),
                           ("sleep", "#!/bin/sh\n")):
            (fake / name).write_text(body)
            (fake / name).chmod(0o755)
        (fake / "ping-results").write_text("".join(f"{r}\n" for r in ping_results))
        if no_route:
            (fake / "no-route").touch()
        for h in holders:
            (fake / "modules" / "brcmfmac" / "holders" / h).mkdir(parents=True)
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", FAKE=str(fake),
                   MODULES=str(fake / "modules"), CHECKS=str(checks))
        result = subprocess.run(["bash", "-c", script()], capture_output=True, text=True, env=env, timeout=30)
        modprobe_log = fake / "modprobe.log"
        calls = modprobe_log.read_text().splitlines() if modprobe_log.exists() else []
    return calls, result.stdout + result.stderr


RELOAD = ["modprobe -r brcmfmac_cyw brcmfmac", "modprobe brcmfmac"]


class WatchdogScriptTest(unittest.TestCase):
    def test_healthy_never_reloads(self):
        calls, log = run([], checks=60)
        self.assertEqual(calls, [])
        self.assertNotIn("down", log)

    def test_three_minutes_down_reloads_once(self):
        calls, log = run([1] * 12, checks=13)
        self.assertEqual(calls, RELOAD)
        self.assertIn("wlan0 is down", log)
        self.assertIn("wlan0 is back", log)

    def test_eleven_failed_checks_do_not_reload(self):
        calls, _ = run([1] * 11, checks=20)
        self.assertEqual(calls, [])

    def test_a_blip_resets_the_count(self):
        calls, _ = run([1] * 11 + [0] + [1] * 11, checks=30)
        self.assertEqual(calls, [])

    def test_still_down_waits_out_the_cooldown(self):
        # Reloads at check 12; the next may come only after 40 more checks.
        calls, _ = run([1] * 100, checks=51)
        self.assertEqual(calls, RELOAD)
        calls, _ = run([1] * 100, checks=52)
        self.assertEqual(calls, RELOAD * 2)

    def test_no_default_route_counts_as_down(self):
        calls, _ = run([], checks=12, no_route=True)
        self.assertEqual(calls, RELOAD)

    def test_bookworm_holder(self):
        calls, _ = run([1] * 12, checks=12, holders=("brcmfmac_wcc",))
        self.assertEqual(calls, ["modprobe -r brcmfmac_wcc brcmfmac", "modprobe brcmfmac"])

    def test_no_holders(self):
        calls, log = run([1] * 12, checks=12, holders=())
        self.assertEqual(calls, ["modprobe -r brcmfmac", "modprobe brcmfmac"])
        self.assertIn("holders: none", log)


class WatchdogManifestTest(unittest.TestCase):
    def test_runs_on_every_node_and_stays_on_a_notready_one(self):
        spec = daemonset()["spec"]["template"]["spec"]
        self.assertEqual(spec["tolerations"], [{"operator": "Exists"}])
        self.assertTrue(spec["hostPID"])
        self.assertTrue(container()["securityContext"]["privileged"])

    def test_enters_the_hosts_namespaces(self):
        self.assertEqual(container()["command"][:3], ["nsenter", "-t", "1"])

    def test_script_has_no_kubernetes_variable_references(self):
        # Kubernetes replaces $(NAME) in args with an env var of that name.
        self.assertEqual(re.findall(r"\$\([A-Za-z_][A-Za-z0-9_]*\)", script()), [])

    def test_argo_cd_deploys_it(self):
        app = yaml.safe_load((ROOT / "argocd" / "apps" / "cluster-manifests.yaml").read_text())
        self.assertIn("node-health/*.yaml", app["spec"]["source"]["directory"]["include"])


if __name__ == "__main__":
    unittest.main()
