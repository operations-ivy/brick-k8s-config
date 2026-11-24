"""cameras/: lidlesseye's snapshot CronJob and its script (stdlib unittest)."""

import datetime
import functools
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CAMERAS = ROOT / "cameras" / "cameras.yaml"


@functools.cache
def docs() -> dict[str, dict]:
    return {d["kind"]: d for d in yaml.safe_load_all(CAMERAS.read_text())}


class Manifests(unittest.TestCase):
    def test_deployed_by_cluster_manifests(self):
        app = yaml.safe_load((ROOT / "argocd/apps/cluster-manifests.yaml").read_text())
        self.assertIn("cameras/*.yaml", app["spec"]["source"]["directory"]["include"])

    def test_the_job_writes_to_extra_on_brick2000_with_a_pinned_image(self):
        pod = docs()["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "brick2000"})
        [cameras] = [v for v in pod["volumes"] if "hostPath" in v]
        self.assertEqual(cameras["hostPath"]["path"], "/extra/cameras")
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)  # /extra is exFAT, uid 1000's

    def test_suspended_until_the_user_starts_it(self):
        self.assertTrue(docs()["CronJob"]["spec"]["suspend"])

    def test_it_reaches_only_dns_and_the_cameras_rtsp(self):
        egress = docs()["NetworkPolicy"]["spec"]["egress"]
        ports = sorted(p["port"] for rule in egress for p in rule["ports"])
        self.assertEqual(ports, [53, 53, 8554])


class Script(unittest.TestCase):
    """snapshot.sh from the ConfigMap, with a stand-in ffmpeg."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dest = self.tmp / "lidlesseye"
        self.script = self.tmp / "snapshot.sh"
        self.script.write_text(docs()["ConfigMap"]["data"]["snapshot.sh"])

    def ffmpeg(self, body: str) -> None:
        f = self.bin / "ffmpeg"
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def run_script(self) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": os.pathsep.join([str(self.bin), os.environ["PATH"]]), "STREAM": "rtsp://cam/cam",
               "DEST": str(self.dest), "KEEP_DAYS": "14"}
        return subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True)

    def test_a_snapshot_lands_by_day_and_as_latest(self):
        self.ffmpeg('for last; do :; done; printf jpeg > "$last"\n')  # writes its last argument
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        [day] = [d for d in self.dest.iterdir() if d.is_dir()]
        [snap] = list(day.glob("*.jpg"))
        self.assertRegex(snap.name, r"^\d{4}\.jpg$")  # HHMM: no colons on exFAT
        self.assertEqual((self.dest / "latest.jpg").read_text(), "jpeg")
        self.assertEqual(list(day.glob(".*")), [])  # no .part left behind

    def test_a_camera_thats_off_is_a_skip_not_a_failure(self):
        self.ffmpeg("exit 1\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertIn("snapshot skipped", result.stdout)
        self.assertFalse((self.dest / "latest.jpg").exists())

    def test_days_past_keep_days_are_pruned(self):
        self.ffmpeg('for last; do :; done; printf jpeg > "$last"\n')
        old = (datetime.date.today() - datetime.timedelta(days=20)).isoformat()
        kept = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        for d in (old, kept):
            (self.dest / d).mkdir(parents=True)
        result = self.run_script()
        self.assertIn(f"pruned {old}", result.stdout)
        self.assertFalse((self.dest / old).exists())
        self.assertTrue((self.dest / kept).exists())


if __name__ == "__main__":
    unittest.main()
