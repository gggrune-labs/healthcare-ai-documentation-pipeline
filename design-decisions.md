# Design Decisions

This document explains the key architectural and technology choices made in the pipeline, including alternatives considered and the reasoning behind each decision.

---

## 1. Cloud Functions Gen 2 over Cloud Run or App Engine

**Decision:** Use Cloud Functions Gen 2 (backed by Cloud Run internally) rather than a persistent Cloud Run service or App Engine instance.

**Reasoning:**
- The workload is inherently event-driven and bursty. Sessions happen throughout the day, not continuously. A persistent service would idle for the majority of every hour.
- Cloud Functions Gen 2 provides a true serverless execution model with per-invocation billing, sub-second cold starts on Python 3.11, and native Eventarc integration.
- Operational overhead is minimal — no container image management, no health check configuration, no autoscaling policy tuning.

**Alternatives considered:**
- **Cloud Run (always-on):** More control, but unjustified for this traffic pattern. Would have added container registry management with no benefit.
- **App Engine Standard:** Mature but less integrated with modern GCP event primitives (Eventarc, Pub/Sub push). Deployment model is heavier.
- **Cloud Run Jobs:** Good for batch but not event-triggered; doesn't fit the Pub/Sub trigger pattern cleanly.

---

## 2. Pub/Sub + Eventarc over Direct HTTP Invocation

**Decision:** `poll_drive` publishes to a Pub/Sub topic; Eventarc triggers `process_transcript` from that topic rather than `poll_drive` calling `process_transcript` via HTTP.

**Reasoning:**
- **Durability.** If `process_transcript` is unavailable (cold start, throttle, transient error), Pub/Sub holds the message for up to 7 days and retries delivery. A direct HTTP call would need its own retry logic and would lose the message on `poll_drive`'s exit.
- **Decoupling.** `poll_drive` completes in milliseconds regardless of how long `process_transcript` takes. Gemini inference can take 10–30 seconds; that cost is paid asynchronously.
- **Fan-out.** If multiple processing strategies are ever needed (e.g., a separate function for intake notes vs. progress notes), additional Pub/Sub subscriptions can be added without modifying `poll_drive`.
- **Observability.** Pub/Sub provides built-in metrics: undelivered message count, oldest undelivered message age, and delivery latency — all without custom instrumentation.

**Alternative considered:** Direct HTTP call from `poll_drive` to `process_transcript` via Cloud Tasks. Cloud Tasks adds queuing and retry semantics, but Pub/Sub + Eventarc is more idiomatic for GCP event-driven architectures and requires less boilerplate.

---

## 3. Gemini 1.5 Pro over GPT-4 or Fine-Tuned Model

**Decision:** Use Gemini 1.5 Pro via Vertex AI rather than OpenAI GPT-4 via the OpenAI API, or a fine-tuned open-source model (e.g., Llama 3 on Vertex AI).

**Reasoning:**
- **HIPAA alignment.** The entire stack runs on GCP, where a signed BAA is already in place with Google. Adding OpenAI would require a separate OpenAI Business Associate Agreement and PHI data flowing out of the GCP trust boundary — increasing compliance surface area.
- **Long context window.** Gemini 1.5 Pro's 1M-token context window comfortably handles even hour-long therapy session transcripts (typically 8,000–20,000 tokens). GPT-4 Turbo's 128K window would also suffice, but would incur the HIPAA risk above.
- **JSON mode.** Gemini supports `response_mime_type="application/json"` natively, eliminating JSON extraction hacks and reducing parse failures in production.
- **Vertex AI SDK integration.** The `google-cloud-aiplatform` SDK handles auth via Application Default Credentials, the same mechanism used everywhere else in the stack — no extra credential management.

**Alternatives considered:**
- **GPT-4 Turbo (OpenAI API):** Excellent quality, but requires routing PHI outside the GCP BAA perimeter. Rejected on compliance grounds.
- **Fine-tuned Llama 3 on Vertex AI Model Garden:** Would provide tighter control and potentially lower inference cost at scale, but requires a labeled training dataset (which didn't exist), ongoing fine-tuning maintenance, and a longer initial build time. The practice's volume does not justify the operational overhead.
- **Med-PaLM 2:** Google's medical-domain model. Not yet generally available on Vertex AI at the time of build; Gemini 1.5 Pro with a carefully crafted clinical prompt performs comparably on SOAP generation tasks.

---

## 4. Salesforce as the Clinical Note Store

**Decision:** Post SOAP notes as `Note` objects on Salesforce `Contact` records, treating Salesforce as the system of record for clinical documentation.

**Reasoning:**
- The practice already uses Salesforce Health Cloud as its CRM and patient management system. Storing notes there means therapists see documentation in the same tool they use for scheduling, billing, and patient history — no workflow change required.
- Salesforce's role-based access controls, field-level security, and sharing rules satisfy the "minimum necessary access" principle of HIPAA's Privacy Rule without any additional infrastructure.
- Salesforce is a HIPAA-eligible service with a signed BAA available.

**Alternative considered:** Write notes to a GCS bucket or Cloud SQL database and build a separate viewer. Rejected because it would fragment the therapist's workflow — they'd need to check a second system — and would require building access controls from scratch.

---

## 5. GCP Secret Manager over Environment Variables or a Vault Instance

**Decision:** All credentials (Salesforce OAuth tokens, API keys) are stored in and fetched from GCP Secret Manager at function startup.

**Reasoning:**
- Environment variables in Cloud Functions are visible in the GCP Console deployment config, in CI/CD logs, and potentially in crash dumps. They are not suitable for PHI-adjacent credentials under HIPAA.
- Secret Manager provides per-secret IAM access controls, automatic audit logging of every access, versioning with rollback, and rotation support — all without any additional infrastructure to operate.
- Alternative: HashiCorp Vault. Powerful, but requires a dedicated VM or cluster to operate and maintain. Secret Manager provides equivalent security for this use case at zero operational overhead.

---

## 6. WebVTT (.vtt) Transcript Format

**Decision:** Ingest transcripts in WebVTT format as written by Google Meet.

**Reasoning:**
- Google Meet exports captions exclusively in WebVTT format. Building an additional conversion step (e.g., to plain text, SRT, or JSON) would add latency and a failure mode with no benefit — the parser strips VTT formatting before sending to Gemini anyway.
- VTT timestamps provide metadata useful for detecting session duration, long silences, and speaker turn patterns — all of which are included in `transcript_parser.py`'s output.

---

## 7. Python 3.11 over Node.js or Go

**Decision:** Python 3.11 for both Cloud Functions.

**Reasoning:**
- The `google-cloud-aiplatform` Vertex AI SDK and `simple-salesforce` library both have mature, well-documented Python clients. The equivalent Node.js or Go clients for Vertex AI are thinner and less idiomatically integrated.
- The data transformation work (parsing VTT, cleaning text, structuring JSON) is more ergonomic in Python.
- ML and data engineering team familiarity.

---

## 8. Cloud Scheduler over Eventarc on Drive Notifications

**Decision:** Use Cloud Scheduler to poll Drive on a fixed 2-minute interval rather than registering a Drive push notification (webhook) to trigger the pipeline on new file creation.

**Reasoning:**
- **Drive push notifications require a public HTTPS endpoint** registered with a domain verification step. Managing this endpoint's certificate, domain, and reliability adds operational surface area.
- Push notifications have a **maximum TTL of 7 days** and must be renewed programmatically — a maintenance burden.
- At a 2-minute poll interval, the latency difference between polling and push is negligible for this use case (notes are not urgently needed in under 2 minutes).
- Polling is also more robust to partial failures: if Drive is slow or a push notification is lost, the file is simply picked up on the next poll cycle.

**Trade-off acknowledged:** At high file volumes (hundreds per hour), polling becomes inefficient compared to push. The current practice volume of ~20 sessions/day makes this trade-off clearly correct.

---

## 9. PHI-Safe Logging over Redaction-at-Storage

**Decision:** Strip PHI from log entries in application code (`sanitize_for_logging()` in `utils.py`) before the log is written, rather than relying on a downstream log redaction service (e.g., Cloud DLP on log exports).

**Reasoning:**
- Redaction-at-storage still exposes PHI briefly in the logging pipeline (Cloud Logging ingestion buffers). Under HIPAA, this is a meaningful risk.
- Application-level sanitization ensures PHI never enters the logging infrastructure at all — no temporary exposure, no reliance on a second service's availability or correctness.
- Cloud DLP adds cost proportional to log volume and introduces processing latency. For this application's log structure, known PHI fields can be sanitized deterministically in code.

**Trade-off:** The sanitizer must be kept in sync with new data fields added to the application. A test suite validates that the sanitizer catches all known PHI patterns.
