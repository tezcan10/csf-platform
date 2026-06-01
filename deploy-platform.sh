#!/usr/bin/env bash
# deploy-platform.sh — deploy dashboard and supporting k8s resources
# Pipeline now runs via GitHub Actions (csf-gitops/.github/workflows/deploy.yml)
set -euo pipefail

echo "==> Applying namespace, PVC, RBAC..."
kubectl apply -f k8s/namespace.yml
kubectl apply -f k8s/pvc.yml
kubectl apply -f k8s/rbac.yml

echo "==> Creating dashboard ConfigMap from dashboard.html..."
kubectl create configmap dashboard-html \
  --from-file=dashboard.html=agents/dashboard.html \
  --namespace csf-platform \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Deploying dashboard..."
kubectl apply -f k8s/dashboard.yml

echo ""
echo "==> Done. Dashboard will be available at:"
kubectl get svc csf-dashboard -n csf-platform \
  -o jsonpath='http://{.status.loadBalancer.ingress[0].ip}{"\n"}' 2>/dev/null || \
  echo "    (LoadBalancer IP pending — check: kubectl get svc -n csf-platform)"
