# Phase 1.5 — Infrastructure & Deployment (Milestone 1.5)

**Goal:** Take the working Docker Compose system from Phase 1 and deploy
it as a real, publicly reachable, horizontally scalable production
platform on Kubernetes, provisioned by Terraform, shipped by GitHub
Actions — then connect real workers to it from real machines across the
public Internet.

**Why here and not later:** Everything from Phase 2 onward must be
testable across the Internet at real scale. Building the platform now
means Phases 2, 3, and 4 are developed and demonstrated on production
infrastructure rather than retrofitted onto it.

**Prerequisite:** Phase 1 complete and approved.

**Critical constraint:** The coordinator source code does not change to
run in Kubernetes. If it does, Phase 1 violated invariant 9 and that is
fixed before proceeding.

---

## Step 1.5.0 — Design gate

Decide and record:

- Cloud provider and region strategy — workers are global, the
  coordinator is not; where it sits and why.
- Managed versus self-hosted for the database and Redis. Compare
  operational burden, cost, failover, and backup.
- Kubernetes distribution: managed control plane or self-managed.
- Environment topology: how staging and production are isolated.
- Cost ceiling. State the expected monthly spend before provisioning
  anything.
- Secret management approach.

**Exit criteria**
- [ ] Every decision recorded with alternatives and reasoning.
- [ ] Estimated monthly cost documented and accepted before any resource
      is created.
- [ ] Teardown procedure documented before provisioning, so nothing runs
      up a bill unattended.

---

## Step 1.5.1 — Terraform base infrastructure

All cloud resources as code. No console clicks, ever.

- Terraform modules: network, Kubernetes cluster, managed database,
  managed Redis, container registry, DNS zone, secret store.
- Remote state backend with locking.
- Separate workspaces or state files for staging and production.
- Tagging so every resource is attributable.

**Exit criteria**
- [ ] `terraform plan` on a clean checkout produces no unexpected diff.
- [ ] `terraform apply` builds the full staging environment from nothing.
- [ ] `terraform destroy` removes it completely, verified in the console.
- [ ] Apply, destroy, and apply again reproduces an identical environment.
- [ ] Remote state locking prevents a concurrent apply — tested.
- [ ] No credential in the Terraform code or state committed to Git.
- [ ] Actual cost after 24 hours recorded against the estimate.

---

## Step 1.5.2 — Kubernetes manifests and Helm packaging

Package the services for the cluster.

- Deployments for coordinator and dashboard. Workers are **not**
  deployed here — they live outside the cluster, which is the whole
  point.
- Services, ConfigMaps, Secrets, resource requests and limits.
- Liveness and readiness probes wired to the Phase 1 endpoints.
- Horizontal Pod Autoscaler for the coordinator.
- Pod Disruption Budgets so upgrades do not drop the fleet.
- Database migrations as an init job that runs before rollout.
- Helm chart with values files per environment.

**Exit criteria**
- [ ] Coordinator and dashboard deploy to staging via Helm.
- [ ] Pods reach ready; probes function correctly.
- [ ] A worker on a local laptop connects to the staging coordinator.
- [ ] Migrations run automatically before the new version serves traffic.
- [ ] Deleting a coordinator pod triggers automatic reschedule; connected
      workers reconnect without operator action.
- [ ] Rolling upgrade completes with no permanent worker loss.
- [ ] Resource limits set on every container.

---

## Step 1.5.3 — GitHub Actions CI pipeline

Every change gets verified before it lands.

- On pull request: lint, unit tests, integration tests against ephemeral
  database and Redis, build images, vulnerability scan, Terraform
  validate and plan.
- Required status checks before merge.
- Build caching so the pipeline stays fast.

**Exit criteria**
- [ ] Every pull request runs the full pipeline automatically.
- [ ] A deliberately broken test blocks merge — verified by trying.
- [ ] A deliberately vulnerable dependency is flagged — verified.
- [ ] Terraform plan output appears on infrastructure pull requests.
- [ ] Pipeline completes within an acceptable, documented duration.
- [ ] Images are tagged with the commit SHA, never only `latest`.

---

## Step 1.5.4 — GitHub Actions CD pipeline

Automated delivery.

- Merge to main deploys to staging automatically.
- Production deploy is gated by manual approval.
- Deployment smoke test runs after rollout.
- Automatic rollback on smoke test failure.
- Deployment events logged with the commit SHA.

**Exit criteria**
- [ ] Merging to main deploys to staging with no manual steps.
- [ ] Production requires explicit approval before rolling out.
- [ ] A deliberately broken deploy rolls back automatically — verified by
      doing it.
- [ ] The running version is identifiable from the dashboard or an
      endpoint.
- [ ] Full path from commit to running staging is timed and recorded.

---

## Step 1.5.5 — Public ingress, TLS, DNS

Make it reachable from anywhere on Earth.

- Ingress controller with a real, automatically renewed certificate.
- DNS records managed in Terraform.
- Long-lived connection support through the ingress — timeouts tuned so
  persistent connections are not severed.
- Rate limiting at the edge.

**Exit criteria**
- [ ] Coordinator reachable at a public hostname with a valid
      certificate, verified from an external network.
- [ ] Dashboard reachable publicly and protected from anonymous access.
- [ ] A persistent worker connection survives 60 minutes through the
      ingress without being cut.
- [ ] Certificate renewal verified, not assumed.
- [ ] Edge rate limiting blocks a registration flood.
- [ ] Connection works from a mobile hotspot and from behind a
      restrictive corporate network — both tested.

---

## Step 1.5.6 — Observability stack

You cannot operate what you cannot see.

- Metrics collection and dashboards: connected workers, connection
  churn, heartbeat rate, auth failures, coordinator CPU and memory,
  database and Redis health, request latency percentiles.
- Log aggregation with correlation ID search across all coordinator
  replicas.
- Alerts: coordinator down, worker fleet drop, auth failure spike,
  database or Redis unavailable, certificate near expiry.
- Alert delivery to a real channel that reaches you.

**Exit criteria**
- [ ] Metrics dashboard shows live fleet state.
- [ ] A single worker session is traceable by correlation ID across
      replicas.
- [ ] Killing the coordinator fires an alert that actually reaches you.
- [ ] Simulated auth failure spike fires an alert.
- [ ] Log retention period configured and documented.
- [ ] No credential or token appears in any aggregated log.

---

## Step 1.5.7 — Coordinator horizontal scaling proof

Prove invariant 9 holds in reality.

- Run three or more coordinator replicas behind the ingress.
- Verify workers distribute across replicas.
- Verify any replica can address any worker via the Redis connection
  registry.
- Verify autoscaling under load.

**Exit criteria**
- [ ] Three replicas serve one fleet simultaneously.
- [ ] Worker distribution across replicas verified, not assumed.
- [ ] Killing one replica migrates its workers to others automatically;
      fleet count recovers on the dashboard.
- [ ] Autoscaler adds replicas under load and removes them after.
- [ ] Dashboard shows one coherent fleet regardless of replica count.
- [ ] No coordinator instance holds authoritative state — verified by
      killing each in turn with no data loss.

---

## Step 1.5.8 — Real Internet worker onboarding

The point of the whole phase.

- Worker distribution: container image and a plain installer for
  machines without Docker.
- Onboarding documentation someone else can follow unaided.
- Enrollment credential issuance and revocation procedure.
- Configuration is exactly three things: coordinator URL, enrollment
  credential, CA trust.

**Exit criteria**
- [ ] Worker connects from a second laptop on your home network.
- [ ] Worker connects from a desktop.
- [ ] Worker connects from a VPS in a different country.
- [ ] Worker connects from a friend's computer on their own ISP,
      installed by them following only the documentation.
- [ ] Worker connects over a mobile hotspot.
- [ ] All appear on one dashboard simultaneously alongside Docker
      workers.
- [ ] Coordinator code is byte-identical to the local run.
- [ ] No inbound port opened on any worker machine.
- [ ] Latency differences between regions visible on the dashboard.
- [ ] Each verified machine recorded in `PHASE_STATE.md`.

---

## Step 1.5.9 — M1.5 demo and verification

**Demo you run yourself**
1. Open the public dashboard from your phone on mobile data.
2. Show workers connected from multiple countries and networks.
3. Push a trivial commit; watch CI run and deploy to staging.
4. Show the new version live without disconnecting the fleet.
5. Scale coordinator replicas up and down; fleet stays coherent.

**Failure demo you run yourself**
- Kill a coordinator pod → workers migrate, dashboard recovers.
- Take the database offline → readiness fails, alert fires, recovery on
  restore.
- Deploy a broken version → automatic rollback.
- Disconnect a remote worker's Internet → offline, then automatic
  reconnect.
- Revoke a remote worker's credential → disconnected, cannot return.

**Capturable:** dashboard with geographically distributed workers;
GitHub Actions run; rollback in action; alert notification; replica
scaling.

**Exit criteria**
- [ ] Full demo performed by you.
- [ ] Full failure demo performed by you.
- [ ] Five real Internet worker machine types verified and recorded.
- [ ] Staging environment reproducible from scratch via Terraform.
- [ ] Runbook written for deploy, rollback, scale, and teardown.
- [ ] Cost tracked against estimate.
- [ ] `PHASE_STATE.md` updated.
- [ ] Approval obtained before Phase 2.
