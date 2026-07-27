#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Manual deployment with gcloud (no Terraform required)
# Usage: bash deploy.sh <PROJECT_ID> <BILLING_ACCOUNT_ID> [REGION]
###############################################################################

PROJECT_ID="${1:?Usage: deploy.sh PROJECT_ID BILLING_ACCOUNT_ID [REGION]}"
BILLING_ACCOUNT_ID="${2:?Provide billing account ID}"
REGION="${3:-us-central1}"
SERVICE_NAME="cost-killswitch"
SA_NAME="killswitch-rt"
TOPIC="budget-alerts"
SUBSCRIPTION="killswitch-push"
ARTIFACT_REPO="cloud-billing"

echo "=== Deploying cost kill switch to ${PROJECT_ID} ==="

# 1. Enable APIs
gcloud services enable \
  run.googleapis.com \
  pubsub.googleapis.com \
  bigquery.googleapis.com \
  cloudbilling.googleapis.com \
  cloudscheduler.googleapis.com \
  compute.googleapis.com \
  --project="${PROJECT_ID}"

# 2. Create BigQuery dataset
bq --location_id=US mk --dataset "${PROJECT_ID}:cloud_billing_export" || true

# 3. Create Pub/Sub topic
gcloud pubsub topics create "${TOPIC}" --project="${PROJECT_ID}" || true

# 4. Create service account
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Cost Kill Switch Runtime SA" \
  --project="${PROJECT_ID}" || true

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 5. Grant IAM roles (least privilege)
grant_role() {
  local role="$1"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet 2>/dev/null || true
}

grant_role "roles/run.admin"
grant_role "roles/pubsub.subscriber"
grant_role "roles/bigquery.dataViewer"
grant_role "roles/bigquery.jobUser"

# Conditional roles (uncomment as needed)
# grant_role "roles/cloudscheduler.admin"
# grant_role "roles/compute.instanceAdmin"
# grant_role "roles/cloudbuild.builds.editor"

# 6. Create Artifact Registry repo
gcloud artifacts repositories create "${ARTIFACT_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" || true

# 7. Build and push image
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/killswitch:latest"
gcloud builds submit --tag "${IMAGE}" --project="${PROJECT_ID}"

# 8. Deploy to Cloud Run (scale-to-zero, internal ingress)
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=1 \
  --memory=256Mi \
  --cpu=1 \
  --set-env-vars="DRY_RUN=true,ALLOWLIST=${PROJECT_ID},ENABLE_BILLING_SHUTOFF=false,ENABLE_RUN_PAUSE=false,ENABLE_TRIGGER_DISABLE=false,STOP_COMPUTE_INSTANCES=false,LOG_LEVEL=INFO,PROJECT_ID=${PROJECT_ID},ALERT_TOPIC=${TOPIC},BQ_BILLING_TABLE=${PROJECT_ID}.cloud_billing_export.gcp_billing_export_resource_v1_$(echo ${BILLING_ACCOUNT_ID} | tr '-' '_'),BUDGET_AMOUNT_USD=5"

# 9. Get the Cloud Run URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url)')

echo "Cloud Run URL: ${SERVICE_URL}"

# 10. Allow Pub/Sub to invoke the service
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet

# 11. Create push subscription
gcloud pubsub subscriptions create "${SUBSCRIPTION}" \
  --topic="${TOPIC}" \
  --push-endpoint="${SERVICE_URL}/" \
  --push-auth-service-account="${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --min-retry-delay=10s \
  --max-retry-delay=600s \
  --ack-deadline=30 || true

# 12. Create budget alert (requires billing account)
echo ""
echo "=== Creating budget (manual step recommended via Console) ==="
echo "Budget account: ${BILLING_ACCOUNT_ID}"
echo "Topic: projects/${PROJECT_ID}/topics/${TOPIC}"
echo "Amount: \$5/month"
echo "Thresholds: 50% actual, 90% forecast, 100% actual, 150% actual"
echo ""
echo "Or run:"
echo "gcloud billing budgets create --billing-account=${BILLING_ACCOUNT_ID} \\"
echo "  --display-name='Cost Kill Switch Budget' \\"
echo "  --budget-amount=5.00USD \\"
echo "  --threshold-rule=percent=0.5,basis=CURRENT_SPEND \\"
echo "  --threshold-rule=percent=0.9,basis=FORECASTED_SPEND \\"
echo "  --threshold-rule=percent=1.0,basis=CURRENT_SPEND \\"
echo "  --threshold-rule=percent=1.5,basis=CURRENT_SPEND \\"
echo "  --pubsub-topic=projects/${PROJECT_ID}/topics/${TOPIC}"

echo ""
echo "=== Enable billing export to BigQuery ==="
echo "Console > Billing > Billing export > BigQuery export > Edit"
echo "Dataset: ${PROJECT_ID}:cloud_billing_export"
echo ""
echo "=== Deployment complete ==="
echo "Service:   ${SERVICE_URL}"
echo "Health:    ${SERVICE_URL}/health"
echo "Config:    ${SERVICE_URL}/ (GET)"
echo "Dashboard: ${SERVICE_URL}/dashboard"
