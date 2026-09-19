resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
    labels = {
      name = "monitoring"
    }
  }
}

resource "kubernetes_namespace" "kubernetes_dashboard" {
  metadata {
    name = "kubernetes-dashboard"
  }
}

# cluster-admin is what upstream's own "Creating a sample user" guide uses to
# get a working login token — broad on purpose for a home-lab single-user
# cluster. Scope this down (a Role bound to just the `chuck` namespace, say)
# if this cluster ever has more than one user.
resource "kubernetes_service_account" "dashboard_admin_user" {
  metadata {
    name      = "admin-user"
    namespace = kubernetes_namespace.kubernetes_dashboard.metadata[0].name
  }
}

resource "kubernetes_cluster_role_binding" "dashboard_admin_user" {
  metadata {
    name = "admin-user"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.dashboard_admin_user.metadata[0].name
    namespace = kubernetes_namespace.kubernetes_dashboard.metadata[0].name
  }
}
