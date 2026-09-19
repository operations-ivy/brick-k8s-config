# Terraform for this cluster

Scope: Terraform manages **k8s-level resources on top of an already-running
k3s cluster** — namespaces, deployments, services, and (later) the
`kube-prometheus-stack` Helm release. It does not install k3s or join nodes;
that stays a bootstrap script (see the repo root `README.md`) because it's
imperative, one-time, SSH-based OS provisioning that Terraform `remote-exec`
provisioners would only make more fragile.

## Status

Not yet run against the cluster. Review the plan before applying:

```bash
cd terraform
terraform init
terraform plan -var kubeconfig_path=~/.kube/chuck-config
```

Managed here so far — deliberately the small, locally-authored pieces
rather than bulk upstream manifests:

- `kubernetes_namespace.monitoring` — trivial, currently-unapplied namespace.
- `kubernetes_namespace.kubernetes_dashboard` — previously the `Namespace`
  block inside `dashboard/dashboard.yaml`; removed from that file since this
  now owns it. **Apply this before applying `dashboard/dashboard.yaml`.**
- `kubernetes_service_account.dashboard_admin_user` /
  `kubernetes_cluster_role_binding.dashboard_admin_user` — replaces
  `dashboard/dashboard-admin-user.yaml` (deleted). Small enough to convert
  natively, and worth having under state/drift-detection since it's a
  `cluster-admin` binding.

## Open question: how to bring in the remaining manifests

- `dashboard/dashboard.yaml` (minus the namespace, now above) is still the
  upstream Kubernetes Dashboard Deployment/Service/RBAC/Secret/ConfigMap
  boilerplate — many resources in one file. Hand-converting it to native
  `kubernetes_deployment` / `kubernetes_service` / etc. resources is a lot of
  rewrite for boilerplate that rarely changes. If/when it's worth managing
  via Terraform, the `gavinbunney/kubectl` provider's `kubectl_manifest`
  resource can ingest the raw YAML with much less rework than a native
  rewrite.
- `monitoring/kube-prometheus-stack/` is a Helm release (not yet installed).
  The `hashicorp/helm` provider can drive that `helm install` from here once
  item 4 (observability stack) is actually being worked.
- The chuck app's own manifests (`chucks-wisdom/k8s_config/`) are out of
  scope for this repo's Terraform — that's a separate decision for the app
  repo to make, if it wants to move off `kubectl apply -f`.
