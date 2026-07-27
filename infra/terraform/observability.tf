# Step 1.5.6 — Observability stack (design gate 2026-07-27, user-approved).
#
# Lean self-hosted, $0 (no paid Azure services — same family as Decision
# #41: no Log Analytics, no Managed Grafana). Installed cluster-wide as
# Helm releases into an `observability` namespace, mirroring how ingress.tf
# installs ingress-nginx / cert-manager:
#
#   - kube-prometheus-stack : Prometheus (metrics store + alert engine),
#       Grafana (dashboards), Alertmanager (delivery), node-exporter +
#       kube-state-metrics. ONE release covers 3 components + node/pod
#       CPU-mem out of the box (ponytail: one release beats wiring three).
#   - loki  : log store, SingleBinary mode, filesystem storage, retention
#       bounded (var.loki_retention_hours) — the documented retention value.
#   - alloy : DaemonSet, tails pod stdout (already structured JSON with
#       correlation_id, §11) and ships to Loki.
#
# Needs the extra node (var.node_count 1->2, this phase). Grafana is reached
# by `kubectl port-forward` for the demo (no 2nd public IP spent — same
# port-forward reach accepted for internal tooling in 1.5.2).
#
# Alerts route to a Discord/Slack incoming webhook. The webhook URL is NEVER
# in Terraform state or values (§12): Alertmanager reads it from a file
# mounted from a pre-created Secret (var.alertmanager_webhook_secret_name,
# key `webhook-url`), via slack_configs.api_url_file. For Discord, use the
# webhook URL with `/slack` appended (Discord's Slack-compatible endpoint).
#
# UNVERIFIED until first `terraform apply` on Azure: chart versions AND the
# values below (kube-prometheus / loki v6 / alloy River config are all
# fiddly) are drafts — confirm on init/plan/apply, exactly as the ingress
# chart versions are flagged. Do not assume correct without that apply.

# APPLY ORDER (grafana + alertmanager consume Secrets that must pre-exist,
# or their pods hang — the 1.5.5 `--wait` deadlock lesson):
#   1. terraform apply -target=kubernetes_namespace.observability
#   2. ./infra/helm/bootstrap-secrets.ps1   (creates grafana-admin +
#      alertmanager-webhook in the observability ns)
#   3. terraform apply                       (installs the charts)
resource "kubernetes_namespace" "observability" {
  metadata {
    name   = "observability"
    labels = local.common_labels
  }
}

# --- Metrics + dashboards + alerting (one release) ---------------------
resource "helm_release" "kube_prometheus_stack" {
  name       = "kube-prometheus-stack" # = the release label ServiceMonitors/Rules select on
  namespace  = kubernetes_namespace.observability.metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.kube_prometheus_stack_chart_version

  # This chart + CRDs + a scaling 2nd node can exceed the provider's default
  # 300s --wait (the 1.5.5 deadlock lesson). Give it room.
  timeout = 900

  # Alertmanager mounts the webhook Secret; its config points a Slack
  # receiver at the mounted file. Keep timeouts/limits small for one node.
  values = [<<-EOT
    # Managed AKS hides these control-plane components — scraping them just
    # produces permanent "down" targets and noise. Turn them off.
    kubeControllerManager: { enabled: false }
    kubeScheduler: { enabled: false }
    kubeEtcd: { enabled: false }
    kubeProxy: { enabled: false }

    prometheus:
      prometheusSpec:
        retention: 24h
        resources:
          requests: { cpu: 100m, memory: 400Mi }
          limits: { cpu: 500m, memory: 900Mi }
        storageSpec:
          volumeClaimTemplate:
            spec:
              accessModes: ["ReadWriteOnce"]
              resources: { requests: { storage: 5Gi } }

    alertmanager:
      alertmanagerSpec:
        # Mounts Secret at /etc/alertmanager/secrets/<name>/webhook-url
        secrets:
          - ${var.alertmanager_webhook_secret_name}
        resources:
          requests: { cpu: 25m, memory: 64Mi }
          limits: { cpu: 100m, memory: 128Mi }
      config:
        # Default receiver is `null`: the generic kube-prometheus built-in
        # alerts (KubeHpaMaxedOut, KubeCPUOvercommit, ...) are noisy on a
        # deliberately tiny single-pool cluster and would spam Discord. Only
        # OUR alerts (labelled team=dcds in the platform PrometheusRule) match
        # the child route to `chat` (Step 1.5.6 #7).
        route:
          receiver: "null"
          group_wait: 30s
          group_interval: 5m
          repeat_interval: 3h
          routes:
            # The always-firing Watchdog heartbeat goes nowhere (defining the
            # `null` receiver is also what stops the operator's
            # "undefined receiver null" reconcile error).
            - match: { alertname: Watchdog }
              receiver: "null"
            # Only our own alerts reach the channel.
            - match: { team: dcds }
              receiver: chat
        receivers:
          - name: "null"
          - name: chat
            slack_configs:
              - channel: "#alerts"
                send_resolved: true
                api_url_file: /etc/alertmanager/secrets/${var.alertmanager_webhook_secret_name}/webhook-url
                title: '{{ .CommonAnnotations.summary }}'
                text: '{{ range .Alerts }}{{ .Annotations.summary }}{{ "\n" }}{{ end }}'

    grafana:
      # admin password from a pre-created Secret, never in state (§12).
      admin:
        existingSecret: grafana-admin
        userKey: admin-user
        passwordKey: admin-password
      resources:
        requests: { cpu: 50m, memory: 128Mi }
        limits: { cpu: 200m, memory: 256Mi }
      # Loki added as a datasource so logs are queryable in Explore and
      # log-based (auth-spike) alerts can be authored in the UI at demo time.
      additionalDataSources:
        - name: Loki
          type: loki
          access: proxy
          url: http://loki.observability.svc:3100

    nodeExporter:
      resources:
        requests: { cpu: 25m, memory: 32Mi }
        limits: { cpu: 100m, memory: 64Mi }
    kube-state-metrics:
      resources:
        requests: { cpu: 25m, memory: 32Mi }
        limits: { cpu: 100m, memory: 128Mi }
    prometheusOperator:
      resources:
        requests: { cpu: 50m, memory: 64Mi }
        limits: { cpu: 200m, memory: 256Mi }
  EOT
  ]

  depends_on = [kubernetes_namespace.observability]
}

# --- Log store ---------------------------------------------------------
resource "helm_release" "loki" {
  name       = "loki"
  namespace  = kubernetes_namespace.observability.metadata[0].name
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  version    = var.loki_chart_version
  timeout    = 600

  values = [<<-EOT
    deploymentMode: SingleBinary
    loki:
      auth_enabled: false
      commonConfig: { replication_factor: 1 }
      storage: { type: filesystem }
      schemaConfig:
        configs:
          - from: "2024-01-01"
            store: tsdb
            object_store: filesystem
            schema: v13
            index: { prefix: index_, period: 24h }
      limits_config:
        retention_period: ${var.loki_retention_hours}h
      compactor:
        retention_enabled: true
        delete_request_store: filesystem
    singleBinary:
      replicas: 1
      persistence: { enabled: true, size: 5Gi }
      resources:
        requests: { cpu: 100m, memory: 256Mi }
        limits: { cpu: 400m, memory: 512Mi }
    # Disable the scale-out components — SingleBinary only.
    backend: { replicas: 0 }
    read: { replicas: 0 }
    write: { replicas: 0 }
    chunksCache: { enabled: false }
    resultsCache: { enabled: false }
    gateway: { enabled: false }
    lokiCanary: { enabled: false }
    test: { enabled: false }
  EOT
  ]

  depends_on = [kubernetes_namespace.observability]
}

# --- Log collector (DaemonSet) -----------------------------------------
resource "helm_release" "alloy" {
  name       = "alloy"
  namespace  = kubernetes_namespace.observability.metadata[0].name
  repository = "https://grafana.github.io/helm-charts"
  chart      = "alloy"
  version    = var.alloy_chart_version
  timeout    = 600

  # River config: discover pods, tail their logs, ship to Loki with
  # namespace/pod/container labels. UNVERIFIED — validate with `alloy fmt`
  # / on first apply before trusting.
  values = [<<-EOT
    alloy:
      resources:
        requests: { cpu: 50m, memory: 96Mi }
        limits: { cpu: 200m, memory: 192Mi }
      configMap:
        content: |-
          discovery.kubernetes "pods" {
            role = "pod"
          }

          discovery.relabel "pods" {
            targets = discovery.kubernetes.pods.targets
            rule {
              source_labels = ["__meta_kubernetes_namespace"]
              target_label  = "namespace"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_name"]
              target_label  = "pod"
            }
            rule {
              source_labels = ["__meta_kubernetes_pod_container_name"]
              target_label  = "container"
            }
          }

          loki.source.kubernetes "pods" {
            targets    = discovery.relabel.pods.output
            forward_to = [loki.write.default.receiver]
          }

          loki.write "default" {
            endpoint {
              url = "http://loki.observability.svc:3100/loki/api/v1/push"
            }
          }
  EOT
  ]

  depends_on = [helm_release.loki]
}

# --- Outputs -----------------------------------------------------------

output "grafana_port_forward" {
  description = "Reach Grafana for the demo (no public IP spent)."
  value       = "kubectl -n observability port-forward svc/kube-prometheus-stack-grafana 3000:80"
}
