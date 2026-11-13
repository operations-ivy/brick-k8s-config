"""media/: the movies-480p CronJob and its script (stdlib unittest)."""

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


class MediaTest(unittest.TestCase):
    def test_configmap_carries_the_script(self):
        [cm] = docs(MEDIA / "movies-script.yaml")
        self.assertEqual(cm["data"]["convert-480p.sh"], (MEDIA / "convert-480p.sh").read_text(),
                         "run scripts/media-configmap")

    def test_script_parses(self):
        result = subprocess.run(["bash", "-n", str(MEDIA / "convert-480p.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cronjob_is_gentle_and_pinned(self):
        by_kind = {d["kind"]: d for d in docs(MEDIA / "movies.yaml")}
        cron = by_kind["CronJob"]["spec"]
        pod = cron["jobTemplate"]["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        self.assertEqual(pod["priorityClassName"], by_kind["PriorityClass"]["metadata"]["name"])
        self.assertLess(by_kind["PriorityClass"]["value"], 0)
        self.assertEqual(by_kind["PriorityClass"]["preemptionPolicy"], "Never")
        self.assertEqual(container["resources"]["limits"]["cpu"], "2")
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "brick2000"})
        [movies] = [v for v in pod["volumes"] if "hostPath" in v]
        self.assertEqual(movies["hostPath"]["path"], "/extra/movies")

    def test_deployed_by_cluster_manifests(self):
        app = yaml.safe_load((ROOT / "argocd/apps/cluster-manifests.yaml").read_text())
        self.assertIn("media/*.yaml", app["spec"]["source"]["directory"]["include"])


if __name__ == "__main__":
    unittest.main()
