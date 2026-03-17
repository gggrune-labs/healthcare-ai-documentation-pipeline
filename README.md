# Healthcare AI Documentation Pipeline

An end-to-end, production-grade pipeline that automates clinical SOAP note generation for a behavioral health practice. The system watches a Google Drive folder for therapy session transcripts, processes them through a Gemini 1.5 Pro prompt on Vertex AI, and posts structured SOAP notes directly to the practice's Salesforce contact records — fully HIPAA-compliant from intake to storage.

---

## Problem Solved

Therapists at a behavioral health practice spend 30–45 minutes after each session manually writing SOAP notes (Subjective, Objective, Assessment, Plan). These notes must be accurate, structured, and stored in the EHR (Salesforce) before the next appointment. The manual process was a bottleneck: documentation lagged, clinician burnout increased, and billing cycles were delayed.

This pipeline reduces post-session documentation time from ~40 minutes to under 2 minutes while maintaining clinician review and sign-off, full audit trails, and HIPAA compliance.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        TRIGGER LAYER                                │
│                                                                     │
│   Google Meet Session  →  .vtt Transcript  →  Drive: /Incoming      │
│                                                      │              │
│                          Cloud Scheduler (every 2m)  │              │
│                                    │                 │              │
│                                    ▼                 │              │
└─────────────────────────────────────────────────────────────────────┘
                                     │
┌─────────────────────────────────────────────────────────────────────┐
│                       ORCHESTRATION LAYER                           │
│                                                                     │
│              poll_drive (Cloud Function, Gen 2, HTTP)               │
│              • Authenticates via Workload Identity                  │
│              • Lists files in Drive /Incoming folder                │
│              • Publishes file metadata to Pub/Sub                   │
│                                    │                                │
│                                    ▼                                │
│              Pub/Sub Topic: transcript-ingestion                    │
│                                    │                                │
│                                    ▼                                │
│              process_transcript (Cloud Function, Gen 2, Pub/Sub)    │
│              • Triggered via Eventarc on message arrival            │
│              • Downloads & parses .vtt from Drive API               │
│              • Sends cleaned transcript to Gemini 1.5 Pro           │
│              • Posts structured SOAP JSON to Salesforce             │
│              • Moves file to /Processed, /Error, or /NoMatch        │
└─────────────────────────────────────────────────────────────────────┘
                                     │
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER                                 │
│                                                                     │
│   Vertex AI (Gemini 1.5 Pro)    Salesforce (Contact Records)        │
│   Secret Manager (credentials)  Cloud Logging (PHI-safe)           │
│   Cloud Storage (staging)       Cloud Audit Logs                   │
└─────────────────────────────────────────────────────────────────────┘
```

### Folder Routing Logic

| Outcome | Drive Destination | Reason |
|---|---|---|
| SOAP note posted successfully | `/Processed` | Nominal flow |
| No matching Salesforce contact | `/NoMatch` | Manual review required |
| Any processing error | `/Error` | Retry or escalation |

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Trigger | Google Meet + Drive | Session recording → transcript drop |
| Scheduler | Cloud Scheduler | Polls Drive every 2 minutes |
| Compute | Cloud Functions Gen 2 (Python 3.11) | Serverless, event-driven processing |
| Messaging | Pub/Sub + Eventarc | Decoupled, durable trigger between functions |
| AI / NLP | Vertex AI — Gemini 1.5 Pro | SOAP note generation from raw transcript |
| CRM / EHR | Salesforce REST API | Clinical note storage on contact records |
| Secrets | GCP Secret Manager | All credentials, zero env-var secrets |
| Logging | Cloud Logging (structured JSON) | PHI-safe audit trail |
| IAM | Dedicated service accounts (least privilege) | Zero standing permissions |
| Compliance | Signed BAAs: Google Workspace, GCP, Salesforce | HIPAA covered-entity alignment |

---

## HIPAA Compliance Summary

This system was designed for HIPAA compliance from the ground up — not retrofitted.

- **No PHI in logs.** A `sanitize_for_logging()` utility strips patient names, dates of birth, contact info, and session identifiers before any log entry is written.
- **Secrets in Secret Manager.** Zero credentials in environment variables, source code, or deployment configs. All API keys, OAuth tokens, and connection strings are fetched at runtime from GCP Secret Manager.
- **Least-privilege IAM.** Two dedicated service accounts — one for `poll_drive` (Drive read + Pub/Sub publish) and one for `process_transcript` (Drive read/write + Vertex AI + Salesforce Secret + Logging write). No shared or default service accounts.
- **Encryption in transit and at rest.** All GCP services use Google-managed encryption. Salesforce API calls use TLS 1.2+.
- **Signed BAAs on file** with Google Workspace, Google Cloud Platform, and Salesforce for HIPAA covered-entity compliance.
- **Audit logging.** Cloud Audit Logs capture all API calls to Drive, Pub/Sub, Vertex AI, and Secret Manager. Logs retained per HIPAA's 6-year minimum.
- **Access controls.** Google Drive folders are shared only with the designated service account. No broad sharing or public links.

See [docs/hipaa-compliance.md](docs/hipaa-compliance.md) for the full safeguard inventory.

---

## Repository Structure

```
healthcare-ai-documentation-pipeline/
├── README.md
├── docs/
│   ├── architecture.md          # Detailed pipeline flow and infrastructure
│   ├── design-decisions.md      # Why each technology was chosen
│   └── hipaa-compliance.md      # Full HIPAA safeguard inventory
└── src/
    ├── main.py                  # Cloud Function entry points
    ├── transcript_parser.py     # .vtt parsing and transcript cleaning
    ├── gemini_client.py         # Vertex AI / Gemini 1.5 Pro integration
    ├── salesforce_client.py     # Salesforce REST API with retry logic
    ├── utils.py                 # Secret Manager, logging, PHI sanitization
    └── requirements.txt         # Pinned production dependencies
```

---

## Configuration Placeholders

Before deploying, replace the following placeholders:

| Placeholder | Description |
|---|---|
| `YOUR_GCP_PROJECT_ID` | GCP project ID |
| `YOUR_GCP_REGION` | GCP region (e.g., `us-central1`) |
| `YOUR_PUBSUB_TOPIC` | Pub/Sub topic name for transcript events |
| `YOUR_DRIVE_INCOMING_FOLDER_ID` | Google Drive folder ID for incoming transcripts |
| `YOUR_DRIVE_PROCESSED_FOLDER_ID` | Google Drive folder ID for processed transcripts |
| `YOUR_DRIVE_ERROR_FOLDER_ID` | Google Drive folder ID for errored transcripts |
| `YOUR_DRIVE_NOMATCH_FOLDER_ID` | Google Drive folder ID for no-match transcripts |
| `YOUR_SALESFORCE_INSTANCE_URL` | Salesforce instance URL (e.g., `https://yourorg.my.salesforce.com`) |
| `YOUR_SALESFORCE_API_VERSION` | Salesforce API version (e.g., `v59.0`) |
| `YOUR_SECRET_SF_CLIENT_ID` | Secret Manager secret name for SF connected app client ID |
| `YOUR_SECRET_SF_CLIENT_SECRET` | Secret Manager secret name for SF client secret |
| `YOUR_SECRET_SF_USERNAME` | Secret Manager secret name for SF username |
| `YOUR_SECRET_SF_PASSWORD` | Secret Manager secret name for SF password + security token |
| `YOUR_POLL_DRIVE_SA` | Service account email for `poll_drive` function |
| `YOUR_PROCESS_TRANSCRIPT_SA` | Service account email for `process_transcript` function |

---

## Deployment Overview

```bash
# Deploy poll_drive
gcloud functions deploy poll_drive \
  --gen2 \
  --runtime=python311 \
  --region=YOUR_GCP_REGION \
  --source=./src \
  --entry-point=poll_drive \
  --trigger-http \
  --service-account=YOUR_POLL_DRIVE_SA \
  --no-allow-unauthenticated \
  --set-env-vars GCP_PROJECT=YOUR_GCP_PROJECT_ID

# Deploy process_transcript
gcloud functions deploy process_transcript \
  --gen2 \
  --runtime=python311 \
  --region=YOUR_GCP_REGION \
  --source=./src \
  --entry-point=process_transcript \
  --trigger-topic=YOUR_PUBSUB_TOPIC \
  --service-account=YOUR_PROCESS_TRANSCRIPT_SA \
  --set-env-vars GCP_PROJECT=YOUR_GCP_PROJECT_ID
```

---

## Skills Demonstrated

- **Cloud-native event-driven architecture** — Cloud Scheduler → HTTP Function → Pub/Sub → Eventarc → Pub/Sub Function, fully decoupled
- **LLM prompt engineering for structured output** — Gemini 1.5 Pro with JSON-mode constraints, few-shot examples, and clinical domain instructions
- **Healthcare data compliance** — HIPAA-aligned design including PHI-safe logging, Secret Manager, least-privilege IAM, and signed BAAs
- **Production Python** — Retry logic with exponential backoff, structured error handling, input validation, PHI sanitization
- **Multi-system API integration** — Google Drive API, Pub/Sub, Vertex AI SDK, Salesforce REST API, GCP Secret Manager

---

## License

This repository contains no PHI and no client-identifying information. All credentials, folder IDs, and instance URLs have been replaced with generic placeholders. The code reflects real production logic and architecture.
