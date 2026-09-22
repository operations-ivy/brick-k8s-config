# brick-k8s-config

Cluster-level infrastructure for the k3s cluster running on 2 Raspberry Pi
5s (`brick420` control-plane, `brick2000` worker). Anything specific to the
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

```bash
ssh zaphod@<worker-node-ip>

curl -sfL https://get.k3s.io | K3S_URL=https://<main-node-ip>:6443 \
  K3S_TOKEN=<token-from-main-node> sudo -E sh -s - agent
```

## Verify

```bash
sudo k3s kubectl get nodes -o wide
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

  Exposed on the LAN via Traefik at `https://dashboard.local` (add
  `<main-node-ip> dashboard.local` to `/etc/hosts`; both node reservations
  are set in the router, so this IP is stable). `dashboard-ingress.yaml`
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
  `http://grafana.local` (add `<main-node-ip> grafana.local` to `/etc/hosts`;
  no `IngressRoute`/`ServersTransport` needed here, unlike the dashboard,
  since Grafana serves plain HTTP rather than self-signed HTTPS). Login is
  admin / the hardcoded `adminPassword` in the values file — see the repo's
  secrets-management todo. Any app repo can auto-register a dashboard by
  applying a `ConfigMap` labeled `grafana_dashboard: "1"` in its own namespace
  (the sidecar watches cluster-wide); `chucks-wisdom` does this for the
  importer's dashboard.

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

- `debug/` — a node-problem-detector `DaemonSet` for `kube-system`.

## Log & trace retention policy (2026-09-21, revised to 4 days 2026-09-22)

Kept to 4 days end-to-end, at every layer, to keep storage bounded on both
Pis:

- **Loki** (`monitoring/loki/`): `limits_config.retention_period: 96h`,
  compactor-enforced.
- **Tempo** (`monitoring/tempo/`): `tempo.retention: 96h`, compactor-enforced.
- **journald**, both nodes: `/etc/systemd/journald.conf.d/retention.conf`
  sets `MaxRetentionSec=4day` plus a hard `SystemMaxUse` size cap as a
  belt-and-suspenders limit (`300M` on `brick420` — only 20G free on its SD
  card; `2G` on `brick2000`, which has far more headroom). Not tracked as a
  manifest since it's host config, not cluster config — reapply by hand if
  either Pi is ever reimaged:

  ```bash
  ssh zaphod@<node-ip> "sudo mkdir -p /etc/systemd/journald.conf.d && \
    sudo tee /etc/systemd/journald.conf.d/retention.conf >/dev/null <<'EOF'
  [Journal]
  MaxRetentionSec=4day
  SystemMaxUse=<300M on brick420, 2G on brick2000>
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
