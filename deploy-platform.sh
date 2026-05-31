#!/usr/bin/env bash
# deploy-platform.sh — build pipeline image, push to ACR, deploy all k8s manifests
set -euo pipefail

ACR="acrcsfdemo.azurecr.io"
IMAGE="${ACR}/csf-pipeline:latest"

echo "==> Logging into ACR..."
az acr login --name acrcsfdemo

echo "==> Building pipeline image..."
docker build -f Dockerfile.pipeline -t "$IMAGE" .

echo "==> Pushing pipeline image..."
docker push "$IMAGE"

echo "==> Applying namespace, PVC, RBAC..."
kubectl apply -f k8s/namespace.yml
kubectl apply -f k8s/pvc.yml
kubectl apply -f k8s/rbac.yml

echo "==> Creating dashboard ConfigMap from dashboard.html..."
kubectl create configmap dashboard-html \
  --from-file=dashboard.html=agents/dashboard.html \
  --namespace csf-platform \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Checking for k8s/secret.yml..."
if [ -f k8s/secret.yml ]; then
  kubectl apply -f k8s/secret.yml
else
  echo "    WARNING: k8s/secret.yml not found — create it from k8s/secret.yml.example"
  echo "    Pipeline CronJob will not start without secrets."
fi

echo "==> Deploying CronJob and dashboard..."
kubectl apply -f k8s/pipeline-cronjob.yml
kubectl apply -f k8s/dashboard.yml

echo ""
echo "==> Done. Dashboard will be available at:"
kubectl get svc csf-dashboard -n csf-platform \
  -o jsonpath='http://{.status.loadBalancer.ingress[0].ip}{"\n"}' 2>/dev/null || \
  echo "    (LoadBalancer IP pending — check: kubectl get svc -n csf-platform)"
