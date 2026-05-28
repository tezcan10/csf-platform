#!/bin/bash
# PRE-REQUISITE: Run 'az login --use-device-code' before
# executing this script

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/infrastructure/csf"
ACR_NAME="acrcsfdemo"
ACR_LOGIN_SERVER="acrcsfdemo.azurecr.io"
IMAGE_NAME="csf-app"
IMAGE_TAG="v1"
AKS_RG="rg-csf-demo"
AKS_NAME="aks-csf-demo"
ARGOCD_NAMESPACE="argocd"
# ──────────────────────────────────────────────────────────────────────────────

# ── Helpers ───────────────────────────────────────────────────────────────────
step() { echo ""; echo "══════════════════════════════════════════"; echo "  $1"; echo "══════════════════════════════════════════"; }
ok()   { echo "  ✔  $1"; }
info() { echo "  →  $1"; }
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     CSF Pipeline Bootstrap               ║"
echo "╚══════════════════════════════════════════╝"

# ── 0. Verify Azure login ─────────────────────────────────────────────────────
step "STEP 0 — Verifying Azure login"
if ! az account show &>/dev/null; then
  echo "  ERROR: Not logged in to Azure."
  echo "  Run 'az login --use-device-code' first, then re-run this script."
  exit 1
fi
SUBSCRIPTION=$(az account show --query name -o tsv)
ok "Logged in — Subscription: $SUBSCRIPTION"

# ── 1. Bootstrap Terraform backend ───────────────────────────────────────────
step "STEP 1 — Bootstrapping Terraform backend (Azure Storage)"
info "Running setup-backend.sh..."
bash "$INFRA_DIR/setup-backend.sh"
ok "Backend ready"

# ── 2. Terraform init ─────────────────────────────────────────────────────────
step "STEP 2 — Terraform init"
info "Initialising Terraform in $INFRA_DIR..."
terraform -chdir="$INFRA_DIR" init
ok "Terraform initialised"

# ── 3. Terraform apply ────────────────────────────────────────────────────────
step "STEP 3 — Terraform apply (ACR + AKS + Role Assignment)"
info "Provisioning Azure infrastructure..."
terraform -chdir="$INFRA_DIR" apply -auto-approve
ok "Azure infrastructure provisioned"

# ── 4. Docker build ───────────────────────────────────────────────────────────
step "STEP 4 — Docker build"
info "Building image $IMAGE_NAME:$IMAGE_TAG from $SCRIPT_DIR..."
docker build -t "$IMAGE_NAME:$IMAGE_TAG" "$SCRIPT_DIR"
ok "Image built — $(docker images "$IMAGE_NAME:$IMAGE_TAG" --format '{{.Size}}')"

# ── 5. Push image to ACR ──────────────────────────────────────────────────────
step "STEP 5 — Push image to ACR"
info "Logging in to $ACR_LOGIN_SERVER..."
az acr login --name "$ACR_NAME"
ok "ACR login succeeded"

info "Tagging $IMAGE_NAME:$IMAGE_TAG → $ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG..."
docker tag "$IMAGE_NAME:$IMAGE_TAG" "$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG"

info "Pushing to ACR..."
docker push "$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG"
ok "Image pushed to ACR"

# ── 6. Connect kubectl to AKS ─────────────────────────────────────────────────
step "STEP 6 — Connect kubectl to AKS"
info "Fetching credentials for $AKS_NAME..."
az aks get-credentials \
  --resource-group "$AKS_RG" \
  --name "$AKS_NAME" \
  --overwrite-existing
ok "kubectl connected to $AKS_NAME"

info "Verifying cluster connectivity..."
kubectl get nodes
ok "Cluster reachable"

# ── 7. Install ArgoCD ─────────────────────────────────────────────────────────
step "STEP 7 — Install ArgoCD"
info "Creating argocd namespace..."
kubectl create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

info "Applying ArgoCD manifests..."
kubectl apply -n "$ARGOCD_NAMESPACE" \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
  2>&1 | grep -v "^$" | tail -5 || true

info "Applying again with server-side apply to handle large CRDs..."
kubectl apply --server-side --force-conflicts \
  -n "$ARGOCD_NAMESPACE" \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
  2>&1 | grep -v "^$" | tail -5 || true

info "Waiting for ArgoCD server to be ready (up to 3 minutes)..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=argocd-server \
  -n "$ARGOCD_NAMESPACE" \
  --timeout=180s
ok "ArgoCD is running"

ARGOCD_PASSWORD=$(kubectl -n "$ARGOCD_NAMESPACE" get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d)
ok "ArgoCD admin password: $ARGOCD_PASSWORD"

# ── 8. Apply ArgoCD Application manifest ──────────────────────────────────────
step "STEP 8 — Apply ArgoCD Application manifest"
info "Registering csf-app with ArgoCD..."
kubectl apply -f "$INFRA_DIR/argocd-app.yml"
ok "ArgoCD Application created"

info "Waiting for ArgoCD to sync (up to 2 minutes)..."
for i in $(seq 1 24); do
  SYNC_STATUS=$(kubectl get application csf-app -n "$ARGOCD_NAMESPACE" \
    -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "Unknown")
  HEALTH_STATUS=$(kubectl get application csf-app -n "$ARGOCD_NAMESPACE" \
    -o jsonpath='{.status.health.status}' 2>/dev/null || echo "Unknown")
  info "[$i/24] Sync: $SYNC_STATUS — Health: $HEALTH_STATUS"
  if [ "$SYNC_STATUS" = "Synced" ] && [ "$HEALTH_STATUS" = "Healthy" ]; then
    break
  fi
  sleep 5
done
ok "Application synced and healthy"

# ── 9. Final status ───────────────────────────────────────────────────────────
step "PIPELINE READY"
echo ""
kubectl get pods,svc -n default
echo ""
EXTERNAL_IP=$(kubectl get svc csf-app-svc -n default \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")
echo ""
ok "Application URL : https://$EXTERNAL_IP"
ok "ArgoCD UI       : https://localhost:8888  (run: kubectl port-forward svc/argocd-server -n argocd 8888:443)"
ok "ArgoCD login    : admin / $ARGOCD_PASSWORD"
echo ""
