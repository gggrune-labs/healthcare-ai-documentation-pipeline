"""
transcript_parser.py
--------------------
Reads and cleans WebVTT (.vtt) transcripts downloaded from Google Drive.

Responsibilities:
- Download raw .vtt bytes from Drive using the Drive API v3
- Parse WebVTT format: strip WEBVTT header, cue timestamps, and cue IDs
- Normalize speaker labels (e.g., "Speaker 1:", "Therapist:")
- Remove duplicate consecutive lines and common transcription noise
- Return cleaned plain-text transcript and metadata (duration, speaker turns)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import IO

from googleapiclient.discovery import Resource
from googleapiclient.http import MediaIoBaseDownload

from utils import DriveAPIError, TranscriptParseError, get_logger, retry_with_backoff

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# VTT cue timestamp: HH:MM:SS.mmm --> HH:MM:SS.mmm
_VTT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}.*$"
)
# VTT cue identifier (numeric or string on its own line before the timestamp)
_VTT_CUE_ID_RE = re.compile(r"^\d+$")
# Speaker label patterns: "John Smith:", "Therapist:", "Speaker 1:", "SPEAKER_00:"
_SPEAKER_LABEL_RE = re.compile(
    r"^([A-Z][A-Za-z\s\-_]+\d*|SPEAKER_\d{2})\s*:\s*", re.IGNORECASE
)
# Google Meet auto-caption noise patterns to strip
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\[.*?\]"),           # [inaudible], [laughter], [crosstalk]
    re.compile(r"<.*?>"),             # <v SpeakerName> VTT inline voice tags
    re.compile(r"\s{2,}"),            # collapse multiple spaces
]
# Minimum words in a cue line to be considered meaningful content
_MIN_WORDS_PER_LINE = 2


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class SpeakerTurn:
    """A single speaker turn extracted from the transcript."""

    speaker: str
    text: str
    start_time: str  # HH:MM:SS.mmm
    end_time: str    # HH:MM:SS.mmm


@dataclass
class ParsedTranscript:
    """Cleaned transcript ready for the Gemini prompt."""

    full_text: str                         # Cleaned plain-text transcript
    speaker_turns: list[SpeakerTurn]       # Ordered list of speaker turns
    speakers: list[str]                    # Unique speaker labels found
    duration_seconds: float                # Estimated session duration
    cue_count: int                         # Number of VTT cues parsed
    word_count: int                        # Total word count
    metadata: dict[str, str] = field(default_factory=dict)  # Filename, file_id, etc.


# ---------------------------------------------------------------------------
# Drive Download
# ---------------------------------------------------------------------------


def download_vtt_from_drive(drive_service: Resource, file_id: str) -> bytes:
    """
    Download a .vtt file from Google Drive as raw bytes.

    Uses streaming download via MediaIoBaseDownload to avoid loading large
    transcripts entirely into memory before writing to the buffer.

    Args:
        drive_service: Authenticated Google Drive API v3 service resource.
        file_id: Google Drive file ID of the .vtt transcript.

    Returns:
        Raw bytes of the .vtt file content.

    Raises:
        DriveAPIError: If the download fails after retries.
    """
    import io

    def _do_download() -> bytes:
        buffer = io.BytesIO()
        request = drive_service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    try:
        return retry_with_backoff(
            _do_download,
            max_attempts=3,
            base_delay=1.0,
            logger=logger,
        )
    except Exception as exc:
        raise DriveAPIError(
            f"Failed to download file {file_id} from Drive after retries"
        ) from exc


# ---------------------------------------------------------------------------
# VTT Parsing
# ---------------------------------------------------------------------------


def parse_vtt(raw_bytes: bytes, file_name: str = "") -> ParsedTranscript:
    """
    Parse raw WebVTT bytes into a structured, cleaned transcript.

    WebVTT format:
        WEBVTT

        1
        00:00:01.000 --> 00:00:04.500
        Speaker Name: Hello, how are you today?

        2
        00:00:05.200 --> 00:00:08.100
        Speaker Name: I'm doing well, thanks.

    Args:
        raw_bytes: Raw bytes from the .vtt file download.
        file_name: Original filename, used for metadata and error messages.

    Returns:
        ParsedTranscript with cleaned text and structural metadata.

    Raises:
        TranscriptParseError: If the content cannot be parsed as valid VTT.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")  # Strip BOM if present
    except UnicodeDecodeError as exc:
        raise TranscriptParseError(
            f"Could not decode transcript as UTF-8: {file_name}"
        ) from exc

    if not text.strip().startswith("WEBVTT"):
        raise TranscriptParseError(
            f"File does not appear to be WebVTT format (missing WEBVTT header): {file_name}"
        )

    lines = text.splitlines()
    cues: list[dict] = _extract_cues(lines)

    if not cues:
        raise TranscriptParseError(
            f"No cue blocks found in VTT file: {file_name}"
        )

    speaker_turns = _build_speaker_turns(cues)
    full_text = _build_full_text(speaker_turns)
    duration_seconds = _compute_duration(cues)
    speakers = _extract_unique_speakers(speaker_turns)
    word_count = len(full_text.split())

    logger.info(
        "Parsed VTT transcript",
        extra={
            "file_name": _safe_filename(file_name),
            "cue_count": len(cues),
            "speaker_count": len(speakers),
            "duration_seconds": round(duration_seconds, 1),
            "word_count": word_count,
        },
    )

    return ParsedTranscript(
        full_text=full_text,
        speaker_turns=speaker_turns,
        speakers=speakers,
        duration_seconds=duration_seconds,
        cue_count=len(cues),
        word_count=word_count,
        metadata={"file_name": _safe_filename(file_name)},
    )


def _extract_cues(lines: list[str]) -> list[dict]:
    """
    Extract raw cue blocks from VTT lines.

    Each cue block contains:
    - An optional cue ID (e.g., "1" or a UUID)
    - A timestamp line ("00:00:01.000 --> 00:00:04.500")
    - One or more content lines

    Returns a list of dicts with keys: start, end, content_lines.
    """
    cues: list[dict] = []
    current_cue: dict | None = None

    for line in lines:
        line = line.strip()

        if not line:
            if current_cue and current_cue.get("content_lines"):
                cues.append(current_cue)
            current_cue = None
            continue

        if line == "WEBVTT" or line.startswith("NOTE ") or line.startswith("STYLE"):
            continue

        if _VTT_TIMESTAMP_RE.match(line):
            parts = re.split(r"\s+-->\s+", line, maxsplit=1)
            start_raw = parts[0].strip()
            # end may have optional settings (position, align, etc.)
            end_raw = parts[1].split()[0].strip()
            current_cue = {"start": start_raw, "end": end_raw, "content_lines": []}
            continue

        if _VTT_CUE_ID_RE.match(line) and current_cue is None:
            continue  # Numeric cue ID before a timestamp — skip

        if current_cue is not None:
            cleaned = _clean_cue_line(line)
            if cleaned:
                current_cue["content_lines"].append(cleaned)

    # Flush any trailing cue not followed by a blank line
    if current_cue and current_cue.get("content_lines"):
        cues.append(current_cue)

    return cues


def _clean_cue_line(line: str) -> str:
    """Apply noise removal patterns to a single cue content line."""
    for pattern in _NOISE_PATTERNS:
        line = pattern.sub(" ", line)
    return line.strip()


def _build_speaker_turns(cues: list[dict]) -> list[SpeakerTurn]:
    """
    Convert extracted cues into SpeakerTurn objects.

    Merges consecutive cues from the same speaker into a single turn
    to reduce fragmentation common in auto-captioned transcripts.
    """
    turns: list[SpeakerTurn] = []

    for cue in cues:
        full_content = " ".join(cue["content_lines"])
        speaker, text = _split_speaker_and_text(full_content)

        if len(text.split()) < _MIN_WORDS_PER_LINE:
            continue  # Skip near-empty cues (breathing, single words)

        # Merge with previous turn if same speaker and cues are contiguous
        if turns and turns[-1].speaker == speaker:
            merged = turns[-1]
            turns[-1] = SpeakerTurn(
                speaker=merged.speaker,
                text=merged.text.rstrip() + " " + text,
                start_time=merged.start_time,
                end_time=cue["end"],
            )
        else:
            turns.append(
                SpeakerTurn(
                    speaker=speaker,
                    text=text,
                    start_time=cue["start"],
                    end_time=cue["end"],
                )
            )

    return turns


def _split_speaker_and_text(content: str) -> tuple[str, str]:
    """
    Separate a speaker label from the utterance text.

    Returns:
        Tuple of (speaker_label, text). If no label is found, speaker is
        "Unknown Speaker".
    """
    match = _SPEAKER_LABEL_RE.match(content)
    if match:
        speaker = _normalize_speaker_label(match.group(1))
        text = content[match.end():].strip()
        return speaker, text
    return "Unknown Speaker", content.strip()


def _normalize_speaker_label(label: str) -> str:
    """Normalize speaker labels to title case and remove trailing punctuation."""
    label = label.strip().rstrip(":").strip()
    # SPEAKER_00 → Speaker 00 (Google Meet auto-speaker format)
    if re.match(r"^SPEAKER_\d+$", label, re.IGNORECASE):
        parts = label.split("_")
        return f"Speaker {parts[1]}"
    return label.title()


def _build_full_text(speaker_turns: list[SpeakerTurn]) -> str:
    """
    Concatenate speaker turns into a clean, readable plain-text transcript.

    Format:
        Speaker Name: Their utterance text here.

        Other Speaker: Their response.
    """
    lines = [f"{turn.speaker}: {turn.text}" for turn in speaker_turns]
    return "\n\n".join(lines)


def _compute_duration(cues: list[dict]) -> float:
    """
    Estimate session duration in seconds from first and last cue timestamps.
    """
    if not cues:
        return 0.0
    try:
        start = _vtt_time_to_seconds(cues[0]["start"])
        end = _vtt_time_to_seconds(cues[-1]["end"])
        return max(0.0, end - start)
    except (ValueError, KeyError):
        return 0.0


def _vtt_time_to_seconds(timestamp: str) -> float:
    """Convert HH:MM:SS.mmm to total seconds as a float."""
    parts = timestamp.replace(",", ".").split(":")
    hours = float(parts[0])
    minutes = float(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def _extract_unique_speakers(speaker_turns: list[SpeakerTurn]) -> list[str]:
    """Return ordered unique speaker labels preserving first appearance order."""
    seen: set[str] = set()
    unique: list[str] = []
    for turn in speaker_turns:
        if turn.speaker not in seen:
            seen.add(turn.speaker)
            unique.append(turn.speaker)
    return unique


def _safe_filename(file_name: str) -> str:
    """
    Return a log-safe version of a filename.

    Strips patient identifier components from filenames that follow the
    naming convention: session_YYYY-MM-DD_P{patient_id}.vtt
    Returns only the date and a masked patient token.
    """
    # Convention: session_2024-01-15_P12345.vtt
    match = re.match(r"(session_\d{4}-\d{2}-\d{2})_(.+)\.(vtt)", file_name, re.IGNORECASE)
    if match:
        return f"{match.group(1)}_[PATIENT-ID-REDACTED].{match.group(3)}"
    return "[FILENAME-REDACTED]"
