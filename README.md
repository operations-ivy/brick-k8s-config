# brick-k8s-config

Cluster-level infrastructure for the k3s cluster running on 3 Raspberry Pi
5s and a Pi 4 (`brick420` control-plane; `brick2000`, `brick666` and
`brick1982` workers). Anything specific to the
`chucks-wisdom` app itself (Postgres, reader, importer) lives in that repo,
not here — this repo covers the cluster and would stay useful even if a
different app replaced chuck.

The steps below reflect the **current** live setup (k3s's built-in Traefik
ingress, no Portainer/ingress-nginx) — this replaces an earlier version of
this README from before the Pi 5 redeploy.

## MAIN NODE (control-plane)

```bash
ssh zaphod@<main-node-ip>

curl -sfL https://get.k3s.io | sudo sh -s - server \
  --tls-san <main-node-ip> \
  --write-kubeconfig-mode 644
```

k3s ships Traefik and ServiceLB by default — leave them enabled.

```bash
sudo cat /var/lib/rancher/k3s/server/node-token   # needed by the worker
sudo k3s kubectl get nodes
```

## AGENT NODE (worker)

On a fresh Raspberry Pi OS (64-bit) install with SSH key login and
passwordless sudo, and a DHCP reservation for its address:

1. Turn on memory cgroups. The Pi kernel disables them by default
   (`cgroup_disable=memory` in `/proc/cmdline`), and k3s won't run without
   them. Append to the single line of `/boot/firmware/cmdline.txt`, then
   reboot and check `/sys/fs/cgroup/cgroup.controllers` lists `memory`:

   ```bash
   sudo sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' /boot/firmware/cmdline.txt
   sudo reboot
   ```

2. Pin the node to its IPv4 address before installing (see below for why):

   ```bash
   echo 'node-ip: <this-node-ip>' | sudo install -D -m 644 /dev/stdin /etc/rancher/k3s/config.yaml
   ```

3. Install the agent at the cluster's exact version (`kubectl get nodes`
   shows it). From your workstation, piping the token straight from the
   control plane so it's never printed:

   ```bash
   ssh <main-node> 'sudo cat /var/lib/rancher/k3s/server/node-token' \
     | ssh <worker-node> 'read -r T; curl -sfL https://get.k3s.io \
         | sudo INSTALL_K3S_VERSION=<version> K3S_URL=https://<main-node-ip>:6443 K3S_TOKEN="$T" sh -s - agent'
   kubectl wait --for=condition=Ready node/<worker-node> --timeout=180s
   ```

The DaemonSets (node-exporter, promtail, Traefik's svclb) start on it by
themselves, and brick9000's board picks it up from Prometheus. Running pods
don't move to a new node on their own; to spread stateless ones onto it,
cordon the other nodes, `kubectl rollout restart` the chosen deployments, and
uncordon. Leave the pods with local-path volumes where they are.

`brick666` (the former wardriving Pi 5, SD card) joined this way on
2026-10-08, and `brick1982` (a Pi 4 8GB, SD card, with the 7" touchscreen) on
2026-10-10. Some of the old k3s add-ons (CoreDNS, local-path-provisioner,
Traefik) tolerate the control-plane taint, so cordon `brick420` too when
moving them, or they land there.

brick1982 differs from the Pi 5 workers in two ways, set by hand:

- **It runs a kiosk outside Kubernetes** (cage and Chromium for brick-arena,
  see brick-cicd-config). Its `/etc/rancher/k3s/config.yaml` reserves room
  for that, so the scheduler doesn't hand it to pods, and `k3s-agent` was
  restarted after (running pods carry on):

  ```yaml
  kubelet-arg:
    - "system-reserved=cpu=500m,memory=1Gi"
  ```

- **Its cores are slower** (Cortex-A72, about a third of a Pi 5's per core),
  which Kubernetes can't see: `100m` is `100m` on any node. It's labelled
  `chuck.io/cpu=pi4` (`kubectl label node brick1982 chuck.io/cpu=pi4`) so a
  CPU-hungry pod can prefer the Pi 5s with a node affinity. Nothing uses the
  label yet; tight CPU limits are the thing to watch (Grafana's 200m one
  crash-looped it there, see `monitoring/`).

## Keep workloads off the control plane

The single control plane runs on an SD card over Wi-Fi with a SQLite
datastore, so a busy pod there can stall the API server for the whole
cluster. It's tainted so only DaemonSets (which tolerate it) run there:

```bash
kubectl taint node brick420 node-role.kubernetes.io/control-plane=:NoSchedule
```

Applied 2026-10-08, after moving the Pushgateway's volume off it. A pod with a
local-path volume on a tainted node would stay `Pending` after its next
restart, so check `kubectl get pv` first. Keep write-heavy volumes
(Prometheus, Loki, Tempo) on `brick2000`, the only NVMe node.

## Pin each node to its IPv4 address

Both nodes also get a global IPv6 address from the ISP's delegated prefix, and
k3s registers it as a second node IP unless told otherwise. The prefix changes
after an ISP outage (it did on 2026-09-29), leaving the nodes registered with
an address they no longer have: kubelet then logs `failed to validate
secondaryNodeIP` every 10s. The cluster is IPv4-only (pod CIDRs, flannel,
services), so pin each node to its DHCP reservation. On each node, with its
own address:

```bash
echo 'node-ip: <this-node-ip>' | sudo tee /etc/rancher/k3s/config.yaml
sudo systemctl restart k3s         # k3s-agent on the worker
```

Pods keep running across the restart; the API is down for under a minute.
Applied to both nodes on 2026-09-30.

## Verify

```bash
sudo k3s kubectl get nodes -o wide   # INTERNAL-IP is IPv4 only
```

## LOCALHOST

Pull the kubeconfig to your workstation:

```bash
ssh zaphod@<main-node-ip> sudo cat /etc/rancher/k3s/k3s.yaml \
  | sed "s/127.0.0.1/<main-node-ip>/" > ~/.kube/chuck-config

KUBECONFIG=~/.kube/chuck-config kubectl get nodes
```

## Repo layout

- `dashboard/` — Kubernetes Dashboard (v2.7.0, last release with a static
  manifest) + an `admin-user` service account. Deploy:

  ```bash
  kubectl apply -f dashboard/dashboard.yaml
  kubectl apply -f dashboard/dashboard-admin-user.yaml
  kubectl apply -f dashboard/dashboard-ingress.yaml
  ```

  Exposed on the LAN at `https://dashboard.brick.nozdormu.cloud` (see "LAN
  names for ingresses" below). `dashboard-ingress.yaml`
  is a Traefik `IngressRoute` (not a plain `networking.k8s.io/Ingress` —
  the `service.serverstransport` annotation on a plain Ingress silently
  doesn't apply on this Traefik v3 build, so `IngressRoute`'s native
  `serversTransport` field is used instead) referencing a `ServersTransport`
  with `insecureSkipVerify`, since the dashboard's backend only speaks
  HTTPS with a self-signed cert. The route itself runs on Traefik's
  `websecure` entrypoint with `tls: {}` (Traefik's own default self-signed
  cert) — plain HTTP won't work here regardless of backend config, since
  the Dashboard's frontend refuses to allow sign-in unless served over
  HTTPS or from `localhost`. Expect a browser cert warning to click
  through (self-signed, same as the tunnel approach this replaced). Mint
  a login token from the control-plane node:

  ```bash
  ssh zaphod@<main-node-ip> 'sudo k3s kubectl -n kubernetes-dashboard create token admin-user'
  ```

- `monitoring/` — `kube-prometheus-stack` Helm values, **installed** (2026-09-21).
  Apply the namespace once, then install/upgrade with `helm upgrade --install`,
  which is idempotent, rather than a one-shot `helm install`:

  ```bash
  kubectl apply -f monitoring/monitoring-namespace.yaml
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  helm repo update
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --namespace monitoring -f monitoring/kube-prometheus-stack/kube-prometheus-stack-values.yaml
  ```

  Grafana is exposed on the LAN via a plain Traefik `Ingress` (`monitoring/grafana-ingress.yaml`,
  applied separately — `kubectl apply -f monitoring/grafana-ingress.yaml`) at
  `https://grafana.brick.nozdormu.cloud` (see "LAN names for ingresses"
  below;
  no `IngressRoute`/`ServersTransport` needed here, unlike the dashboard,
  since Grafana serves plain HTTP rather than self-signed HTTPS). Login is
  admin / the hardcoded `adminPassword` in the values file — see the repo's
  secrets-management todo. Any app repo can auto-register a dashboard by
  applying a `ConfigMap` labeled `grafana_dashboard: "1"` in its own namespace
  (the sidecar watches cluster-wide); `chucks-wisdom` does this for the
  importer's dashboard.

  Prometheus itself is exposed the same way at
  `https://prometheus.brick.nozdormu.cloud`
  (`monitoring/prometheus-ingress.yaml`, applied separately), so
  `brick-status` on brick9000 (see `brick-cicd-config`) can query it. It has
  no login, so anyone on the LAN can run queries; that was a deliberate
  choice. An IP allowlist (Traefik `ipAllowList` middleware) does **not** work
  here: Traefik's service uses `externalTrafficPolicy: Cluster`, so k3s's
  ServiceLB rewrites every client's source address before Traefik sees it, and
  the allowlist blocks everyone.

- `monitoring/loki/` — Grafana Loki (single-binary mode, filesystem storage,
  4-day retention via the compactor), **installed** (2026-09-21). Deliberately
  minimal for this cluster's size: SimpleScalable components (`read`/`write`/
  `backend`), the nginx `gateway`, memcached-based caching, MinIO, the test
  suite, and the canary are all disabled — this is a single pod writing to a
  10Gi PVC, scheduled on `brick2000` (`chuck.io/storage-node=true`, same as
  Postgres) so it doesn't compete with `brick420`'s small SD card. Install:

  ```bash
  helm repo add grafana https://grafana.github.io/helm-charts
  helm repo update
  helm upgrade --install loki grafana/loki --version 7.3.0 \
    --namespace monitoring -f monitoring/loki/loki-values.yaml
  ```

- `monitoring/tempo/` — Grafana Tempo (single-binary mode, filesystem
  storage, 4-day retention via the compactor), same minimal single-pod
  pattern as Loki, on `brick2000`. Install:

  ```bash
  helm upgrade --install tempo grafana/tempo --version 1.24.4 \
    --namespace monitoring -f monitoring/tempo/tempo-values.yaml
  ```

- `monitoring/otel-collector/` — OpenTelemetry Collector (`deployment` mode,
  not `daemonset` — it just receives OTLP over the network from app pods,
  no host-level log/metric collection). Traces-only: the receivers/pipelines
  for logs and metrics are nulled out, and the only exporter is `otlp` to
  `tempo.monitoring.svc.cluster.local:4317`. importer's existing
  Prometheus-native custom metrics deliberately stay on their current scrape
  path rather than being routed through this collector too. Install:

  ```bash
  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
  helm repo update
  helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
    --namespace monitoring -f monitoring/otel-collector/otel-collector-values.yaml
  ```

  `chucks-wisdom`'s reader and importer send OTLP traces to
  `otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4317`.

- `monitoring/pushgateway/` — Prometheus Pushgateway, for batch jobs whose
  pods are gone before Prometheus would scrape them (`wigle-sync`'s hourly
  CronJob pushes to
  `http://pushgateway-prometheus-pushgateway.monitoring.svc.cluster.local:9091`).
  Scraped via the chart's `ServiceMonitor` with `honorLabels`. Install:

  ```bash
  helm upgrade --install pushgateway prometheus-community/prometheus-pushgateway \
    --namespace monitoring -f monitoring/pushgateway/pushgateway-values.yaml
  ```

- `debug/` — a node-problem-detector `DaemonSet` for `kube-system`.

- `secrets/` — the [Sealed Secrets](https://github.com/bitnami/sealed-secrets) controller, so real secret values never sit in git as plaintext. Install:

  ```bash
  kubectl apply -f secrets/sealed-secrets-namespace.yaml
  helm upgrade --install sealed-secrets oci://registry-1.docker.io/bitnamicharts/sealed-secrets --version 2.20.0 \
    --namespace sealed-secrets -f secrets/sealed-secrets/sealed-secrets-values.yaml
  ```

  **Pi note:** first-boot RSA-4096 keypair generation took ~80s on a Pi 5 node — well past the chart's default ~30s liveness/readiness budget, which killed and restarted the pod before it ever finished. `sealed-secrets-values.yaml` enables a generous `startupProbe` to fix this; if you ever see the controller stuck crash-looping right after install with no error in its logs (just `"Searching for existing private keys"` and nothing after), this is why.

  `secrets/sealed-secrets-public-cert.pem` is the controller's public cert (safe to commit — public key only), fetched via:

  ```bash
  kubeseal --controller-name=sealed-secrets --controller-namespace=sealed-secrets --fetch-cert > secrets/sealed-secrets-public-cert.pem
  ```

  **The resulting `SealedSecret` YAMLs do NOT live in this repo** — this repo
  is public, and while the ciphertext is safe by design, its safety depends
  entirely on the controller's private key never leaking for as long as git
  history exists. No reason to make that bet publicly. They live in the
  private companion repo [`brick-k8s-secrets`](https://github.com/operations-ivy/brick-k8s-secrets)
  instead, at the same relative path they'd be applied from here. To seal a
  new secret:

  ```bash
  kubectl create secret generic <name> --namespace <ns> --dry-run=client \
    --from-literal=key1=value1 --from-literal=key2=value2 -o yaml | \
  kubeseal --cert secrets/sealed-secrets-public-cert.pem --format yaml \
    > ~/code/brick-k8s-secrets/path/to/<name>-sealedsecret.yaml

  kubectl apply -f ~/code/brick-k8s-secrets/path/to/<name>-sealedsecret.yaml
  ```

  The resulting `SealedSecret` YAML is safe to commit — only the in-cluster controller can decrypt it. **Sealing a value is not a substitute for keeping a durable copy of it** (KeePass, etc.) — once sealed there's no way to read the plaintext back out except by pulling the live decrypted `Secret` off the running cluster, so save the value somewhere you control before or as you seal it, not only in git.

## LAN names for ingresses

Every web UI is at `https://<app>.brick.nozdormu.cloud` (`grafana`,
`prometheus`, `wigle`, `reader`, `dashboard`, and `jenkins`, whose Ingress
comes from its Helm release in `brick-cicd-config`), through one front door: Caddy on
brick9000 (`brick-cicd-config`, "Ports 80 and 443"). It holds a Let's Encrypt
wildcard certificate and forwards each name to Traefik on either node, which
routes by that name like any other host. So an ingress only needs its new host
name; TLS ends at brick9000, and the hop to Traefik is plain HTTP on the LAN
(the Dashboard's is HTTPS to Traefik's `websecure` entrypoint).

brick9000 is the front door rather than Traefik so that the status board,
which lives there, stays reachable when the cluster is down.

The names resolve on every device, phones included, with nothing to install:

- **Public DNS** (Porkbun): one record, `A *.brick` to `192.168.1.221`.
- **The router's DNS** drops public answers that point at private addresses
  (DNS rebinding protection), so each name also has an entry in its static DNS
  host table, pointing at `192.168.1.221`. No wildcards there: **a new ingress
  needs a router entry**, as well as its host name in the manifest.

### The last .local name

`grafana.local` still answers, because the live wigle console image links to
it; it goes once the next `wigle-console` image (with the new link) is out.
`brick420` announces it over mDNS, pointing at itself (192.168.1.183), with an
`mdns-alias@<name>` systemd unit:

```ini
# /etc/systemd/system/mdns-alias@.service on brick420
[Unit]
Description=Publish %i.local over mDNS, pointing at this node (Traefik ingress)
After=avahi-daemon.service network-online.target
Requires=avahi-daemon.service

[Service]
# -R: no reverse (PTR) record; 192.168.1.183 already reverse-resolves to brick420.local.
ExecStart=/usr/bin/avahi-publish -a -R %i.local 192.168.1.183
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

To retire it: remove the `grafana.local` rule from `monitoring/grafana-ingress.yaml`,
then on brick420 `sudo systemctl disable --now mdns-alias@grafana` (and the
unit file can go too). `wigle.local`, `reader.local`, `prometheus.local` and
`dashboard.local` are already gone.

## Native arm64 image builds

App images are built natively on `brick2000` rather than under qemu on the
laptop. A buildx builder on the laptop drives BuildKit running in a container
there over SSH, with its cache on the NVMe. One-time setup (needs `zaphod` in
the `docker` group on brick2000, which it is):

```bash
docker context create brick2000 --docker "host=ssh://zaphod@192.168.1.170"
docker buildx create --name brick2000-arm64 --driver docker-container --platform linux/arm64 \
  --buildkitd-config build/buildkitd.toml --bootstrap brick2000
```

Then build (and push, with the laptop's `docker login`) from any app repo:

```bash
docker buildx build --builder brick2000-arm64 --platform linux/arm64 \
  -t whitepatrick/<image>:<version> --push .
```

`build/buildkitd.toml` caps the build cache at 5GB (least recently used
layers go first, never below 1GB). Note that the legacy `gckeepstorage` option
maps to BuildKit's *reserved* space, a floor rather than a cap, which is why
the config uses `maxUsedSpace`. Creating the `brick2000` context also
auto-registers a `brick2000` builder that uses Docker 20.10's built-in BuildKit
v0.8; use `brick2000-arm64` instead.

## Image retention: current + previous only

No compliance requirements, so only the two newest versions of each app image
are kept: on Docker Hub, in each node's containerd image store, and locally.
Neither kubelet image GC (disk-pressure based) nor Docker Hub (no retention on
this plan) can express "keep the last two versions", so
`scripts/prune_images.py` does it explicitly. Run it after each release:

```bash
scripts/prune_images.py            # dry run: shows what would go
scripts/prune_images.py --apply    # delete
```

Versions are ordered by semver and "newest" comes from Docker Hub. An image a
node is still running can't be removed there and is reported instead. Hub
deletes use `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` if set, otherwise the token
`docker login` stored. Add new app repos to `REPOS` in the script.

## Log & trace retention policy (2026-09-21, revised to 4 days 2026-09-22)

Kept to 4 days end-to-end, at every layer, to keep storage bounded on both
Pis:

- **Loki** (`monitoring/loki/`): `limits_config.retention_period: 96h`,
  compactor-enforced.
- **Tempo** (`monitoring/tempo/`): `tempo.retention: 96h`, compactor-enforced.
- **Prometheus** (`monitoring/kube-prometheus-stack/`): `retention: 72h`
  (24h until 2026-10-01), with `retentionSize: 8GB` as the hard cap on its
  10Gi volume on `brick2000`.
- **journald**, every node: `/etc/systemd/journald.conf.d/retention.conf`
  sets `MaxRetentionSec=4day` plus a hard `SystemMaxUse` size cap as a
  belt-and-suspenders limit (`300M` on `brick420` — only 20G free on its SD
  card; `2G` on `brick2000`, which has far more headroom; `1G` on `brick666`,
  an SD card with plenty free). Raspberry Pi OS
  on Trixie ships `40-rpi-volatile-storage.conf` (`Storage=volatile`), so
  without `Storage=persistent` the journal is lost on every reboot and these
  limits never apply; `brick420` lost the evidence of the 2026-09-29 DNS
  outage that way. Not tracked as a
  manifest since it's host config, not cluster config — reapply by hand if
  either Pi is ever reimaged:

  ```bash
  ssh zaphod@<node-ip> "sudo mkdir -p /etc/systemd/journald.conf.d && \
    sudo tee /etc/systemd/journald.conf.d/retention.conf >/dev/null <<'EOF'
  [Journal]
  Storage=persistent
  MaxRetentionSec=4day
  SystemMaxUse=<300M on brick420, 2G on brick2000, 1G on brick666>
  EOF
  sudo systemctl restart systemd-journald"
  ```

- **kubelet container logs** (pod stdout/stderr, `/var/log/pods`): no change
  needed — both nodes run plain `k3s agent`/`k3s server` with no kubelet-arg
  overrides, so it's already on the upstream kubelet defaults
  (`containerLogMaxSize: 10Mi`, `containerLogMaxFiles: 5` = 50Mi/container
  cap). That's size-bounded, not age-bounded, but was already safe from
  unbounded growth — no explosion risk, just not framed in days like the
  other two layers.
- `rsyslog` is inactive on both nodes (journald-only logging), so there's no
  separate flat-file `/var/log/syslog` growth to manage — the small existing
  `/etc/logrotate.d/` entries on `brick2000` (e.g. `prometheus-node-exporter`,
  from its apt package) are unrelated to this and were left alone.

Everything here is plain `kubectl apply -f` (static manifests) or `helm
upgrade --install` (things already packaged as a Helm chart) — no Terraform.
It was tried for the dashboard's namespace/RBAC but dropped as unneeded
tooling overhead for a single-operator homelab cluster this size.

For deploying the `chucks-wisdom` app itself once this cluster is up, see
that repo's `k8s_config/CLUSTER_SETUP.md`.
