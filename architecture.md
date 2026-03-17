# Architecture

## System Overview

The pipeline is a fully serverless, event-driven system on Google Cloud Platform. Its core principle is **fan-out via Pub/Sub**: a lightweight polling function discovers new work and publishes it; a heavier processing function handles the actual AI and API calls. This separation keeps the two concerns independently scalable, independently deployable, and independently observable.

---

## Component Inventory

### 1. Google Workspace (Source of Record)

- **Google Meet** records therapy sessions and generates `.vtt` (WebVTT) caption transcripts automatically when recording is enabled.
- Transcripts land in a designated **Google Drive folder** (`/Incoming`) shared exclusively with the `poll_drive` service account.
- No therapist action is required to initiate pipeline processing once recording ends.

### 2. Cloud Scheduler (Trigger)

- A Cloud Scheduler job fires an authenticated HTTP `POST` to the `poll_drive` Cloud Function every **2 minutes**.
- Uses OIDC token authentication targeting the `poll_drive` function's invoker audience — no public invocation allowed.
- Configured to retry on failure up to 3 times with exponential backoff.

**Why 2 minutes?** The practice's workflow requires notes available before the next back-to-back session. A 2-minute poll interval keeps end-to-end latency under 5 minutes while avoiding excessive Drive API quota consumption.

### 3. `poll_drive` — Cloud Function (Gen 2, HTTP-triggered)

**Runtime:** Python 3.11, Cloud Functions Gen 2
**Service Account:** `YOUR_POLL_DRIVE_SA`
**IAM Roles Required:**
- `roles/drive.readonly` on the `/Incoming` Drive folder (via Domain-Wide Delegation or direct share)
- `roles/pubsub.publisher` on the `transcript-ingestion` topic

**Responsibilities:**
1. Lists all `.vtt` files in the `/Incoming` Drive folder via the Drive API v3 `files.list` endpoint.
2. For each file found, publishes a Pub/Sub message to the `transcript-ingestion` topic containing:
   - `file_id` — Drive file ID
   - `file_name` — original filename
   - `mime_type` — verified as `text/vtt`
   - `published_at` — ISO 8601 UTC timestamp
3. Returns a `200 OK` with a count of files queued.

**Design note:** `poll_drive` is intentionally dumb — it does not deduplicate, does not inspect content, and does not move files. Idempotency is handled downstream by checking for a file's presence in `/Incoming` before processing begins in `process_transcript`.

### 4. Pub/Sub Topic: `transcript-ingestion`

- Decouples polling from processing. If `process_transcript` is slow or temporarily down, messages queue durably.
- Default message retention: 7 days.
- Subscriptions use **exactly-once delivery** mode where supported; downstream logic also handles duplicate detection.
- Eventarc connects this topic to the `process_transcript` function, converting each message into a Cloud Events invocation.

### 5. `process_transcript` — Cloud Function (Gen 2, Pub/Sub-triggered via Eventarc)

**Runtime:** Python 3.11, Cloud Functions Gen 2
**Service Account:** `YOUR_PROCESS_TRANSCRIPT_SA`
**IAM Roles Required:**
- `roles/drive.file` on the Drive folder tree (read file, move between folders)
- `roles/aiplatform.user` (Vertex AI inference)
- `roles/secretmanager.secretAccessor` on Salesforce credential secrets
- `roles/logging.logWriter`

**Responsibilities:**
1. **Parse event** — decode the Pub/Sub message from the Eventarc CloudEvent envelope.
2. **Fetch transcript** — download the `.vtt` file content from Drive via `files.get` with `alt=media`.
3. **Clean transcript** — strip `.vtt` formatting (timestamps, cue identifiers, WEBVTT header), normalize speaker labels, remove filler/overlap artifacts. See `transcript_parser.py`.
4. **Generate SOAP note** — send cleaned text to Gemini 1.5 Pro via the Vertex AI Python SDK. The prompt instructs the model to return a strict JSON object with keys: `subjective`, `objective`, `assessment`, `plan`, `session_duration_minutes`, `presenting_concerns`, `risk_assessment`. See `gemini_client.py`.
5. **Match Salesforce contact** — extract patient identifiers from the filename (a naming convention agreed with the practice). Query Salesforce via SOQL to find the matching `Contact` record.
6. **Post SOAP note** — create a Salesforce `Note` object (or custom clinical object, depending on org config) linked to the matched `Contact`, with the SOAP JSON stored in the note body.
7. **Route transcript** — move the Drive file to:
   - `/Processed` — on successful Salesforce post
   - `/NoMatch` — if no Salesforce contact was found
   - `/Error` — on any unhandled exception
8. **Log outcome** — write a structured JSON log entry with sanitized metadata (no PHI).

### 6. Vertex AI — Gemini 1.5 Pro

- Invoked via the `google-cloud-aiplatform` Python SDK using the `GenerativeModel` interface.
- The prompt is a multi-part structure:
  - **System instruction** — defines the model's role as a clinical documentation assistant and specifies output format constraints.
  - **Few-shot examples** — 2–3 representative transcript → SOAP note pairs (synthetic, no real PHI).
  - **User turn** — the cleaned transcript text.
- **Response schema enforcement** — `generation_config` specifies `response_mime_type="application/json"` to constrain output to parseable JSON.
- **Safety settings** — BLOCK_NONE on all harm categories (medical content would otherwise be incorrectly filtered).
- Temperature: `0.2` for consistency in clinical outputs.

### 7. Salesforce

- Authentication uses the **OAuth 2.0 Username-Password flow** (suitable for server-to-server integrations where the connected app is tightly controlled).
- All credentials fetched from Secret Manager at function startup — never stored in env vars.
- SOQL query matches by a patient identifier extracted from the transcript filename.
- Note creation uses the standard Salesforce REST API (`/services/data/vXX.X/sobjects/Note/`).
- Full retry logic with exponential backoff handles transient Salesforce 5xx errors and token refresh on 401.

### 8. GCP Secret Manager

- Stores: Salesforce client ID, client secret, username, password+token.
- Accessed via the `google-cloud-secret-manager` Python SDK using Application Default Credentials.
- Secrets versioned — pipeline always fetches `latest` version.
- Access audited via Cloud Audit Logs.

### 9. Cloud Logging

- Both functions write structured JSON logs using Python's `logging` module with a custom `JsonFormatter`.
- A `sanitize_for_logging()` function in `utils.py` runs on all user-controlled strings before they enter a log record.
- Log fields: `function_name`, `file_id`, `outcome`, `duration_ms`, `error_type` (never error message raw), `timestamp`.
- Log-based metrics and alerts configured on `outcome=ERROR` for on-call paging.

---

## Data Flow Sequence

```
 Cloud Scheduler
       │  POST (OIDC)
       ▼
 poll_drive CF
       │  Drive API v3 files.list
       ▼
 Google Drive /Incoming
       │  file_id, file_name
       ▼
 Pub/Sub: transcript-ingestion
       │  CloudEvent via Eventarc
       ▼
 process_transcript CF
       ├─► Drive API v3 files.get (download .vtt)
       ├─► transcript_parser.py (clean text)
       ├─► Vertex AI Gemini 1.5 Pro (generate SOAP JSON)
       ├─► Salesforce SOQL (match contact)
       ├─► Salesforce REST API (create Note)
       └─► Drive API v3 files.update (move to /Processed | /NoMatch | /Error)
```

---

## Error Handling and Retry Strategy

| Failure Point | Behavior |
|---|---|
| Drive API unavailable during poll | Cloud Scheduler retries up to 3× |
| Pub/Sub publish failure | Cloud Function returns 5xx → Scheduler retries |
| Drive API unavailable during process | Pub/Sub retries message delivery (Eventarc default: exponential, up to 7 days) |
| Gemini API error | Retry 3× with exponential backoff; move to `/Error` on exhaustion |
| Salesforce auth failure | Refresh token and retry once; move to `/Error` on second failure |
| Salesforce contact not found | Move transcript to `/NoMatch`; no retry (manual review required) |
| Unhandled exception | Move to `/Error`; structured log with sanitized error type |

---

## Scalability Notes

- **Cloud Functions Gen 2** supports up to 1,000 concurrent instances per region per function by default. The practice's volume (~20 sessions/day) puts this system at <1% of capacity.
- **Pub/Sub** provides horizontal fan-out if session volume grows significantly. Multiple `process_transcript` instances can process messages in parallel without coordination.
- **Gemini 1.5 Pro** quota on Vertex AI can be increased via support ticket; current throughput is well within free-tier + standard quota.
- **Salesforce API** limits (daily request quota) are monitored via Salesforce's API usage dashboard; current volume is <0.1% of org quota.

---

## Infrastructure Diagram (Text)

```
GCP Project: YOUR_GCP_PROJECT_ID
Region: YOUR_GCP_REGION

IAM
├── Service Account: YOUR_POLL_DRIVE_SA
│   ├── roles/pubsub.publisher          (transcript-ingestion topic)
│   └── Drive folder share              (/Incoming, read-only)
└── Service Account: YOUR_PROCESS_TRANSCRIPT_SA
    ├── roles/aiplatform.user
    ├── roles/secretmanager.secretAccessor
    ├── roles/logging.logWriter
    └── Drive folder share              (/Incoming, /Processed, /Error, /NoMatch)

Networking
└── All egress to Salesforce over public internet, TLS 1.2+
    (VPC-SC perimeter available for higher-security deployments)
```
