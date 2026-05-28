#!/bin/bash
#
# setup-backend.sh
# Bootstraps the Azure Storage backend for Terraform state.
# Run this ONCE before `terraform init`.
#
# Usage: ./setup-backend.sh
#

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
RESOURCE_GROUP="rg-csf-tfstate"
STORAGE_ACCOUNT="stcsfdemotfstate"
CONTAINER="tfstate"
LOCATION="westeurope"
# ─────────────────────────────────────────────────────────────────────────────

echo "==> Checking az cli login..."
if ! az account show &>/dev/null; then
  echo "ERROR: Not logged in. Run 'az login' first."
  exit 1
fi

SUBSCRIPTION=$(az account show --query name -o tsv)
echo "    Subscription: $SUBSCRIPTION"

echo ""
echo "==> Creating resource group '$RESOURCE_GROUP'..."
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output table

echo ""
echo "==> Creating storage account '$STORAGE_ACCOUNT'..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --output table

echo ""
echo "==> Creating blob container '$CONTAINER'..."
az storage container create \
  --name "$CONTAINER" \
  --account-name "$STORAGE_ACCOUNT" \
  --auth-mode login \
  --output table

echo ""
echo "==> Done. Backend is ready."
echo "    You can now run: terraform init"
