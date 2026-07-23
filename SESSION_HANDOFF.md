# SESSION_HANDOFF.md

Read `CLAUDE.md` first (guardrails), then `PHASE_STATE.md` (authoritative
status, decisions log, blockers). This file is a resume-work pointer for
the next session — it is not a source of truth, `PHASE_STATE.md` is.

---

# Where things stand

## 2026-07-23 — LATEST: local k3d + Cloudflare Tunnel DECIDED, build-ready

Read this first; the GCP/OCI narrative below it is superseded history.

- **Compute host decided = local Kubernetes via k3d** (k3s-in-Docker,
  real CNCF K8s on the user's PC, $0) — Decisions Log #53, resolving the
  previously-open Open Questions #2. V1 keeps Kubernetes; it just runs
  locally instead of on a cloud host.
- **Reachability = Cloudflare Tunnel** (`cloudflared` on the coordinator
  PC → public `trycloudflare.com` URL) — Decisions Log #52. Free, no
  card, works on the company network (the block is a **MAC-filter on
  Google only**, not an account restriction; Cloudflare isn't Google).
- **No paid cloud provider.** Earlier "GCP account restricted / all M1.5
  parked" framing (Decisions #50–#51) was a misread of the block as an
  account restriction — **superseded by #52–#53**. Cloud-account blocker
  is gone.
- **Provider history (do NOT re-litigate):** OCI → k3d → OCI → GCP →
  (final) local k3d + Cloudflare Tunnel. Stands unless the user says
  otherwise.
- **Cloudflare Tunnel setup** (given to user, not yet run): install
  `cloudflared` on the coordinator PC (`winget install
  --id Cloudflare.cloudflared`) → `docker compose up` →
  `cloudflared tunnel --no-tls-verify --url https://localhost:8443` →
  public URL → point a worker on another PC/network at
  `wss://<that-host>` (port 443, no dev-CA needed). Worker/test PCs
  install nothing.
- **Docs are build-ready; no code written this session** (per user
  instruction). Nothing committed or pushed — docs-only
  (`PHASE_STATE.md` ×2, this file). The two identical phase-state files
  were kept in sync — worth collapsing to one canonical file later.

---

## Prior session history (2026-07-22) — superseded above

**Milestone 1 is complete** (unchanged from before this session).
This session worked entirely inside **Milestone 1.5**, and spent most
of its time on **Step 1.5.0 — design gate**, which was decided,
reopened, and re-decided twice before settling:

1. **First pass**: built a full $0-cost design gate around OCI
   (Oracle Cloud) Always Free tier + OKE — Decisions Log #36–41.
   Approved by the user.
2. **User then rejected OCI outright** on learning every major cloud
   requires a card on file even for free tiers, and asked for a
   platform needing no payment method at all. Clarified the user has
   no spare always-on machine — only their main laptop, on only while
   testing. Redesigned around **k3d (k3s in Docker) on the laptop +
   Cloudflare Tunnel/DNS for public reachability** — Decisions Log
   #42–46, superseding #36/#38/#37/#44. Approved by the user.
3. **User then reversed course again**, explicitly choosing to accept
   OCI's card requirement after all ("we are going to use OCI cloud
   for this"). Confirmed via a direct question before acting, since it
   contradicted the stated reason for rejecting OCI the first time.
   Reverted to OCI/OKE — Decisions Log #47–49, superseding #42/#43/#44
   again (i.e. back to the first pass's compute/region decisions).

**Decisions #39–41 and #45–46 were never OCI- or k3d-specific and
stood unchanged through both pivots**: self-hosted Postgres/Redis
in-cluster, one cluster split into `staging`/`production` namespaces,
`sealed-secrets`, ghcr.io for images, Terraform Cloud for remote
state. **Cost ceiling stayed $0/month throughout** — a card on file
for OCI identity verification doesn't change that; Always Free
resources are never billed.

Full history and reasoning for all three passes: `PHASE_STATE.md`
Decisions Log #36–49. Do not re-litigate this without new information
— it's already been through two reversals this session alone.

**Step 1.5.1 — Terraform base infrastructure — started, scaffolded to
match the final (OCI) decision, not run.** New directory
`infra/terraform/`:
- `versions.tf` — Terraform Cloud remote-state backend block, `oci`
  provider block.
- `variables.tf` — OCI API-key auth variables (`tenancy_ocid`,
  `user_ocid`, `fingerprint`, `private_key_path`, `region`,
  `compartment_ocid`), none of which have real values yet.
- `main.tf` — OKE via the community
  `oracle-terraform-modules/oke/oci` module (not hand-rolled
  VCN/subnet/security-list resources), node pool sized to OCI's
  Always Free `VM.Standard.A1.Flex` allowance (2 OCPU/12GB × 2 nodes
  = 4 OCPU/24GB total).
- `README.md` — exactly what's needed before a real `terraform init`
  (see Open Items below), and what Terraform deliberately does not
  manage in this project (DB/Redis, ingress specifics, container
  registry).

**Every file in `infra/terraform/` is explicitly marked UNVERIFIED in
its own comments** — no `terraform` CLI is installed on this machine,
no Terraform Cloud or OCI account exists yet, so nothing has been
through `init`/`validate`/`plan`/`apply`. The module version (`~> 5.0`
of `oracle-terraform-modules/oke/oci`) and its input variable names are
a draft that must be checked against the module's current Terraform
Registry page before trusting this config — do not assume it's correct
without that check.

---

# Open items — not done, not something this session finished

1. **Nothing committed.** `git status` at session end:
   - Modified, unstaged: `SESSION_HANDOFF.md`, `docs/PHASE_STATE.md`,
     `phase_state.md`.
   - Untracked: `infra/terraform/` (new this session), `.claude.backup/`
     (carried forward, pre-existing, still unexplained/harmless),
     `.playwright-mcp/` (new, likely a tool cache dir, not project
     code — not yet judged safe to delete or gitignore).
   - Committing was not requested this session; per CLAUDE.md working
     discipline, none of the above was staged or committed.
2. **Nothing pushed to `origin`.** `main` is still 2 commits ahead
   (`5003ad9`, `78277be`) from before this session — unchanged.
3. **Terraform CLI not installed** on this machine.
4. **No Terraform Cloud account/org/workspace/token** exists yet.
5. **No OCI account exists yet** — needs: account creation (card on
   file, now accepted, Decisions Log #47), an API signing key
   (`user_ocid`, `fingerprint`, private key), `tenancy_ocid`, a
   dedicated project compartment (`compartment_ocid`), and a check of
   which OCI home region actually has `VM.Standard.A1.Flex` (Ampere,
   Always Free) capacity right now — this shape is frequently reported
   "out of host capacity" in popular regions, a real provider-side
   constraint to expect, not a sign of a config error.
6. **`certs;C`** — same stray empty directory noted in every prior
   session's handoff, still present, still not this project's call to
   delete.
7. **Branch protection on `main`** and **PR #1 unmerged** — unchanged
   from every prior session, not touched this session.

---

# Gotchas discovered this session

- **Every major cloud provider requires a payment method on file for
  free-tier signup**, OCI included — this is universal fraud
  prevention, not something specific to any one provider, and drove
  the first design-gate reversal. If a future session is asked again
  for a "genuinely free, no card" cloud Kubernetes option: there isn't
  one among AWS/GCP/Azure/OCI/DigitalOcean. The only real no-card path
  is self-hosting (k3d/k3s on hardware you already own), which is what
  the second (later reverted) pass used.
- **A cloud-vs-self-host decision cascades into more than just
  "compute"**: switching from OCI to k3d-on-laptop also forced changes
  to region (n/a locally), and public ingress (Cloudflare Tunnel
  becomes necessary specifically because a laptop has no stable public
  IP and no port should be opened on the home router) — three
  Decisions Log entries changed together, not just one. When OCI came
  back, all three reverted together for the same reason. Don't treat
  "compute platform" as an isolated decision from "ingress" and
  "region" — they're coupled once self-hosting is on the table.
- **The community `oracle-terraform-modules/oke/oci` Terraform module
  was used instead of hand-rolling OCI's VCN/subnet/security-list/NAT-
  gateway resources individually** — OKE's networking prerequisites are
  verbose and well-templated already; re-deriving them from memory
  risks wrong resource attributes. Flagged as unverified rather than
  asserted correct, since no `terraform init` has run against it yet.

---

# Verification method used this session

None of this session's `infra/terraform/` work has been executed —
no Terraform CLI available, no cloud/Terraform Cloud accounts exist.
Everything under Step 1.5.1 is unverified draft code, explicitly
labeled as such in-file and in `PHASE_STATE.md`, per the zero-
hallucination rule (`CLAUDE.md` §10) applying to infrastructure code
exactly as it does to application code and benchmark claims.

The `docker compose`-based verification method that closed out every
Phase 1 phase (fresh clone, `docker compose up`, `curl`/`GET
/api/workers`, `docker kill`/`pause`/`network disconnect` for failure
injection) was not used this session — no code from M1 was touched,
and M1.5's own tooling (Terraform, OKE, kubectl) isn't installed yet
for a first real verification pass.

---

# Next step

**Compute host decided (local k3d) — build-ready.** Begin **Step 1.5.1
— Terraform base infrastructure**. Order of operations:

1. **Install tooling** on the coordinator PC: `k3d` and the Terraform
   CLI. (Create a Terraform Cloud workspace only if Step 1.5.1's design
   sub-gate keeps Terraform for remote state — Decision #46.) None of
   these are Google, so the company network block doesn't affect them.

2. **Open Step 1.5.1 with a short design sub-gate** (CLAUDE.md §9):
   decide Terraform's now-reduced scope given there are no cloud
   resources to provision — e.g. Terraform driving the k3d cluster +
   namespaces + Helm releases via the `kubernetes`/`helm` providers,
   versus creating the cluster with the k3d CLI and using Helm directly.
   Present options, recommend, get user approval before writing code.

3. **Rewrite or retire the existing `infra/terraform/`** — it is
   OCI-targeted and must not be built on as-is.

4. **Cloudflare Tunnel for §8 testing** (usable independently, any time):
   install `cloudflared` on the coordinator PC
   (`winget install --id Cloudflare.cloudflared`) → `docker compose up`
   → `cloudflared tunnel --no-tls-verify --url https://localhost:8443`
   → point a worker on a second PC / hotspot laptop at `wss://<host>`
   (port 443, no dev-CA needed) → verify register / heartbeat / reconnect
   across the real network.

Provider-agnostic Decisions #39–41 / #45–46 (self-hosted Postgres/Redis,
staging/production namespaces, sealed-secrets, ghcr.io, Terraform Cloud
remote state) still stand. Nothing has been committed or pushed.
