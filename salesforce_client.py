"""
salesforce_client.py
--------------------
Salesforce REST API integration for posting SOAP notes to Contact records.

Responsibilities:
- Authenticate via OAuth 2.0 Username-Password flow (server-to-server)
- Query Contact records by patient identifier (SOQL)
- Create Note objects linked to the matched Contact
- Handle token refresh and retry on transient errors
- Expose a clean interface to main.py without leaking Salesforce internals
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry

from gemini_client import SOAPNote
from utils import (
    SalesforceAPIError,
    SalesforceMatchError,
    get_logger,
    get_secret,
    retry_with_backoff,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SF_INSTANCE_URL: str = os.environ.get("SF_INSTANCE_URL", "YOUR_SALESFORCE_INSTANCE_URL")
SF_API_VERSION: str = os.environ.get("SF_API_VERSION", "YOUR_SALESFORCE_API_VERSION")

# Secret Manager secret names — actual values fetched at runtime
SECRET_SF_CLIENT_ID: str = os.environ.get("SECRET_SF_CLIENT_ID", "YOUR_SECRET_SF_CLIENT_ID")
SECRET_SF_CLIENT_SECRET: str = os.environ.get("SECRET_SF_CLIENT_SECRET", "YOUR_SECRET_SF_CLIENT_SECRET")
SECRET_SF_USERNAME: str = os.environ.get("SECRET_SF_USERNAME", "YOUR_SECRET_SF_USERNAME")
SECRET_SF_PASSWORD: str = os.environ.get("SECRET_SF_PASSWORD", "YOUR_SECRET_SF_PASSWORD")

SF_TOKEN_URL: str = f"{SF_INSTANCE_URL}/services/oauth2/token"
SF_SOBJECTS_URL: str = f"{SF_INSTANCE_URL}/services/data/{SF_API_VERSION}/sobjects"
SF_QUERY_URL: str = f"{SF_INSTANCE_URL}/services/data/{SF_API_VERSION}/query"

# SOQL field used to match transcripts to Salesforce contacts.
# The practice's filename convention encodes patient ID (e.g., P12345),
# which maps to a custom field on the Contact object.
SF_PATIENT_ID_FIELD: str = "Patient_ID__c"

# Maximum characters for Salesforce Note body (standard limit)
SF_NOTE_BODY_MAX_CHARS: int = 32_000

# HTTP adapter with connection-level retry (for network blips, not 4xx/5xx)
_SESSION_RETRY = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PATCH"],
)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class SalesforceContact:
    """Minimal Contact record representation."""

    contact_id: str       # Salesforce record ID (18-char)
    patient_id: str       # Practice's patient identifier (non-PHI in logs)


@dataclass
class NoteCreationResult:
    """Result of a Note object creation in Salesforce."""

    note_id: str                # Salesforce Note record ID
    contact_id: str             # Parent Contact record ID
    created_at: str             # ISO 8601 UTC timestamp


# ---------------------------------------------------------------------------
# Salesforce Client
# ---------------------------------------------------------------------------


class SalesforceClient:
    """
    Client for Salesforce REST API operations.

    Authentication uses the OAuth 2.0 Username-Password flow via a
    Connected App. All credentials are fetched from GCP Secret Manager
    on first use and cached for the lifetime of the function instance.

    Token refresh is handled automatically: on a 401 response the client
    re-authenticates once and retries the failed request before raising.
    """

    def __init__(self) -> None:
        self._session = self._build_session()
        self._access_token: str | None = None
        self._auth_headers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public Interface
    # ------------------------------------------------------------------

    def find_contact_by_patient_id(self, patient_id: str) -> SalesforceContact:
        """
        Query Salesforce for a Contact record matching the given patient ID.

        Args:
            patient_id: The practice's patient identifier extracted from the
                transcript filename (e.g., "P12345").

        Returns:
            SalesforceContact with the record ID.

        Raises:
            SalesforceMatchError: If no matching Contact is found.
            SalesforceAPIError: If the SOQL query fails.
        """
        self._ensure_authenticated()
        soql = (
            f"SELECT Id, {SF_PATIENT_ID_FIELD} "
            f"FROM Contact "
            f"WHERE {SF_PATIENT_ID_FIELD} = '{_escape_soql(patient_id)}' "
            f"LIMIT 1"
        )
        result = self._soql_query(soql)
        records = result.get("records", [])

        if not records:
            raise SalesforceMatchError(
                f"No Contact found for patient_id={patient_id}"
            )

        contact = records[0]
        return SalesforceContact(
            contact_id=contact["Id"],
            patient_id=patient_id,
        )

    def post_soap_note(
        self,
        contact: SalesforceContact,
        soap_note: SOAPNote,
        file_name: str = "",
    ) -> NoteCreationResult:
        """
        Create a Salesforce Note linked to the given Contact.

        The SOAP note JSON is serialized into the Note body. If the
        serialized body exceeds Salesforce's character limit, a truncated
        version is stored with a warning appended.

        Args:
            contact: SalesforceContact record to attach the note to.
            soap_note: Generated SOAP note content.
            file_name: Original transcript filename (for the Note title).

        Returns:
            NoteCreationResult with the new Note record ID.

        Raises:
            SalesforceAPIError: If Note creation fails after retries.
        """
        self._ensure_authenticated()
        title = _build_note_title(file_name)
        body = _serialize_note_body(soap_note)

        payload = {
            "Title": title,
            "Body": body,
            "ParentId": contact.contact_id,
            "IsPrivate": False,
        }

        result = retry_with_backoff(
            self._create_note,
            payload,
            max_attempts=3,
            base_delay=2.0,
            retryable_exceptions=(SalesforceAPIError, RequestException),
            logger=logger,
        )

        logger.info(
            "Salesforce Note created",
            extra={
                "note_id": result.note_id,
                "contact_id": contact.contact_id,
                "patient_id": contact.patient_id,
            },
        )
        return result

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _ensure_authenticated(self) -> None:
        """Authenticate if we don't have a token yet."""
        if not self._access_token:
            self._authenticate()

    def _authenticate(self) -> None:
        """
        Perform OAuth 2.0 Username-Password flow to obtain an access token.

        Fetches all credentials from Secret Manager. Sets the Authorization
        header on the shared session for subsequent requests.

        Raises:
            SalesforceAPIError: If authentication fails.
        """
        try:
            client_id = get_secret(SECRET_SF_CLIENT_ID)
            client_secret = get_secret(SECRET_SF_CLIENT_SECRET)
            username = get_secret(SECRET_SF_USERNAME)
            password = get_secret(SECRET_SF_PASSWORD)  # password + security token
        except Exception as exc:
            raise SalesforceAPIError(
                "Failed to retrieve Salesforce credentials from Secret Manager"
            ) from exc

        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        }

        try:
            response = self._session.post(SF_TOKEN_URL, data=data, timeout=15)
            response.raise_for_status()
        except RequestException as exc:
            raise SalesforceAPIError(
                "Salesforce OAuth token request failed"
            ) from exc

        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._auth_headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        logger.info(
            "Salesforce authentication successful",
            extra={"instance_url": SF_INSTANCE_URL, "api_version": SF_API_VERSION},
        )

    def _refresh_token(self) -> None:
        """Clear cached token and re-authenticate."""
        self._access_token = None
        self._auth_headers = {}
        self._authenticate()

    # ------------------------------------------------------------------
    # API Operations
    # ------------------------------------------------------------------

    def _soql_query(self, soql: str) -> dict[str, Any]:
        """
        Execute a SOQL query and return the parsed response.

        Handles a single token refresh on 401 before raising.

        Args:
            soql: The SOQL query string.

        Returns:
            Parsed JSON response dict.

        Raises:
            SalesforceAPIError: On HTTP error after token refresh attempt.
        """
        for attempt in range(2):  # Allow one token refresh
            response = self._session.get(
                SF_QUERY_URL,
                headers=self._auth_headers,
                params={"q": soql},
                timeout=15,
            )
            if response.status_code == 401 and attempt == 0:
                logger.warning("Salesforce 401 on SOQL query — refreshing token")
                self._refresh_token()
                continue
            try:
                response.raise_for_status()
            except RequestException as exc:
                raise SalesforceAPIError(
                    f"Salesforce SOQL query failed with status {response.status_code}"
                ) from exc
            return response.json()

        raise SalesforceAPIError("Salesforce SOQL query failed after token refresh")

    def _create_note(self, payload: dict[str, Any]) -> NoteCreationResult:
        """
        POST a new Note record to Salesforce.

        Args:
            payload: Dict with Title, Body, ParentId, IsPrivate.

        Returns:
            NoteCreationResult on success.

        Raises:
            SalesforceAPIError: On HTTP error.
        """
        for attempt in range(2):
            response = self._session.post(
                f"{SF_SOBJECTS_URL}/Note/",
                headers=self._auth_headers,
                data=json.dumps(payload),
                timeout=30,
            )
            if response.status_code == 401 and attempt == 0:
                logger.warning("Salesforce 401 on Note creation — refreshing token")
                self._refresh_token()
                continue
            try:
                response.raise_for_status()
            except RequestException as exc:
                _log_salesforce_error(response, "Note creation")
                raise SalesforceAPIError(
                    f"Salesforce Note creation failed with status {response.status_code}"
                ) from exc

            data = response.json()
            if not data.get("success"):
                raise SalesforceAPIError(
                    f"Salesforce Note creation returned success=false: {data}"
                )

            return NoteCreationResult(
                note_id=data["id"],
                contact_id=payload["ParentId"],
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        raise SalesforceAPIError("Salesforce Note creation failed after token refresh")

    # ------------------------------------------------------------------
    # Session Setup
    # ------------------------------------------------------------------

    @staticmethod
    def _build_session() -> requests.Session:
        """Build a requests Session with connection-level retry and TLS enforcement."""
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=_SESSION_RETRY)
        session.mount("https://", adapter)
        return session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_note_title(file_name: str) -> str:
    """
    Generate a Salesforce Note title from the transcript filename.

    Strips the patient identifier portion so the title contains only the
    date and a generic descriptor. Salesforce Note titles are visible to
    all users with record access.

    Example: "session_2024-01-15_P12345.vtt" → "SOAP Note — 2024-01-15"
    """
    match = re.match(r"session_(\d{4}-\d{2}-\d{2})_", file_name, re.IGNORECASE)
    if match:
        return f"SOAP Note — {match.group(1)}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"SOAP Note — {today}"


def _serialize_note_body(soap_note: SOAPNote) -> str:
    """
    Serialize a SOAPNote into a formatted string for the Salesforce Note body.

    Uses a readable section-header format rather than raw JSON so therapists
    can read the note directly in Salesforce without JSON parsing.
    """
    concerns_text = "\n".join(f"  • {c}" for c in soap_note.presenting_concerns)

    body = (
        f"SOAP NOTE\n"
        f"Generated by AI Documentation Pipeline | Duration: "
        f"{soap_note.session_duration_minutes} min\n"
        f"{'=' * 60}\n\n"
        f"SUBJECTIVE\n{soap_note.subjective}\n\n"
        f"OBJECTIVE\n{soap_note.objective}\n\n"
        f"ASSESSMENT\n{soap_note.assessment}\n\n"
        f"PLAN\n{soap_note.plan}\n\n"
        f"PRESENTING CONCERNS\n{concerns_text}\n\n"
        f"RISK ASSESSMENT\n{soap_note.risk_assessment}\n"
    )

    if len(body) > SF_NOTE_BODY_MAX_CHARS:
        truncation_notice = "\n\n[NOTE TRUNCATED — FULL TEXT IN SOURCE SYSTEM]"
        body = body[: SF_NOTE_BODY_MAX_CHARS - len(truncation_notice)] + truncation_notice
        logger.warning(
            "SOAP note body exceeded Salesforce character limit and was truncated",
            extra={"body_length": len(body), "limit": SF_NOTE_BODY_MAX_CHARS},
        )

    return body


def _escape_soql(value: str) -> str:
    """Escape a string value for safe inclusion in a SOQL WHERE clause."""
    # Escape single quotes (the only special char in SOQL string literals)
    return value.replace("'", "\\'")


def _log_salesforce_error(response: requests.Response, operation: str) -> None:
    """Log a Salesforce error response without echoing PHI from the response body."""
    try:
        errors = response.json()
        # Log error codes and types, not user-controlled messages
        error_codes = [e.get("errorCode", "UNKNOWN") for e in errors]
    except Exception:
        error_codes = ["UNPARSEABLE_RESPONSE"]
    logger.error(
        "Salesforce API error",
        extra={
            "operation": operation,
            "status_code": response.status_code,
            "error_codes": error_codes,
        },
    )
