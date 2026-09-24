#!/usr/bin/env python3
"""Keep only the newest N versions of our app images, everywhere they pile up.

No compliance requirements here, so retention is "current + previous" (N=2)
by default. Kubelet's image GC is disk-pressure based and Docker Hub has no
retention policy on this plan, so neither can express "keep the last two
versions" on its own. This does it explicitly, across:

  - Docker Hub          tags on each repo
  - k3s nodes           containerd's image store (via `k3s crictl`, over SSH)
  - this machine        local docker images, including unprefixed compose tags

Versions are ordered by semver (1.0.10 > 1.0.9); non-version tags (latest,
native-test...) are ignored. An image a node is still running can't be
removed there; that's reported, not forced.

Dry run by default:
    scripts/prune_images.py            # show what would go
    scripts/prune_images.py --apply    # actually delete

Docker Hub deletes use DOCKERHUB_USERNAME + DOCKERHUB_TOKEN (a personal access
token with delete scope) when set, otherwise the access token `docker login`
left in ~/.docker/config.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

NAMESPACE = "whitepatrick"
REPOS = ["wigle-sync", "wigle-console", "joke-reader", "joke-importer"]
NODES = ["192.168.1.183", "192.168.1.170"]  # brick420, brick2000
SSH_USER = "zaphod"
HUB = "https://hub.docker.com/v2"
VERSION = re.compile(r"^\d+(\.\d+)*$")


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in tag.split("."))


def stale(tags: set[str], keep: int, newest: list[str]) -> list[str]:
    """Version tags not among the repo's newest `keep` versions (as published on Hub)."""
    keepers = set(newest[:keep])
    return sorted((t for t in tags if VERSION.match(t) and t not in keepers), key=version_key)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------- Docker Hub
def hub_request(method: str, url: str, token: str | None = None, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def hub_tags(repo: str) -> list[str]:
    tags, url = [], f"{HUB}/repositories/{NAMESPACE}/{repo}/tags?page_size=100"
    while url:
        page = hub_request("GET", url)
        tags += [t["name"] for t in page.get("results", [])]
        url = page.get("next")
    return tags


def hub_token() -> str:
    user, pat = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN")
    if user and pat:
        return hub_request("POST", f"{HUB}/users/login", body={"username": user, "password": pat})["token"]
    auths = json.load(open(os.path.expanduser("~/.docker/config.json"))).get("auths", {})
    entry = auths.get("https://index.docker.io/v1/access-token", {}).get("auth")
    if not entry:
        sys.exit("No Docker Hub credentials: set DOCKERHUB_USERNAME/DOCKERHUB_TOKEN or `docker login`.")
    return base64.b64decode(entry).decode().split(":", 1)[1]


# ---------------------------------------------------------------- k3s nodes
def node_images(node: str) -> dict[str, set[str]]:
    out = run(["ssh", "-o", "BatchMode=yes", f"{SSH_USER}@{node}", "sudo -n k3s crictl images -o json"])
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    found: dict[str, set[str]] = {}
    for image in json.loads(out.stdout)["images"]:
        for ref in image.get("repoTags") or []:
            name, _, tag = ref.rpartition(":")
            repo = name.removeprefix(f"docker.io/{NAMESPACE}/")
            if repo in REPOS:
                found.setdefault(repo, set()).add(tag)
    return found


def node_rmi(node: str, repo: str, tag: str) -> str | None:
    ref = f"docker.io/{NAMESPACE}/{repo}:{tag}"
    out = run(["ssh", "-o", "BatchMode=yes", f"{SSH_USER}@{node}", f"sudo -n k3s crictl rmi {ref}"])
    return None if out.returncode == 0 else out.stderr.strip()


# ---------------------------------------------------------------- local
def local_images() -> dict[str, set[str]]:
    out = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    found: dict[str, set[str]] = {}
    for ref in out.stdout.split():
        name, _, tag = ref.rpartition(":")
        repo = name.removeprefix(f"{NAMESPACE}/")  # compose builds tag without the namespace
        if repo in REPOS:
            found.setdefault(repo, set()).add(f"{name}:{tag}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", type=int, default=2, help="versions to keep per repo (default 2)")
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = parser.parse_args()
    verb = "deleted" if args.apply else "would delete"
    problems = 0

    # Hub is the source of truth for what "newest" means per repo.
    newest = {repo: sorted((t for t in hub_tags(repo) if VERSION.match(t)), key=version_key, reverse=True)
              for repo in REPOS}
    for repo in REPOS:
        print(f"{repo}: keeping {newest[repo][:args.keep]}")

    print("\n== Docker Hub")
    token = hub_token() if args.apply else None
    for repo in REPOS:
        for tag in stale(set(newest[repo]), args.keep, newest[repo]):
            if args.apply:
                try:
                    hub_request("DELETE", f"{HUB}/repositories/{NAMESPACE}/{repo}/tags/{tag}/", token)
                except urllib.error.HTTPError as e:
                    problems += 1
                    print(f"  FAILED {repo}:{tag}: HTTP {e.code}")
                    continue
            print(f"  {verb} {repo}:{tag}")

    for node in NODES:
        print(f"\n== node {node}")
        try:
            images = node_images(node)
        except RuntimeError as e:
            problems += 1
            print(f"  unreachable: {e}")
            continue
        for repo, tags in images.items():
            for tag in stale(tags, args.keep, newest[repo]):
                err = node_rmi(node, repo, tag) if args.apply else None
                if err:
                    problems += 1
                    print(f"  kept {repo}:{tag} ({err})")
                else:
                    print(f"  {verb} {repo}:{tag}")

    print("\n== local docker")
    for repo, refs in local_images().items():
        for ref in sorted(refs):
            tag = ref.rpartition(":")[2]
            if VERSION.match(tag) and tag not in newest[repo][: args.keep]:
                if args.apply and run(["docker", "rmi", ref]).returncode:
                    problems += 1
                    print(f"  kept {ref} (in use)")
                    continue
                print(f"  {verb} {ref}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
