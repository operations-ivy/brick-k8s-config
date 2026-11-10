"""The timeline's live half: every merged pull request of the public
operations-ivy repos, a TimelineJS3 slide per repo per week, written as
feed.json into the ConfigMap timeline-feed, which Caddy serves next to the
page (timeline.brick.nozdormu.cloud). The curated milestones are the page's
other half (timeline-history.yaml).

Run hourly by the CronJob timeline-feed (front-door/timeline.yaml) on a stock
Python image: standard library only. Anonymous GitHub API calls (60 an hour;
this makes about a dozen). If any repo can't be read, nothing is written: the
page keeps the last good feed. Pushes its health to the Pushgateway.

    python3 feed.py            # in the cluster: fetch, write the ConfigMap, push metrics
    python3 feed.py --stdout   # anywhere: print feed.json
"""

import argparse
import html
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

OWNER = "operations-ivy"
# The public repos only: the private ones aren't readable without a token.
REPOS = ["brick-k8s-config", "brick-cicd-config", "wigle-sync", "brick-observe", "chucks-wisdom",
         "overwatch", "home-assistant", "rpi_lcars", "presto"]
GROUP = "Pull requests"
API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # 1,000 pull requests a repo: far more than any has

NAMESPACE = "front-door"
CONFIGMAP = "timeline-feed"
KUBE = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
PUSHGATEWAY = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway-prometheus-pushgateway.monitoring:9091")


def get_json(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "brick-timeline-feed", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as reply:
        return json.load(reply)


def merged_pulls(repo: str, fetch=get_json) -> list[dict]:
    """Every merged pull request of one repo: number, title, url, merged (a datetime)."""
    found = []
    for page in range(1, MAX_PAGES + 1):
        batch = fetch(f"{API}/repos/{OWNER}/{repo}/pulls?state=closed&per_page={PER_PAGE}&page={page}")
        found += [{"number": p["number"], "title": p["title"], "url": p["html_url"],
                   "merged": datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))}
                  for p in batch if p.get("merged_at")]
        if len(batch) < PER_PAGE:
            break
    return found


def week_of(when: datetime) -> date:
    """The Monday of the week a merge happened in."""
    day = when.date()
    return day - timedelta(days=day.weekday())


def slides(pulls: dict[str, list[dict]]) -> list[dict]:
    """A TimelineJS3 slide per repo per week with merges, newest pull requests first."""
    weeks: dict[tuple[str, date], list[dict]] = {}
    for repo, prs in pulls.items():
        for pr in prs:
            weeks.setdefault((repo, week_of(pr["merged"])), []).append(pr)
    events = []
    for (repo, monday), prs in sorted(weeks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        prs.sort(key=lambda p: p["merged"], reverse=True)
        items = "".join(f'<li><a href="{html.escape(p["url"])}" target="_blank" rel="noopener">#{p["number"]}</a> '
                        f'{html.escape(p["title"])}</li>' for p in prs)
        count = f'{len(prs)} pull request{"s" if len(prs) != 1 else ""} merged'
        events.append({
            "start_date": {"year": monday.year, "month": monday.month, "day": monday.day},
            "display_date": f"Week of {monday:%b} {monday.day}, {monday.year}",
            "group": GROUP,
            "unique_id": f"pr-{repo}-{monday.isoformat()}",
            "text": {"headline": f"{html.escape(repo)}: {count}", "text": f"<ul>{items}</ul>"},
        })
    return events


def feed(fetch=get_json, now: float | None = None) -> dict:
    pulls = {repo: merged_pulls(repo, fetch) for repo in REPOS}
    return {
        "generated": datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(timespec="seconds"),
        "pull_requests": {repo: len(prs) for repo, prs in pulls.items()},
        "events": slides(pulls),
    }


def kube(method: str, path: str, body: dict | None = None) -> int:
    """One call to the Kubernetes API as this pod's ServiceAccount; the HTTP status."""
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    req = urllib.request.Request(f"{KUBE}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as reply:
            return reply.status
    except urllib.error.HTTPError as e:
        return e.code


def write_configmap(text: str) -> None:
    """Create or replace the ConfigMap Caddy serves feed.json from."""
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIGMAP, "namespace": NAMESPACE,
                         "labels": {"app.kubernetes.io/name": "timeline-feed"}},
            "data": {"feed.json": text}}
    base = f"/api/v1/namespaces/{NAMESPACE}/configmaps"
    status = kube("PUT", f"{base}/{CONFIGMAP}", body)
    if status == 404:
        status = kube("POST", base, body)
    if status not in (200, 201):
        raise RuntimeError(f"writing ConfigMap {CONFIGMAP}: HTTP {status}")


def push_metrics(data: dict, ok: bool) -> None:
    """Best effort: the job's result, for Grafana and alerts."""
    lines = [f"timeline_feed_success {1 if ok else 0}"]
    if ok:
        lines += [f"timeline_feed_last_success_timestamp_seconds {time.time():.0f}",
                  f"timeline_feed_slides {len(data['events'])}"]
        lines += [f'timeline_feed_pull_requests{{repo="{r}"}} {n}' for r, n in data["pull_requests"].items()]
    req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/timeline-feed", method="PUT",
                                 data=("\n".join(lines) + "\n").encode())
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as e:
        print(f"timeline-feed: no metrics pushed: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stdout", action="store_true", help="print feed.json instead of writing the ConfigMap")
    args = parser.parse_args()
    try:
        data = feed()
    except (OSError, ValueError, KeyError) as e:
        print(f"timeline-feed: GitHub unreadable, the last feed stays: {e}", file=sys.stderr)
        if not args.stdout:
            push_metrics({}, ok=False)
        return 1
    text = json.dumps(data, separators=(",", ":"))
    if args.stdout:
        print(text)
        return 0
    write_configmap(text)
    push_metrics(data, ok=True)
    print(f"timeline-feed: {len(data['events'])} slides from {sum(data['pull_requests'].values())} pull requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
