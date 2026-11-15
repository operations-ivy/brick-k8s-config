"""media/: the movies-480p CronJob and its script (stdlib unittest)."""

import importlib.util
import types
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def audio_track(probe_output: str) -> str:
    """Run the script's audio_track with a fake ffprobe that prints probe_output."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "out").write_text(probe_output)
        fake = Path(tmp) / "ffprobe"
        fake.write_text('#!/bin/sh\ncat "$(dirname "$0")/out"\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}")
        result = subprocess.run(["bash", "-c", 'source "$1"; audio_track x.mkv', "bash", str(MEDIA / "convert-480p.sh")],
                                capture_output=True, text=True, env=env)
    return result.stdout.strip()


class MediaTest(unittest.TestCase):
    def test_configmap_carries_the_scripts(self):
        [cm] = docs(MEDIA / "movies-script.yaml")
        for name in ("convert-480p.sh", "convert-server.py", "convert-worker.sh"):
            with self.subTest(script=name):
                self.assertEqual(cm["data"][name], (MEDIA / name).read_text(), "run scripts/media-configmap")

    def test_scripts_parse(self):
        for name in ("convert-480p.sh", "convert-worker.sh"):
            with self.subTest(script=name):
                result = subprocess.run(["bash", "-n", str(MEDIA / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_workers_are_gentle_and_fenced(self):
        by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs(MEDIA / "convert-workers.yaml")}
        server = by_name[("Deployment", "movies-convert-server")]["spec"]["template"]["spec"]
        worker = by_name[("Deployment", "movies-convert-worker")]["spec"]["template"]["spec"]
        self.assertEqual(server["nodeSelector"], {"kubernetes.io/hostname": "brick2000"})
        self.assertEqual(worker["nodeSelector"], {"kubernetes.io/hostname": "brick666"})
        for pod in (server, worker):
            self.assertEqual(pod["priorityClassName"], "media-batch")
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        # Only the workers reach the server.
        ingress = by_name[("NetworkPolicy", "movies-convert-server")]["spec"]["ingress"]
        self.assertEqual(ingress[0]["from"], [{"podSelector": {"matchLabels": {"app": "movies-convert-worker"}}}])

    def test_english_audio_is_chosen(self):
        cases = (("1,jpn\n2,eng\n", "0:2"), ("1,ENG\n2,jpn\n", "0:1"),
                 # No English track: nothing, so encode falls back to the first audio track.
                 ("1,jpn\n", ""), ("1\n", ""))
        for probe, expected in cases:
            with self.subTest(probe=probe):
                self.assertEqual(audio_track(probe), expected)

    def test_cronjob_is_gentle_and_pinned(self):
        by_kind = {d["kind"]: d for d in docs(MEDIA / "movies.yaml")}
        cron = by_kind["CronJob"]["spec"]
        pod = cron["jobTemplate"]["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        self.assertEqual(pod["priorityClassName"], by_kind["PriorityClass"]["metadata"]["name"])
        self.assertLess(by_kind["PriorityClass"]["value"], 0)
        self.assertEqual(by_kind["PriorityClass"]["preemptionPolicy"], "Never")
        # As many threads as cores, and never more than brick2000's four.
        [threads] = [e["value"] for e in container["env"] if e["name"] == "THREADS"]
        self.assertEqual(container["resources"]["limits"]["cpu"], threads)
        self.assertLessEqual(int(threads), 4)
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "brick2000"})
        [movies] = [v for v in pod["volumes"] if "hostPath" in v]
        self.assertEqual(movies["hostPath"]["path"], "/extra/movies")

    def test_deployed_by_cluster_manifests(self):
        app = yaml.safe_load((ROOT / "argocd/apps/cluster-manifests.yaml").read_text())
        self.assertIn("media/*.yaml", app["spec"]["source"]["directory"]["include"])



def load_server(src: Path, dst: Path) -> types.ModuleType:
    """convert-server.py as a module, pointed at temporary folders."""
    spec = importlib.util.spec_from_file_location("convert_server", MEDIA / "convert-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SRC, module.DST, module.SETTLE = src, dst, 0
    return module


class ConvertServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src, self.dst = Path(tmp.name) / "library", Path(tmp.name) / "480p"
        for rel in ("show/A - 01.mkv", "show/A - 02.mkv", "show/A - 03.mkv", "show/B - 01.mp4", "show/notes.txt",
                    "show/.hidden.mkv"):
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.src / rel).write_bytes(b"video " + rel.encode())
        self.server = load_server(self.src, self.dst)

    def test_pending_skips_done_failed_and_in_progress_newest_name_first(self):
        (self.dst / "show").mkdir(parents=True)
        (self.dst / "show/A - 01.mp4").write_bytes(b"done")
        (self.dst / "show/A - 02.mp4.failed").touch()
        (self.dst / "show/.A - 03.mp4.part").touch()  # the CronJob is on it
        self.assertEqual(self.server.pending(time.time()), ["show/B - 01.mp4"])

    def test_mpeg_files_are_converted_too(self):
        (self.src / "show/D - 01.mpg").write_bytes(b"mpeg-2")
        (self.src / "show/D - 02.MPEG").write_bytes(b"mpeg-2")
        pending = self.server.pending(time.time())
        self.assertIn("show/D - 01.mpg", pending)
        self.assertIn("show/D - 02.MPEG", pending)
        self.assertIn("-iname '*.mpg' -o -iname '*.mpeg'", (MEDIA / "convert-480p.sh").read_text())

    def test_each_claim_takes_a_different_video(self):
        first, second = self.server.claim(time.time()), self.server.claim(time.time())
        self.assertNotEqual(first, second)
        self.assertEqual(self.server.claimed_rel(first), "show/B - 01.mp4")
        self.assertEqual(self.server.claimed_rel(second), "show/A - 03.mkv")
        self.assertIsNone(self.server.claimed_rel("../../etc/passwd"))

    def test_a_stale_claim_is_handed_out_again(self):
        cid = self.server.claim(time.time())
        later = time.time() + self.server.CLAIM_HOURS * 3600 + 60
        self.assertIn(self.server.claimed_rel(cid), self.server.pending(later))

    def test_claim_fetch_and_return_over_http(self):
        httpd = self.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(urllib.request.Request(base + "/claim", method="POST")) as r:
            cid = r.read().decode()
        with urllib.request.urlopen(f"{base}/file/{cid}") as r:
            self.assertEqual(r.read(), b"video show/B - 01.mp4")
        with urllib.request.urlopen(urllib.request.Request(f"{base}/done/{cid}", data=b"480p copy", method="PUT")):
            pass
        self.assertEqual((self.dst / "show/B - 01.mp4").read_bytes(), b"480p copy")
        self.assertFalse((self.dst / ".claims" / cid).exists())
        self.assertNotIn("show/B - 01.mp4", self.server.pending(time.time()))


if __name__ == "__main__":
    unittest.main()
