import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable

from main import (
    DB_PATH,
    ParsedMessage,
    initialize_database,
    normalize_category,
    now_iso,
    slugify,
)


SOURCE_JSON_PATH = Path(__file__).resolve().parent / "data" / "one_health_reports.json"


def load_one_health_reports(path: Path = SOURCE_JSON_PATH) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reports = payload.get("reports", [])
    if not isinstance(reports, list):
        raise ValueError("one_health_reports.json must contain a reports list.")
    return [report for report in reports if isinstance(report, dict)]


def display_date(date_text: str) -> str:
    parsed = datetime.strptime(date_text, "%Y-%m-%d")
    return f"{parsed:%b} {parsed.day}"


def sort_timestamp(date_text: str, time_text: str) -> str:
    parsed = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %I:%M %p")
    return parsed.isoformat(timespec="seconds")


def public_issue(category: str, topic: str, location: str) -> str:
    topic_text = topic.strip().rstrip(".")
    if category == "Environment":
        return f"{topic_text} is being reviewed near {location}."
    if category == "Animal":
        return f"{topic_text} involving animals is being reviewed near {location}."
    return f"{topic_text} is being reviewed in {location}."


def clean_sentence(sentence: str) -> str:
    cleaned = re.sub(r"^(Field team relayed|Community member submitted|Submitted by household contact|Promotor de salud relayed|Tribal community health representative submitted|Reported through participatory surveillance):\s*", "", sentence.strip())
    cleaned = re.sub(r"\bData was forwarded to the local health department for situational awareness\.?", "", cleaned)
    cleaned = re.sub(r"\bCross-sector notification \(animal/human/environment\) was completed\.?", "", cleaned)
    cleaned = re.sub(r"\bThe case was flagged for review at the next One Health coordination call\.?", "", cleaned)
    cleaned = re.sub(r"\bFollow-up was scheduled within 48 hours\.?", "", cleaned)
    cleaned = re.sub(r"\bRisk communication materials were shared with the reporting household\.?", "", cleaned)
    cleaned = re.sub(r"\bSpanish-language follow-up materials were sent to the household\.?", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def public_guidance(report_text: str) -> str:
    sentences = [
        clean_sentence(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", report_text.strip())
        if clean_sentence(sentence)
    ]
    guidance_keywords = (
        "advised",
        "asked",
        "recommended",
        "reminded",
        "use ",
        "avoid",
        "seek care",
        "stay home",
        "testing",
        "masking",
        "wash",
        "hydrated",
        "repellent",
        "bottled",
        "boiled",
        "windows closed",
        "veterinary",
    )
    for sentence in sentences:
        if any(keyword in sentence.lower() for keyword in guidance_keywords):
            return sentence
    return "Follow routine prevention steps and watch for local health updates."


def report_to_parsed_message(report: dict[str, object]) -> tuple[str, ParsedMessage]:
    raw_id = int(report["id"])
    category = normalize_category(str(report.get("category", "Human")))
    topic = str(report.get("potential_topic", "Community Signal")).strip()
    location = str(report.get("location", "")).strip() or "Unknown location"
    date_text = str(report.get("date", "")).strip()
    time_text = str(report.get("time", "")).strip()
    report_text = str(report.get("report", "")).strip()
    issue = public_issue(category, topic, location)
    guidance = public_guidance(report_text)
    caption = f"{issue} {guidance}"
    return (
        f"one_health_report_{raw_id:04d}",
        ParsedMessage(
            category=category,
            topic=topic,
            date=date_text,
            message_date=display_date(date_text),
            message_time=time_text,
            timezone="MST",
            year=int(date_text[:4]),
            district=location,
            issue=issue,
            guidance=guidance,
            caption=caption,
            raw_narrative=report_text,
            raw_signals=str(report.get("symptoms_signals", "")).strip(),
            raw_severity=str(report.get("severity", "")).strip(),
            sort_timestamp=sort_timestamp(date_text, time_text),
        ),
    )


def message_ref_for(source_person_id: str, parsed: ParsedMessage) -> str:
    parsed_at = datetime.fromisoformat(parsed.sort_timestamp)
    return (
        f"msg_{source_person_id}_{slugify(parsed.district)}_"
        f"{slugify(parsed.topic or parsed.category)}_{parsed_at:%Y%m%d_%H%M}"
    )


SAMPLE_RAW_MESSAGES = load_one_health_reports()


def seed_database(
    db_path: Path = DB_PATH,
    rewriter: Callable[[ParsedMessage], ParsedMessage] | None = None,
) -> None:
    initialize_database()
    parsed_messages: list[tuple[str, ParsedMessage]] = []
    for report in SAMPLE_RAW_MESSAGES:
        source_person_id, parsed = report_to_parsed_message(report)
        if rewriter:
            parsed = rewriter(parsed)
        parsed_messages.append((source_person_id, parsed))

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DELETE FROM upstamps")
        connection.execute("DELETE FROM community_messages")
        connection.execute("DELETE FROM sqlite_sequence WHERE name = 'community_messages'")
        created_at = now_iso()
        connection.executemany(
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
            [
                (
                    message_ref_for(source_person_id, parsed),
                    source_person_id,
                    parsed.category,
                    parsed.topic,
                    parsed.date,
                    parsed.message_date,
                    parsed.message_time,
                    parsed.timezone,
                    parsed.year,
                    parsed.district,
                    parsed.issue or "",
                    parsed.guidance or "",
                    parsed.caption,
                    parsed.raw_narrative,
                    parsed.raw_signals,
                    parsed.raw_severity,
                    parsed.sort_timestamp,
                    created_at,
                )
                for source_person_id, parsed in parsed_messages
            ],
        )


if __name__ == "__main__":
    seed_database()
    print(f"Seeded {len(SAMPLE_RAW_MESSAGES)} community messages into {DB_PATH}")

