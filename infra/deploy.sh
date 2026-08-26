#!/usr/bin/env bash
# Idempotent deploy: safe to re-run. Every step either creates or no-ops.
#
#   export GCP_PROJECT=your-project REGION=us-central1
#   bash infra/deploy.sh
#
# Prerequisites this script does NOT do for you:
#   - gcloud installed, `gcloud auth login`, billing enabled
#   - secrets created (see "Secrets" below)
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${REGION:-us-central1}"
TOPIC="pr-review-jobs"
DLQ="pr-review-dlq"
SUB="pr-review-jobs-push"

gcloud config set project "$GCP_PROJECT"

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com pubsub.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com

echo "==> Topics"
gcloud pubsub topics create "$TOPIC" 2>/dev/null || echo "    $TOPIC exists"
gcloud pubsub topics create "$DLQ"   2>/dev/null || echo "    $DLQ exists"

echo "==> Firestore (native mode; fails harmlessly if the database exists)"
gcloud firestore databases create --location="$REGION" 2>/dev/null || echo "    database exists"

echo "==> Service account"
SA="pr-review-agent@${GCP_PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts create pr-review-agent \
  --display-name="PR Review Agent" 2>/dev/null || echo "    service account exists"

for role in roles/datastore.user roles/aiplatform.user \
            roles/pubsub.publisher roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:${SA}" --role="$role" --condition=None >/dev/null
done

# Secrets are mounted as env vars via --set-secrets, so the services need no
# Secret Manager client code. Create them once, by hand:
#   printf %s "$WEBHOOK_SECRET" | gcloud secrets create github-webhook-secret --data-file=-
#   gcloud secrets create github-private-key --data-file=path/to/key.pem
#   printf %s "$PAT" | gcloud secrets create github-pat --data-file=-      # fallback only
SECRETS="GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest"
SECRETS="${SECRETS},GITHUB_PRIVATE_KEY=github-private-key:latest"

echo "==> Deploying worker (private: only Pub/Sub push reaches it)"
gcloud run deploy pr-review-worker \
  --source=. --dockerfile=Dockerfile.worker \
  --region="$REGION" --service-account="$SA" \
  --no-allow-unauthenticated \
  --memory=2Gi --timeout=900 --concurrency=1 \
  --set-secrets="$SECRETS" \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},VERTEX_LOCATION=${REGION},GEMINI_MODEL=${GEMINI_MODEL:-},GITHUB_APP_ID=${GITHUB_APP_ID:-}"

WORKER_URL=$(gcloud run services describe pr-review-worker --region="$REGION" --format='value(status.url)')

echo "==> Deploying receiver (public: GitHub posts here)"
gcloud run deploy pr-review-receiver \
  --source=. --dockerfile=Dockerfile.receiver \
  --region="$REGION" --service-account="$SA" \
  --allow-unauthenticated \
  --memory=512Mi --timeout=60 \
  --set-secrets="$SECRETS" \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},PUBSUB_TOPIC=${TOPIC},GITHUB_APP_ID=${GITHUB_APP_ID:-}"

RECEIVER_URL=$(gcloud run services describe pr-review-receiver --region="$REGION" --format='value(status.url)')

echo "==> Push subscription with dead-lettering"
gcloud pubsub subscriptions create "$SUB" \
  --topic="$TOPIC" \
  --push-endpoint="${WORKER_URL}/jobs" \
  --push-auth-service-account="$SA" \
  --ack-deadline=600 \
  --dead-letter-topic="$DLQ" \
  --max-delivery-attempts=5 2>/dev/null \
  || gcloud pubsub subscriptions update "$SUB" \
       --push-endpoint="${WORKER_URL}/jobs" \
       --push-auth-service-account="$SA" \
       --ack-deadline=600 \
       --dead-letter-topic="$DLQ" \
       --max-delivery-attempts=5

# Pub/Sub's own service agent needs permission to push and to dead-letter.
PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
gcloud run services add-iam-policy-binding pr-review-worker \
  --region="$REGION" --member="serviceAccount:${PUBSUB_SA}" \
  --role=roles/run.invoker >/dev/null
gcloud pubsub topics add-iam-policy-binding "$DLQ" \
  --member="serviceAccount:${PUBSUB_SA}" --role=roles/pubsub.publisher >/dev/null

echo
echo "Receiver: ${RECEIVER_URL}"
echo "Point the GitHub App webhook at ${RECEIVER_URL}/webhook"
echo "Worker:   ${WORKER_URL}  (private)"
