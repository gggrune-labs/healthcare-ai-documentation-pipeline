"""
main.py
-------
Cloud Function entry points for the healthcare AI documentation pipeline.

Entry points:
  poll_drive         — HTTP-triggered (Cloud Scheduler). Lists .vtt files in
                       the Google Drive /Incoming folder and publishes each
                       as a Pub/Sub message.

  process_transcript — Pub/Sub-triggered (via Eventarc). Processes a single
                       transcript: downloads from Drive, generates a SOAP note
                       via Gemini 1.5 Pro, posts to Salesforce, and routes
                       the file to the appropriate Drive folder.

Both functions are deployed to Cloud Functions Gen 2 (Python 3.11).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any

import functions_framework
from cloudevents.http import CloudEvent
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google.cloud import pubsub_v1

from gemini_client import GeminiClient
from salesforce_client import SalesforceClient
from transcript_parser import download_vtt_from_drive, parse_vtt
from utils import (
    DriveAPIError,
    GeminiError,
    SalesforceAPIError,
    SalesforceMatchError,
    TranscriptParseError,
    get_logger,
    sanitize_for_logging,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GCP_PROJECT: str = os.environ["GCP_PROJECT"]
GCP_REGION: str = os.environ.get("GCP_REGION", "YOUR_GCP_REGION")

PUBSUB_TOPIC: str = os.environ.get("PUBSUB_TOPIC", "YOUR_PUBSUB_TOPIC")
DRIVE_INCOMING_FOLDER_ID: str = os.environ.get("DRIVE_INCOMING_FOLDER_ID", "YOUR_DRIVE_INCOMING_FOLDER_ID")
DRIVE_PROCESSED_FOLDER_ID: str = os.environ.get("DRIVE_PROCESSED_FOLDER_ID", "YOUR_DRIVE_PROCESSED_FOLDER_ID")
DRIVE_ERROR_FOLDER_ID: str = os.environ.get("DRIVE_ERROR_FOLDER_ID", "YOUR_DRIVE_ERROR_FOLDER_ID")
DRIVE_NOMATCH_FOLDER_ID: str = os.environ.get("DRIVE_NOMATCH_FOLDER_ID", "YOUR_DRIVE_NOMATCH_FOLDER_ID")

# Filename convention: session_YYYY-MM-DD_P{patient_id}.vtt
_FILENAME_PATIENT_ID_RE = re.compile(
    r"^session_\d{4}-\d{2}-\d{2}_P(\w+)\.vtt$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Module-level singletons (reused across warm invocations)
# ---------------------------------------------------------------------------

_drive_service = None
_pubsub_publisher = None
_gemini_client: GeminiClient | None = None
_salesforce_client: SalesforceClient | None = None


def _get_drive_service():
    """Return a cached Google Drive API v3 service client."""
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", cache_discovery=False)
    return _drive_service


def _get_pubsub_publisher():
    """Return a cached Pub/Sub PublisherClient."""
    global _pubsub_publisher
    if _pubsub_publisher is None:
        _pubsub_publisher = pubsub_v1.PublisherClient()
    return _pubsub_publisher


def _get_gemini_client() -> GeminiClient:
    """Return a cached GeminiClient (initializes Vertex AI SDK once)."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient()
    return _gemini_client


def _get_salesforce_client() -> SalesforceClient:
    """Return a cached SalesforceClient."""
    global _salesforce_client
    if _salesforce_client is None:
        _salesforce_client = SalesforceClient()
    return _salesforce_client


# ---------------------------------------------------------------------------
# Entry Point: poll_drive
# ---------------------------------------------------------------------------


@functions_framework.http
def poll_drive(request) -> tuple[str, int]:
    """
    Cloud Function: HTTP-triggered by Cloud Scheduler every 2 minutes.

    Lists all .vtt files in the Google Drive /Incoming folder and publishes
    a Pub/Sub message for each file found.

    Returns:
        A JSON response body and HTTP status code.
        200: Normal completion (0 or more files queued).
        500: Unexpected error during Drive listing or Pub/Sub publishing.
    """
    start_time = time.monotonic()
    logger.info("poll_drive invoked", extra={"folder_id": DRIVE_INCOMING_FOLDER_ID})

    try:
        drive = _get_drive_service()
        files = _list_incoming_vtt_files(drive)
    except Exception as exc:
        logger.error(
            "poll_drive: Drive listing failed",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        return json.dumps({"status": "error", "error_type": type(exc).__name__}), 500

    queued = 0
    errors = 0
    for file_meta in files:
        try:
            _publish_file_event(file_meta)
            queued += 1
        except Exception as exc:
            logger.error(
                "poll_drive: Failed to publish Pub/Sub message",
                extra={
                    "file_id": file_meta.get("id", ""),
                    "error_type": type(exc).__name__,
                },
            )
            errors += 1

    duration_ms = round((time.monotonic() - start_time) * 1000)
    logger.info(
        "poll_drive complete",
        extra={
            "files_queued": queued,
            "publish_errors": errors,
            "duration_ms": duration_ms,
        },
    )

    return (
        json.dumps({"status": "ok", "files_queued": queued, "publish_errors": errors}),
        200,
    )


def _list_incoming_vtt_files(drive) -> list[dict[str, str]]:
    """
    List all .vtt files currently in the /Incoming Drive folder.

    Uses a MIME type filter to avoid processing non-transcript files and
    handles pagination for large folder contents.

    Returns:
        List of dicts with 'id', 'name', and 'mimeType' keys.
    """
    query = (
        f"'{DRIVE_INCOMING_FOLDER_ID}' in parents "
        f"and mimeType = 'text/vtt' "
        f"and trashed = false"
    )
    files: list[dict[str, str]] = []
    page_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "q": query,
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType, createdTime)",
            "pageSize": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = drive.files().list(**kwargs).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info(
        "Drive /Incoming folder listed",
        extra={"vtt_file_count": len(files)},
    )
    return files


def _publish_file_event(file_meta: dict[str, str]) -> None:
    """
    Publish a Pub/Sub message for a single Drive file.

    The message payload contains only non-PHI identifiers. Transcript
    content is never placed in a Pub/Sub message.

    Args:
        file_meta: Dict with 'id', 'name', 'mimeType' from Drive API.
    """
    publisher = _get_pubsub_publisher()
    topic_path = publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)

    from datetime import datetime, timezone
    message = {
        "file_id": file_meta["id"],
        "file_name": file_meta["name"],
        "mime_type": file_meta.get("mimeType", "text/vtt"),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(message).encode("utf-8")
    future = publisher.publish(topic_path, data)
    future.result(timeout=10)  # Block until confirmed


# ---------------------------------------------------------------------------
# Entry Point: process_transcript
# ---------------------------------------------------------------------------


@functions_framework.cloud_event
def process_transcript(cloud_event: CloudEvent) -> None:
    """
    Cloud Function: Pub/Sub-triggered via Eventarc.

    Processes a single therapy session transcript end-to-end:
    1. Parse the Pub/Sub message from the CloudEvent envelope.
    2. Download the .vtt transcript from Google Drive.
    3. Clean and structure the transcript text.
    4. Generate a SOAP note via Gemini 1.5 Pro on Vertex AI.
    5. Find the matching Salesforce Contact by patient identifier.
    6. Post the SOAP note as a Salesforce Note record.
    7. Move the transcript to /Processed, /NoMatch, or /Error.

    Raises:
        Exception: Unhandled exceptions propagate to Eventarc, which will
            retry delivery per the subscription's retry policy.
    """
    start_time = time.monotonic()
    file_id = ""
    file_name = ""
    outcome = "error"

    try:
        # ---- Step 1: Parse Pub/Sub event --------------------------------
        event_data = _parse_cloud_event(cloud_event)
        file_id = event_data["file_id"]
        file_name = event_data["file_name"]

        logger.info(
            "process_transcript started",
            extra={"file_id": file_id},
        )

        # ---- Step 2: Validate filename convention -----------------------
        patient_id = _extract_patient_id(file_name)
        if not patient_id:
            logger.warning(
                "Filename does not match expected convention — routing to NoMatch",
                extra={"file_id": file_id},
            )
            _move_file(file_id, DRIVE_NOMATCH_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
            outcome = "no_match"
            return

        # ---- Step 3: Download and parse transcript ----------------------
        drive = _get_drive_service()
        raw_bytes = download_vtt_from_drive(drive, file_id)
        parsed = parse_vtt(raw_bytes, file_name=file_name)

        # ---- Step 4: Generate SOAP note via Gemini ----------------------
        gemini = _get_gemini_client()
        soap_note = gemini.generate_soap_note(
            transcript_text=parsed.full_text,
            session_duration_minutes=round(parsed.duration_seconds / 60),
            file_id=file_id,
        )

        # ---- Step 5: Find Salesforce contact ----------------------------
        sf = _get_salesforce_client()
        contact = sf.find_contact_by_patient_id(patient_id)

        # ---- Step 6: Post SOAP note to Salesforce -----------------------
        note_result = sf.post_soap_note(
            contact=contact,
            soap_note=soap_note,
            file_name=file_name,
        )

        # ---- Step 7: Move transcript to /Processed ----------------------
        _move_file(file_id, DRIVE_PROCESSED_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "processed"

        duration_ms = round((time.monotonic() - start_time) * 1000)
        logger.info(
            "process_transcript complete",
            extra={
                "outcome": outcome,
                "file_id": file_id,
                "note_id": note_result.note_id,
                "contact_id": contact.contact_id,
                "patient_id": patient_id,
                "duration_ms": duration_ms,
                "word_count": parsed.word_count,
                "session_duration_minutes": soap_note.session_duration_minutes,
            },
        )

    except SalesforceMatchError:
        logger.warning(
            "No Salesforce contact matched — routing transcript to /NoMatch",
            extra={"file_id": file_id},
        )
        _safe_move_file(file_id, DRIVE_NOMATCH_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "no_match"

    except TranscriptParseError as exc:
        logger.error(
            "Transcript parse error — routing to /Error",
            extra={"file_id": file_id, "error_type": "TranscriptParseError"},
        )
        _safe_move_file(file_id, DRIVE_ERROR_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "error"
        raise  # Re-raise so Eventarc knows the invocation failed

    except GeminiError as exc:
        logger.error(
            "Gemini API error — routing to /Error",
            extra={"file_id": file_id, "error_type": "GeminiError"},
        )
        _safe_move_file(file_id, DRIVE_ERROR_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "error"
        raise

    except SalesforceAPIError as exc:
        logger.error(
            "Salesforce API error — routing to /Error",
            extra={"file_id": file_id, "error_type": "SalesforceAPIError"},
        )
        _safe_move_file(file_id, DRIVE_ERROR_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "error"
        raise

    except Exception as exc:
        logger.error(
            "Unhandled error in process_transcript — routing to /Error",
            extra={"file_id": file_id, "error_type": type(exc).__name__},
            exc_info=True,
        )
        _safe_move_file(file_id, DRIVE_ERROR_FOLDER_ID, DRIVE_INCOMING_FOLDER_ID)
        outcome = "error"
        raise

    finally:
        duration_ms = round((time.monotonic() - start_time) * 1000)
        logger.info(
            "process_transcript finished",
            extra={"outcome": outcome, "file_id": file_id, "duration_ms": duration_ms},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_cloud_event(cloud_event: CloudEvent) -> dict[str, str]:
    """
    Extract and decode the Pub/Sub message payload from an Eventarc CloudEvent.

    Eventarc wraps Pub/Sub messages in a CloudEvent. The message data is
    base64-encoded in cloud_event.data["message"]["data"].

    Returns:
        Dict with file_id, file_name, mime_type, published_at.

    Raises:
        ValueError: If the event data is malformed.
    """
    try:
        pubsub_message = cloud_event.data["message"]
        raw = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        return json.loads(raw)
    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Malformed CloudEvent payload: {exc}") from exc


def _extract_patient_id(file_name: str) -> str | None:
    """
    Extract the patient identifier from a transcript filename.

    Expected format: session_YYYY-MM-DD_P{patient_id}.vtt

    Returns:
        The patient ID string (e.g., "12345"), or None if the filename
        does not match the expected convention.
    """
    match = _FILENAME_PATIENT_ID_RE.match(file_name)
    if match:
        return match.group(1)
    return None


def _move_file(
    file_id: str,
    destination_folder_id: str,
    source_folder_id: str,
) -> None:
    """
    Move a Google Drive file from one folder to another atomically.

    Uses the Drive API's `addParents`/`removeParents` parameters on a
    `files.update` call, which is atomic on Google Drive.

    Args:
        file_id: Drive file ID to move.
        destination_folder_id: Target folder ID.
        source_folder_id: Source folder ID to remove from parents.

    Raises:
        DriveAPIError: If the move fails after retries.
    """
    drive = _get_drive_service()

    def _do_move():
        drive.files().update(
            fileId=file_id,
            addParents=destination_folder_id,
            removeParents=source_folder_id,
            fields="id, parents",
        ).execute()

    try:
        from utils import retry_with_backoff
        retry_with_backoff(_do_move, max_attempts=3, base_delay=1.0, logger=logger)
    except Exception as exc:
        raise DriveAPIError(
            f"Failed to move file {file_id} to folder {destination_folder_id}"
        ) from exc


def _safe_move_file(
    file_id: str,
    destination_folder_id: str,
    source_folder_id: str,
) -> None:
    """
    Move a file to an error/no-match folder, logging but not re-raising on failure.

    Used in exception handlers where we don't want a secondary failure to
    mask the original error.
    """
    if not file_id:
        return
    try:
        _move_file(file_id, destination_folder_id, source_folder_id)
    except Exception as exc:
        logger.error(
            "Failed to move file after processing error — file remains in /Incoming",
            extra={"file_id": file_id, "error_type": type(exc).__name__},
        )
