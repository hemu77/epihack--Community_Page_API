import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import parse_qs

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field, ValidationError


DB_PATH = Path(os.getenv("COMMUNITY_DB_PATH", "community_messages.db"))
JWT_SECRET = os.getenv("JWT_SECRET", "dev-community-secret-change-me-32bytes")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 8
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-7")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "700"))
LLM_USAGE_LOG_PATH = Path(os.getenv("LLM_USAGE_LOG_PATH", "llm_usage.log"))

Role = Literal["server", "client"]
SortMode = Literal["latest", "upstamped"]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

llm_usage_logger = logging.getLogger("community_tab.llm_usage")
if not llm_usage_logger.handlers:
    llm_usage_logger.setLevel(logging.INFO)
    usage_handler = logging.FileHandler(LLM_USAGE_LOG_PATH, encoding="utf-8")
    usage_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    llm_usage_logger.addHandler(usage_handler)
    llm_usage_logger.propagate = False


class TokenRequest(BaseModel):
    person_id: str = Field(min_length=1)
    role: Role


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RawMessageCreate(BaseModel):
    person_id: str = Field(min_length=1)
    year: int = Field(ge=1900, le=3000)
    raw_message: str = Field(min_length=1)


class CommunityMessage(BaseModel):
    message_ref: str
    category: str
    topic: str | None
    year: int
    district: str
    date: str
    time: str
    display_time: str
    issue: str
    guidance: str
    caption: str
    display_text: str
    upstamp_count: int


class CommunityMessageList(BaseModel):
    page: int
    limit: int
    total: int
    sort: SortMode
    filters: dict[str, str | int | None]
    items: list[CommunityMessage]


class ServerMessage(CommunityMessage):
    source_person_id: str
    message_date: str
    message_time: str
    timezone: str
    created_at: str


class UpstampResponse(BaseModel):
    upstamp_count: int


class AuthContext(BaseModel):
    person_id: str
    role: Role


class ParsedMessage(BaseModel):
    category: str
    topic: str | None
    date: str
    message_date: str
    message_time: str
    timezone: str
    year: int
    district: str
    caption: str
    sort_timestamp: str
    issue: str | None = None
    guidance: str | None = None
    raw_narrative: str | None = None
    raw_signals: str | None = None
    raw_severity: str | None = None



def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def initialize_database() -> None:
    with closing(get_connection()) as connection, connection:
        existing_columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(community_messages)").fetchall()
        ]
        if existing_columns and (
            "message_ref" not in existing_columns
            or "date" not in existing_columns
            or "issue" not in existing_columns
            or "guidance" not in existing_columns
            or "message_datetime" in existing_columns
        ):
            connection.execute("DROP TABLE community_messages")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS community_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_ref TEXT NOT NULL UNIQUE,
                source_person_id TEXT NOT NULL,
                category TEXT NOT NULL,
                topic TEXT,
                date TEXT NOT NULL,
                message_date TEXT NOT NULL,
                message_time TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'MST',
                year INTEGER NOT NULL,
                district TEXT NOT NULL,
                issue TEXT NOT NULL,
                guidance TEXT NOT NULL,
                caption TEXT NOT NULL,
                raw_narrative TEXT,
                raw_signals TEXT,
                raw_severity TEXT,
                sort_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        legacy_columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(scenario_messages)").fetchall()
        ]
        if legacy_columns:
            connection.execute("DROP TABLE scenario_messages")
        upstamp_columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(upstamps)").fetchall()
        ]
        if upstamp_columns and "message_ref" not in upstamp_columns:
            connection.execute("DROP TABLE upstamps")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS upstamps (
                message_ref TEXT NOT NULL,
                person_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (message_ref, person_id),
                FOREIGN KEY (message_ref) REFERENCES community_messages(message_ref)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_filters ON community_messages(category, topic, district, year)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_sort_timestamp ON community_messages(sort_timestamp DESC)"
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Community Tab API",
    version="2.0.0",
    description="Authenticated API for arranged community tab messages.",
    lifespan=lifespan,
)


def create_access_token(person_id: str, role: Role) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    payload = {"sub": person_id, "role": role, "exp": expires_at}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def build_token_request(person_id: str, role: str) -> TokenRequest:
    try:
        return TokenRequest(person_id=person_id.strip(), role=role.strip().lower())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="Use person_id and one role: client or server.",
        ) from exc


def get_auth_context(token: str = Depends(oauth2_scheme)) -> AuthContext:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    person_id = payload.get("sub")
    role = payload.get("role")
    if not person_id or role not in {"server", "client"}:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return AuthContext(person_id=person_id, role=role)


def require_role(*allowed_roles: Role):
    def dependency(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if auth.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role for this endpoint",
            )
        return auth

    return dependency


def normalize_text(value: str) -> str:
    return (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("Ã‚Â·", "·")
        .replace("Â·", "·")
        .replace("Ã¢â‚¬â€", "—")
        .replace("â€”", "—")
        .strip()
    )


def normalize_category(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"environment", "environmental"}:
        return "Environment"
    if lowered in {"human", "people", "person"}:
        return "Human"
    if lowered in {"animal", "animals", "wildlife", "livestock"}:
        return "Animal"
    if lowered in {"exposure", "general", "auxiliary"}:
        return "Human"
    return value.strip().title()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "message"


def parse_message_timestamp_parts(date_text: str, year: int, time_text: str) -> datetime:
    value = f"{date_text} {year} {time_text}"
    for pattern in ("%B %d %Y %I:%M %p", "%b %d %Y %I:%M %p"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise HTTPException(
        status_code=422,
        detail="Message date/time must look like: May 19 Â· 4:10 PM MST â€” Coconino District.",
    )


def parse_healthmind_message(raw_message: str, year: int) -> ParsedMessage:
    text = normalize_text(raw_message)
    compact_lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not compact_lines or "HEALTHMIND" not in compact_lines[0].upper():
        raise HTTPException(status_code=422, detail="Not a HEALTHMIND incident message.")

    def field(name: str) -> str | None:
        match = re.search(rf"^{name}\s*:\s*(.+)$", text, flags=re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else None

    category = normalize_category(field("Category") or "")
    explicit_topic = field("Topic")
    location = field("Location")
    symptoms = field("Symptoms")
    severity = field("Severity")
    reported_match = re.search(
        r"Reported\s*:\s*(?P<reported>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if not location or not reported_match:
        raise HTTPException(
            status_code=422,
            detail="HEALTHMIND incident must include Location and Reported timestamp.",
        )

    if not category:
        category = "Human" if field("Patient") or symptoms else "Environment"

    topic = explicit_topic
    if not topic:
        for line in reversed(compact_lines):
            if (
                ":" not in line
                and not line.startswith("[")
                and set(line) != {"-"}
                and set(line) != {"="}
                and len(line) <= 80
            ):
                topic = line.strip()
                break
    topic = topic or "Incident Report"

    reported_at = datetime.strptime(reported_match.group("reported"), "%Y-%m-%d %H:%M:%S")
    narrative_candidates = []
    for line in compact_lines:
        if (
            line.startswith("[")
            or re.match(r"^[A-Za-z ]+\s*:", line)
            or set(line) in ({"-"}, {"="})
            or line == topic
            or "HEALTHMIND" in line.upper()
        ):
            continue
        if len(line.split()) >= 6:
            narrative_candidates.append(line)
    narrative = " ".join(narrative_candidates).strip()

    caption_parts = []
    if narrative:
        caption_parts.append(narrative)
    if symptoms:
        caption_parts.append(f"Symptoms/signals: {symptoms}.")
    if severity:
        caption_parts.append(f"Severity: {severity}.")
    caption = " ".join(caption_parts).strip()
    if not caption:
        caption = f"{topic} incident reported in {location}. Severity: {severity or 'UNDER REVIEW'}."

    return ParsedMessage(
        category=category,
        topic=topic,
        date=reported_at.date().isoformat(),
        message_date=reported_at.strftime("%b %d"),
        message_time=reported_at.strftime("%I:%M %p").lstrip("0"),
        timezone="MST",
        year=year,
        district=location,
        caption=caption,
        sort_timestamp=reported_at.replace(second=0).isoformat(),
        raw_narrative=narrative or None,
        raw_signals=symptoms,
        raw_severity=severity,
    )


def parse_raw_message(raw_message: str, year: int) -> ParsedMessage:
    if "HEALTHMIND" in raw_message[:120].upper():
        return parse_healthmind_message(raw_message, year)

    lines = [line.strip() for line in normalize_text(raw_message).split("\n") if line.strip()]
    if len(lines) < 3:
        raise HTTPException(
            status_code=422,
            detail="Raw message must have category line, date/district line, and quoted caption line.",
        )

    header = lines[0]
    if "·" in header:
        category, topic = [part.strip() for part in header.split("·", 1)]
    else:
        category, topic = header.strip(), None

    match = re.match(
        r"^(?P<date>[A-Za-z]+ \d{1,2})\s*·\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+(?P<timezone>[A-Z]{2,4})\s*—\s*(?P<district>.+)$",
        lines[1],
    )
    if not match:
        raise HTTPException(
            status_code=422,
            detail="Second raw message line must look like: May 19 · 4:10 PM MST — Coconino District.",
        )

    caption = " ".join(lines[2:]).strip()
    if caption.startswith('"') and caption.endswith('"'):
        caption = caption[1:-1].strip()

    parsed_at = parse_message_timestamp_parts(match.group("date"), year, match.group("time"))
    return ParsedMessage(
        category=category,
        topic=topic or None,
        date=parsed_at.date().isoformat(),
        message_date=match.group("date").strip(),
        message_time=match.group("time").strip(),
        timezone=match.group("timezone").strip(),
        year=year,
        district=match.group("district").strip(),
        caption=caption,
        sort_timestamp=parsed_at.replace(second=0).isoformat(),
        raw_narrative=caption,
    )


FORBIDDEN_PUBLIC_PATTERNS = [
    r"\b(MODERATE|HIGH|LOW)\b",
    r"(?i)\bseverity\b",
    r"(?i)\bseverity level\b",
    r"(?i)\bcase\s*#",
    r"(?i)\bHM-\d+\b",
    r"(?i)\b\d{1,3}\s*yo\b",
    r"(?i)\b\d{1,3}\s*-\s*year\s*-\s*old\b",
    r"(?i)\bmale\b",
    r"(?i)\bfemale\b",
    r"(?i)\bnurse\b",
    r"(?i)\bteacher\b",
    r"(?i)\bdoctor\b",
    r"(?i)\boccupation\b",
    r"(?i)\bADHS\b",
    r"(?i)\bAGFD\b",
    r"(?i)\bUSDA\b",
    r"(?i)\bAPHIS\b",
    r"(?i)\bCounty EM\b",
    r"(?i)\bHazMat\b",
    r"(?i)\bIHS\b",
    r"(?i)\bAPI key\b",
    r"(?i)\bpanic\b",
    r"(?i)\bdeadly\b",
    r"(?i)\bdangerous outbreak\b",
    r"(?i)\bemergency\b",
    r"(?i)\bconfirmed outbreak\b",
    r"COUGH/FEVER/ABSENTEEISM",
    r"COUGH/CONGESTION",
    r"VOMITING/DIARRHEA",
]

KNOWN_LOCATION_TERMS = [
    "Apache",
    "Chinle",
    "Coconino",
    "Flagstaff",
    "Gila River",
    "Maricopa",
    "Oro Valley",
    "Phoenix",
    "Pima",
    "Pinal",
    "San Xavier",
    "Sells",
    "Show Low",
    "Tucson",
    "Yuma",
]

AGENCY_SOURCE_PATTERNS = [
    r"(?i)^notified accounts\s*:.*$",
    r"(?i)^internal notes\s*:.*$",
    r"(?i)^agency review\s*:.*$",
    r"(?i)^operational status\s*:.*$",
    r"(?i)^public release status\s*:.*$",
    r"(?i)^source_person_id\s*:.*$",
    r"(?i)^alert id\s*:.*$",
    r"(?i)^symptoms/signals\s*:.*$",
    r"(?i)^symptoms\s*:.*$",
]

PRIVATE_SOURCE_PATTERNS = [
    r"(?i)\bcase\s*#\s*\d+",
    r"(?i)\bHM-\d+\b",
    r"(?i)\b\d{1,3}\s*yo\b",
    r"(?i)\b\d{1,3}\s*-\s*year\s*-\s*old\b",
    r"(?i)\bmale\b",
    r"(?i)\bfemale\b",
    r"(?i)\bnurse\b",
    r"(?i)\bteacher\b",
    r"(?i)\bdoctor\b",
    r"(?i)^patient\s*:.*$",
    r"(?i)^severity\s*:.*$",
    r"(?i)\bseverity level\s*:\s*(moderate|high|low)\b",
    r"\b(MODERATE|HIGH|LOW)\b",
    r"(?i)\bADHS\b",
    r"(?i)\bAGFD\b",
    r"(?i)\bUSDA\b",
    r"(?i)\bAPHIS\b",
    r"(?i)\bCounty EM\b",
    r"(?i)\bHazMat\b",
    r"(?i)\bIHS\b",
]

SOURCE_REPLACEMENTS = [
    (r"(?i)new world screwworm", "animal health condition"),
    (r"(?i)screwworm", "animal health condition"),
    (r"(?i)Cochliomyia hominivorax", "animal health condition"),
]

OPERATIONAL_SOURCE_TERMS = [
    "agency",
    "chain-of-custody",
    "containment",
    "federal",
    "movement",
    "notification",
    "notified",
    "operational",
    "specimen",
    "state ",
]

BEDROCK_SYSTEM_PROMPT = """You rewrite government/public-health risk records for a community tab.

Rules:
- Use only facts present in this row.
- Do not infer causes, diagnoses, counts, locations, dates, certainty, or agency actions.
- Do not add medical claims that are not present in the source row.
- If prevention guidance is not directly available, use only broad routine prevention wording.
- Do not include severity labels, raw signal codes, internal IDs, agency names, private notes, or operational details.
- Keep a calm, lightly alerting tone; do not use panic wording.
- Return strict JSON only with keys "issue" and "guidance".
- "issue" must be exactly one calm sentence.
- "guidance" must be one or two calm sentences with prevention and a health-alert signal summary.
- The combined public text must be two or three sentences total.
"""


def sentence_count(value: str) -> int:
    return len([part for part in re.split(r"[.!?]+", value.strip()) if part.strip()])


def clean_signal_text(value: str | None) -> str:
    if not value:
        return "not provided"
    cleaned = value.replace("/", ", ").replace("_", " ").replace(";", ",")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned or "not provided"


def generalize_sensitive_source_terms(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value
    for pattern, replacement in SOURCE_REPLACEMENTS:
        cleaned = re.sub(pattern, replacement, cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_priority_context(value: str | None) -> str:
    severity = (value or "").strip().upper()
    if severity == "HIGH":
        return "source marked this for time-sensitive review"
    if severity == "MODERATE":
        return "source marked this for standard review"
    if severity == "LOW":
        return "source marked this for routine review"
    return "source did not provide a review priority"


def sanitize_source_text(value: str | None) -> str:
    if not value:
        return "No report paragraph provided."
    kept_lines: list[str] = []
    for line in normalize_text(value).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(re.search(pattern, stripped) for pattern in AGENCY_SOURCE_PATTERNS):
            continue
        cleaned = stripped
        for pattern in PRIVATE_SOURCE_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
        if cleaned:
            kept_lines.append(cleaned)
    sanitized = " ".join(kept_lines)
    sanitized = generalize_sensitive_source_terms(sanitized)
    sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", sanitized):
        if not sentence.strip():
            continue
        lower_sentence = sentence.lower()
        if any(term in lower_sentence for term in OPERATIONAL_SOURCE_TERMS):
            continue
        sentences.append(sentence.strip())
    sanitized = " ".join(sentences)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "No report paragraph provided."


def build_rewrite_source(message: ParsedMessage) -> dict[str, str | int | None]:
    return {
        "category": message.category,
        "topic": generalize_sensitive_source_terms(message.topic or "General"),
        "location": message.district,
        "date": message.date,
        "time": f"{message.message_time} {message.timezone}",
        "signals": clean_signal_text(message.raw_signals),
        "private_priority_context": clean_priority_context(message.raw_severity),
        "report": sanitize_source_text(message.raw_narrative or message.caption),
    }


def validate_public_rewrite(issue: str, guidance: str, source: ParsedMessage | None = None) -> None:
    if not issue or not guidance:
        raise ValueError("LLM response must include issue and guidance.")
    if sentence_count(issue) != 1:
        raise ValueError("Issue must be exactly one sentence.")
    guidance_sentences = sentence_count(guidance)
    if guidance_sentences < 1 or guidance_sentences > 2:
        raise ValueError("Guidance must be one or two sentences.")
    combined = f"{issue} {guidance}"
    total_sentences = sentence_count(combined)
    if total_sentences < 2 or total_sentences > 3:
        raise ValueError("Public rewrite must be two or three sentences total.")
    for pattern in FORBIDDEN_PUBLIC_PATTERNS:
        if re.search(pattern, combined):
            raise ValueError(f"Forbidden public term matched: {pattern}")
    if source:
        lower_combined = combined.lower()
        allowed_location = source.district.lower()
        allowed_topic = (source.topic or "").lower()
        for term in KNOWN_LOCATION_TERMS:
            lower_term = term.lower()
            if (
                lower_term in lower_combined
                and lower_term not in allowed_location
                and lower_term not in allowed_topic
            ):
                raise ValueError(f"Possible invented location matched: {term}")
        years = set(re.findall(r"\b(20\d{2}|19\d{2})\b", combined))
        if years and str(source.year) not in years:
            raise ValueError("Possible invented year matched.")
        if re.search(r"(?i)\bconfirmed\b", combined) and not re.search(
            r"(?i)\bconfirmed\b", source.raw_narrative or source.caption
        ):
            raise ValueError("Possible invented certainty matched.")


def build_rewrite_prompt(message: ParsedMessage, *, repair: bool = False) -> str:
    repair_text = (
        "Your previous answer failed validation. Repair it using only the source row and return JSON only.\n"
        if repair
        else ""
    )
    source_record = json.dumps(build_rewrite_source(message), ensure_ascii=False)
    return (
        f"{repair_text}"
        "Rewrite this source row for the public community tab.\n"
        "Use only facts present in this row. Do not infer causes, diagnoses, counts, locations, dates, certainty, or agency actions.\n"
        "Return strict JSON only with keys issue and guidance.\n"
        'Example shape: {"issue":"Respiratory illness activity is being reviewed in the community.","guidance":"Stay home when sick and follow local health updates."}\n\n'
        f"Source row JSON:\n{source_record}\n"
    )


def extract_json_object(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object.")
    return {"issue": str(parsed.get("issue", "")).strip(), "guidance": str(parsed.get("guidance", "")).strip()}


def call_bedrock_rewrite(prompt: str, client: Any | None = None) -> str:
    has_bearer_token = bool(os.getenv("AWS_BEARER_TOKEN_BEDROCK"))
    has_access_keys = bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))
    if client is None and not (has_bearer_token or has_access_keys):
        raise HTTPException(
            status_code=502,
            detail=(
                "LLM rewrite failed: AWS_BEARER_TOKEN_BEDROCK or "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY is required."
            ),
        )
    if client is None:
        try:
            import boto3
        except ImportError as exc:
            raise HTTPException(status_code=502, detail="LLM rewrite failed: install boto3.") from exc
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    inference_config: dict[str, int | float] = {"maxTokens": LLM_MAX_OUTPUT_TOKENS}
    if "claude-opus-4-7" not in BEDROCK_MODEL_ID:
        inference_config["temperature"] = LLM_TEMPERATURE

    try:
        response = client.converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": BEDROCK_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=inference_config,
        )
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        total_tokens = usage.get("totalTokens", input_tokens + output_tokens)
        llm_usage_logger.info(
            "bedrock_model=%s input_tokens=%s output_tokens=%s total_tokens=%s",
            BEDROCK_MODEL_ID,
            input_tokens,
            output_tokens,
            total_tokens,
        )
        return response["output"]["message"]["content"][0]["text"]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM rewrite failed: Bedrock call failed: {exc}") from exc


def rewrite_public_message(
    message: ParsedMessage,
    llm_call: Callable[[str], str] = call_bedrock_rewrite,
) -> ParsedMessage:
    last_error: Exception | None = None
    for repair in (False, True):
        try:
            raw_response = llm_call(build_rewrite_prompt(message, repair=repair))
            public = extract_json_object(raw_response)
            validate_public_rewrite(public["issue"], public["guidance"], source=message)
            caption = f"{public['issue']} {public['guidance']}"
            return message.model_copy(
                update={
                    "issue": public["issue"],
                    "guidance": public["guidance"],
                    "caption": caption,
                }
            )
        except Exception as exc:
            last_error = exc
    raise HTTPException(status_code=502, detail=f"LLM rewrite failed: {last_error}")


def generate_message_ref(message: ParsedMessage) -> str:
    parsed_at = datetime.fromisoformat(message.sort_timestamp)
    topic_or_category = message.topic or message.category
    base = (
        f"msg_{slugify(message.district)}_{slugify(topic_or_category)}_"
        f"{parsed_at:%Y%m%d_%H%M}"
    )
    message_ref = base
    suffix = 2
    with closing(get_connection()) as connection:
        while connection.execute(
            "SELECT 1 FROM community_messages WHERE message_ref = ?",
            (message_ref,),
        ).fetchone():
            message_ref = f"{base}_{suffix}"
            suffix += 1
    return message_ref


def display_time(row: sqlite3.Row | ParsedMessage) -> str:
    if isinstance(row, sqlite3.Row):
        return f"{row['message_date']} Â· {row['message_time']} {row['timezone']}"
    return f"{row.message_date} Â· {row.message_time} {row.timezone}"


def message_header(row: sqlite3.Row | ParsedMessage) -> str:
    if isinstance(row, sqlite3.Row):
        category, topic = row["category"], row["topic"]
    else:
        category, topic = row.category, row.topic
    return f"{category} Â· {topic}" if topic else category


def public_caption(row: sqlite3.Row | ParsedMessage) -> str:
    if isinstance(row, sqlite3.Row):
        issue, guidance = row["issue"], row["guidance"]
        stored_caption = row["caption"]
    else:
        issue, guidance = row.issue, row.guidance
        stored_caption = row.caption
    if issue and guidance:
        return f"{issue} {guidance}"
    return stored_caption


def display_text(row: sqlite3.Row | ParsedMessage) -> str:
    district = row["district"] if isinstance(row, sqlite3.Row) else row.district
    caption = public_caption(row)
    return f'{message_header(row)}\n{display_time(row)} â€” {district}\n"{caption}"'


def row_to_public_message(row: sqlite3.Row) -> CommunityMessage:
    return CommunityMessage(
        message_ref=row["message_ref"],
        category=row["category"],
        topic=row["topic"],
        year=row["year"],
        district=row["district"],
        date=row["date"],
        time=row["message_time"],
        display_time=display_time(row),
        issue=row["issue"],
        guidance=row["guidance"],
        caption=public_caption(row),
        display_text=display_text(row),
        upstamp_count=row["upstamp_count"],
    )


def row_to_server_message(row: sqlite3.Row) -> ServerMessage:
    public = row_to_public_message(row).model_dump()
    return ServerMessage(
        **public,
        source_person_id=row["source_person_id"],
        message_date=row["message_date"],
        message_time=row["message_time"],
        timezone=row["timezone"],
        created_at=row["created_at"],
    )


def insert_message(source_person_id: str, message: ParsedMessage) -> CommunityMessage:
    initialize_database()
    if not message.issue or not message.guidance:
        message = rewrite_public_message(message)
    message_ref = generate_message_ref(message)
    with closing(get_connection()) as connection, connection:
        connection.execute(
            """
            INSERT INTO community_messages (
                message_ref,
                source_person_id,
                category,
                topic,
                date,
                message_date,
                message_time,
                timezone,
                year,
                district,
                issue,
                guidance,
                caption,
                raw_narrative,
                raw_signals,
                raw_severity,
                sort_timestamp,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_ref,
                source_person_id,
                message.category,
                message.topic,
                message.date,
                message.message_date,
                message.message_time,
                message.timezone,
                message.year,
                message.district,
                message.issue or "",
                message.guidance or "",
                message.caption,
                message.raw_narrative,
                message.raw_signals,
                message.raw_severity,
                message.sort_timestamp,
                now_iso(),
            ),
        )
    return row_to_public_message(fetch_message_row(message_ref))


def fetch_message_row(message_ref: str) -> sqlite3.Row:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                m.message_ref,
                m.source_person_id,
                m.category,
                m.topic,
                m.date,
                m.message_date,
                m.message_time,
                m.timezone,
                m.year,
                m.district,
                m.issue,
                m.guidance,
                m.caption,
                m.raw_narrative,
                m.raw_signals,
                m.raw_severity,
                m.sort_timestamp,
                m.created_at,
                COUNT(u.person_id) AS upstamp_count
            FROM community_messages AS m
            LEFT JOIN upstamps AS u ON u.message_ref = m.message_ref
            WHERE m.message_ref = ?
            GROUP BY m.message_ref
            """,
            (message_ref,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    return row


def fetch_message_rows(
    *,
    category: str | None,
    topic: str | None,
    district: str | None,
    year: int | None,
    since_date: str | None,
    since_timestamp: str | None,
    sort: SortMode,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str | int] = []
    if category:
        clauses.append("LOWER(m.category) = LOWER(?)")
        params.append(category)
    if topic:
        clauses.append("LOWER(m.topic) LIKE LOWER(?)")
        params.append(f"%{topic}%")
    if district:
        clauses.append("LOWER(m.district) LIKE LOWER(?)")
        params.append(f"%{district}%")
    if year:
        clauses.append("m.year = ?")
        params.append(year)
    if since_date:
        clauses.append("m.date > ?")
        params.append(since_date)
    if since_timestamp:
        clauses.append("m.sort_timestamp > ?")
        params.append(since_timestamp)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_by = (
        "ORDER BY upstamp_count DESC, m.sort_timestamp DESC, m.message_ref DESC"
        if sort == "upstamped"
        else "ORDER BY m.sort_timestamp DESC, m.message_ref DESC"
    )

    with closing(get_connection()) as connection:
        return list(
            connection.execute(
                f"""
                SELECT
                    m.message_ref,
                    m.source_person_id,
                    m.category,
                    m.topic,
                    m.date,
                    m.message_date,
                    m.message_time,
                    m.timezone,
                    m.year,
                    m.district,
                    m.issue,
                    m.guidance,
                    m.caption,
                    m.raw_narrative,
                    m.raw_signals,
                    m.raw_severity,
                    m.sort_timestamp,
                    m.created_at,
                    COUNT(u.person_id) AS upstamp_count
                FROM community_messages AS m
                LEFT JOIN upstamps AS u ON u.message_ref = m.message_ref
                {where}
                GROUP BY m.message_ref
                {order_by}
                """,
                params,
            ).fetchall()
        )


def paginate(rows: list[sqlite3.Row], page: int, limit: int) -> list[sqlite3.Row]:
    start = (page - 1) * limit
    return rows[start : start + limit]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    return {
        "service": "Community Tab API",
        "docs": "/docs",
        "health": "/health",
        "client_endpoints": [
            "GET /api/v1/client/messages",
            "POST /api/v1/client/messages/{message_ref}/upstamp",
        ],
        "server_endpoints": [
            "POST /api/v1/server/messages",
            "GET /api/v1/server/messages/{message_ref}",
        ],
    }


@app.post("/api/v1/auth/token", response_model=TokenResponse)
async def token(request: Request) -> TokenResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc
        token_request = build_token_request(
            person_id=str(payload.get("person_id", "")),
            role=str(payload.get("role", "")),
        )
    else:
        body = (await request.body()).decode()
        form = parse_qs(body)
        person_id = form.get("username", [""])[0] or form.get("client_id", [""])[0]
        role = form.get("password", [""])[0] or form.get("client_secret", [""])[0]
        token_request = build_token_request(person_id=person_id, role=role)
    return TokenResponse(access_token=create_access_token(token_request.person_id, token_request.role))


@app.get("/api/v1/client/messages", response_model=CommunityMessageList)
def list_client_messages(
    category: str | None = Query(default=None, min_length=1),
    topic: str | None = Query(default=None, min_length=1),
    district: str | None = Query(default=None, min_length=1),
    year: int | None = Query(default=None, ge=1900, le=3000),
    sort: SortMode = "latest",
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=10),
    since_date: str | None = Query(default=None, description="Return messages after this YYYY-MM-DD date."),
    since_timestamp: str | None = Query(
        default=None,
        description="Return messages after this ISO timestamp, such as 2025-03-16T00:00:00.",
    ),
    _: AuthContext = Depends(require_role("server", "client")),
) -> CommunityMessageList:
    rows = fetch_message_rows(
        category=category,
        topic=topic,
        district=district,
        year=year,
        since_date=since_date,
        since_timestamp=since_timestamp,
        sort=sort,
    )
    return CommunityMessageList(
        page=page,
        limit=limit,
        total=len(rows),
        sort=sort,
        filters={
            "category": category,
            "topic": topic,
            "district": district,
            "year": year,
            "since_date": since_date,
            "since_timestamp": since_timestamp,
        },
        items=[row_to_public_message(row) for row in paginate(rows, page, limit)],
    )


@app.post("/api/v1/client/messages/{message_ref}/upstamp", response_model=UpstampResponse)
def upstamp_message(
    message_ref: str,
    auth: AuthContext = Depends(require_role("client")),
) -> UpstampResponse:
    fetch_message_row(message_ref)
    with closing(get_connection()) as connection, connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO upstamps (message_ref, person_id, created_at)
            VALUES (?, ?, ?)
            """,
            (message_ref, auth.person_id, now_iso()),
        )
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM upstamps WHERE message_ref = ?",
            (message_ref,),
        ).fetchone()["count"]
    return UpstampResponse(upstamp_count=count)


@app.post("/api/v1/server/messages", response_model=CommunityMessage, status_code=201)
def create_server_message(
    request: RawMessageCreate,
    _: AuthContext = Depends(require_role("server")),
) -> CommunityMessage:
    parsed = parse_raw_message(request.raw_message, request.year)
    return insert_message(request.person_id, parsed)


@app.get("/api/v1/server/messages/{message_ref}", response_model=ServerMessage)
def get_server_message(
    message_ref: str,
    _: AuthContext = Depends(require_role("server")),
) -> ServerMessage:
    return row_to_server_message(fetch_message_row(message_ref))

