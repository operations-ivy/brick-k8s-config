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
  ```

  Access via SSH-tunneled port-forward, not exposed on the LAN:

  ```bash
  ssh -L 8443:localhost:8443 zaphod@<main-node-ip> \
    'sudo k3s kubectl port-forward -n kubernetes-dashboard svc/kubernetes-dashboard 8443:443'
  # in another terminal, mint a login token:
  ssh zaphod@<main-node-ip> 'sudo k3s kubectl -n kubernetes-dashboard create token admin-user'
  ```

- `monitoring/` — `kube-prometheus-stack` Helm values, prepped but **not
  yet installed** (that's the open observability-stack todo).
- `debug/` — a node-problem-detector `DaemonSet` for `kube-system`.
- `terraform/` — Terraform for cluster-level k8s resources on top of the
  running cluster (namespaces, and eventually the Helm release above). See
  `terraform/README.md` for scope and status. Node bootstrap above stays a
  plain script — not a good fit for Terraform.

For deploying the `chucks-wisdom` app itself once this cluster is up, see
that repo's `k8s_config/CLUSTER_SETUP.md`.
