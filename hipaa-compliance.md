# HIPAA Compliance

This document enumerates the administrative, technical, and physical safeguards implemented in the pipeline, mapped to the relevant HIPAA Security Rule standard where applicable.

---

## Covered Entity Context

This pipeline processes **Protected Health Information (PHI)** in the form of:
- Therapy session transcripts (patient speech, clinician speech)
- Patient identifiers used to match Salesforce contact records
- Clinical notes (SOAP format) attached to patient records

The pipeline operates as a **Business Associate** of the covered entity (behavioral health practice). Business Associate Agreements (BAAs) are in place with all subcontractors that handle or store PHI:

| Vendor | BAA Status | PHI Scope |
|---|---|---|
| Google Workspace (Drive, Meet) | BAA signed | Transcript storage, video recording |
| Google Cloud Platform | BAA signed | Compute, Pub/Sub, Secret Manager, Logging, Vertex AI |
| Salesforce | BAA signed | Clinical note storage, patient records |

---

## Technical Safeguards (§ 164.312)

### Access Control (§ 164.312(a)(1))

**Unique user identification:** Each Cloud Function runs under a dedicated, named service account:
- `YOUR_POLL_DRIVE_SA` — used exclusively by `poll_drive`. Has no access to Vertex AI, Secret Manager, or Salesforce credentials.
- `YOUR_PROCESS_TRANSCRIPT_SA` — used exclusively by `process_transcript`. Has no ability to directly invoke the polling function or modify Cloud Scheduler.

**Minimum necessary access (least privilege):**

| Service Account | Permitted Actions | Denied Actions |
|---|---|---|
| `YOUR_POLL_DRIVE_SA` | List/read files in `/Incoming`; publish to Pub/Sub | Move files; call Vertex AI; access secrets |
| `YOUR_PROCESS_TRANSCRIPT_SA` | Read files in `/Incoming`; move files to `/Processed`, `/Error`, `/NoMatch`; call Vertex AI; access Salesforce secrets; write logs | Modify IAM; access other projects; publish to Pub/Sub |

**Automatic logoff:** Cloud Functions are stateless and ephemeral. Credentials (service account tokens) expire and are automatically rotated by GCP. There is no persistent session to time out.

**Encryption and decryption:** No custom encryption layer is implemented because GCP provides AES-256 encryption at rest for all storage services (Cloud Storage, Pub/Sub, Secret Manager, Cloud Logging) and TLS 1.2+ for all data in transit by default.

---

### Audit Controls (§ 164.312(b))

**Cloud Audit Logs** are enabled for all GCP services used:
- **Data Access logs** for Drive API, Pub/Sub, Secret Manager, Vertex AI, and Cloud Logging record every read and write operation with caller identity, timestamp, and resource identifier.
- **Admin Activity logs** record all IAM changes, service configuration changes, and function deployments.
- **System Event logs** record GCP-initiated actions (e.g., function instance creation/termination).

Audit logs are retained for a minimum of **6 years** (HIPAA minimum) via a log sink to Cloud Storage with a lifecycle policy. Logs are stored in a locked bucket (Object Versioning + Retention Policy) to prevent deletion.

**Application-level audit trail:** Every invocation of `process_transcript` writes a structured log entry recording:
- `file_id` (non-PHI Drive identifier)
- `outcome` (`processed`, `no_match`, `error`)
- `salesforce_contact_id` (non-PHI Salesforce identifier, not patient name)
- `duration_ms`
- `function_name`, `function_version`, `timestamp`

---

### Integrity (§ 164.312(c)(1))

**PHI-in-transit integrity:** All API calls (Drive, Pub/Sub, Vertex AI, Salesforce) use HTTPS/TLS, which provides both encryption and integrity verification via TLS MAC.

**PHI-at-rest integrity:** GCP and Salesforce both provide checksums and integrity validation for stored data. The pipeline does not modify transcript files in place — it moves them atomically to destination folders after processing, preserving the original content.

**Transcript provenance:** The `file_id` from Google Drive is passed through the entire pipeline and recorded in the final Salesforce note, creating a traceable link from clinical note back to source transcript.

---

### Transmission Security (§ 164.312(e)(1))

- All Google API calls use HTTPS enforced by the GCP SDK (no plaintext fallback).
- Salesforce API calls use HTTPS with TLS 1.2+ enforced by the `simple-salesforce` library.
- Pub/Sub message payloads contain only non-PHI file identifiers (Drive file IDs). Transcript content is never placed in a Pub/Sub message.
- No PHI is transmitted to any system not covered by a BAA.

---

## Administrative Safeguards (§ 164.308)

### Security Management Process (§ 164.308(a)(1))

- All secrets (Salesforce credentials) are stored in GCP Secret Manager with access restricted to the `process_transcript` service account. Secret access is audited.
- A **vulnerability management process** is implemented via dependency pinning (`requirements.txt`) and regular dependency updates reviewed against CVE advisories.
- Incident response: any `outcome=ERROR` log event triggers a Cloud Monitoring alert. The on-call engineer reviews the error, determines if PHI was exposed, and follows the covered entity's breach notification procedures if applicable.

### Workforce Training and Access Management (§ 164.308(a)(3), §164.308(a)(5))

- This pipeline operates without human access to PHI in the processing path. No engineer has access to the contents of therapy transcripts in production.
- Google Drive access to the `/Incoming`, `/Processed`, `/Error`, and `/NoMatch` folders is restricted to the two service accounts. No individual user accounts have access.
- Deployment to Cloud Functions requires `roles/cloudfunctions.developer` which is restricted to authorized engineers in the GCP IAM policy.

### Contingency Plan (§ 164.308(a)(7))

- **Data backup:** Google Drive's native redundancy protects transcript files. Salesforce provides its own data redundancy and backup.
- **Disaster recovery:** Cloud Functions are regionally redundant within the configured GCP region. If the region becomes unavailable, Cloud Scheduler jobs can be redeployed to a secondary region in under 30 minutes.
- **Manual fallback:** If the pipeline is unavailable, therapists revert to manual SOAP note entry in Salesforce. The pipeline is an efficiency tool, not a gate on clinical documentation.

---

## Physical Safeguards (§ 164.310)

Physical safeguards are delegated entirely to Google Cloud Platform and Salesforce under their respective BAAs. GCP data centers are SOC 2 Type II, ISO 27001, and HITRUST CSF certified. Salesforce is similarly certified.

No PHI is ever written to a developer's local machine, CI/CD environment, or any system outside the GCP project and Salesforce org.

---

## PHI Handling in Code

### What is NOT logged

The `sanitize_for_logging()` function in `utils.py` removes the following before any string enters a log record:
- Patient names (first, last, full)
- Dates of birth
- Phone numbers
- Email addresses
- Social Security Numbers
- Session content (transcript text, note text)
- File names that contain patient identifiers

### What IS logged

- Non-PHI Drive file IDs (opaque identifiers)
- Non-PHI Salesforce contact IDs (opaque identifiers)
- Outcome codes (`processed`, `no_match`, `error`)
- Error type categories (e.g., `SalesforceAuthError`, `GeminiTimeoutError`) — never raw error messages that might contain PHI
- Timing metrics (duration in milliseconds)
- Function metadata (name, version, region)

### Pub/Sub Message Contents

Pub/Sub messages contain only:
```json
{
  "file_id": "1aBcDeFgHiJkLmNoPqRsTuVwXyZ",
  "file_name": "session_2024-01-15_P12345.vtt",
  "mime_type": "text/vtt",
  "published_at": "2024-01-15T14:30:00Z"
}
```

Transcript content is never placed in a Pub/Sub message. The `file_id` is used to fetch the transcript directly from Drive within the `process_transcript` function's execution context.

---

## Security Controls Summary

| Control | Implementation | HIPAA Mapping |
|---|---|---|
| Credential management | GCP Secret Manager, no env-var secrets | § 164.312(a)(2)(iv) |
| Least-privilege IAM | Dedicated service accounts, granular role bindings | § 164.312(a)(1) |
| PHI-safe logging | `sanitize_for_logging()` strips PHI pre-write | § 164.312(b) |
| Audit trail | Cloud Audit Logs + application logs, 6yr retention | § 164.312(b) |
| Encryption in transit | TLS 1.2+ on all API calls (GCP + Salesforce) | § 164.312(e)(1) |
| Encryption at rest | GCP AES-256, Salesforce native encryption | § 164.312(a)(2)(iv) |
| BAAs | Google Workspace, GCP, Salesforce | § 164.308(b)(1) |
| PHI data minimization | No PHI in Pub/Sub, no PHI in logs | § 164.514(b) |
| Access logging | Cloud Audit Logs on Secret Manager, Drive, Vertex AI | § 164.312(b) |
| Incident alerting | Cloud Monitoring alert on `outcome=ERROR` | § 164.308(a)(6) |
