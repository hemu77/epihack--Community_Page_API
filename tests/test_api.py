import importlib
import os
import sqlite3
import sys
import types

from fastapi.testclient import TestClient


def fake_rewriter(parsed):
    topic = parsed.topic or "incident"
    issue = f"{topic} is being monitored in {parsed.district}."
    guidance = "Use routine prevention steps and watch for local health alert signals."
    return parsed.model_copy(
        update={
            "issue": issue,
            "guidance": guidance,
            "caption": f"{issue} {guidance}",
        }
    )


def load_app(tmp_path):
    os.environ["COMMUNITY_DB_PATH"] = str(tmp_path / "test_community_tab.db")
    sys.modules.pop("main", None)
    sys.modules.pop("seed_db", None)
    main = importlib.import_module("main")
    seed_db = importlib.import_module("seed_db")
    main.initialize_database()
    seed_db.seed_database(main.DB_PATH, rewriter=fake_rewriter)
    return main, TestClient(main.app)


def token(client, person_id="person_100", role="client"):
    response = client.post(
        "/api/v1/auth/token",
        json={"person_id": person_id, "role": role},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def auth_header(client, person_id="person_100", role="client"):
    return {"Authorization": f"Bearer {token(client, person_id, role)}"}


def test_root_is_api_integration_contract_without_demo_frontend(tmp_path):
    _, client = load_app(tmp_path)

    response = client.get("/")
    body = response.json()

    assert response.status_code == 200
    assert body["service"] == "Community Tab API"
    assert "GET /api/v1/client/messages" in body["client_endpoints"]
    assert "POST /api/v1/client/messages/{message_ref}/upstamp" in body["client_endpoints"]
    assert "POST /api/v1/server/messages" in body["server_endpoints"]
    assert "viewer" not in response.text
    assert "admin" not in response.text
    assert "<html" not in response.text.lower()
    assert "Get messages" not in response.text
    assert "client/interests" not in response.text


def test_root_has_no_demo_form_defaults(tmp_path):
    _, client = load_app(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="category"' not in response.text
    assert 'id="topic"' not in response.text
    assert 'id="district"' not in response.text
    assert 'id="year"' not in response.text
    assert 'value="School Respiratory Outbreak"' not in response.text
    assert 'value="Pima District"' not in response.text


def test_default_demo_query_returns_all_imported_one_health_rows(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, person_id="client_default_demo", role="client")

    response = client.get(
        "/api/v1/client/messages?sort=latest&page=1&limit=10",
        headers=headers,
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == 1000
    assert len(body["items"]) == 10
    assert {item["category"] for item in body["items"]} <= {"Animal", "Environment", "Human"}


def test_topic_filter_matches_interest_words_inside_topic(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, person_id="client_heat_interest", role="client")

    response = client.get(
        "/api/v1/client/messages?topic=heat&sort=latest&page=1&limit=10",
        headers=headers,
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] > 0
    assert all("heat" in item["topic"].lower() for item in body["items"])
    assert {
        "Heat-related Illness Reports",
        "Outdoor Worker Heat Exhaustion",
    } & {item["topic"] for item in body["items"]}


def test_requests_without_bearer_token_are_rejected(tmp_path):
    _, client = load_app(tmp_path)

    response = client.get("/api/v1/client/messages")

    assert response.status_code == 401


def test_auth_accepts_json_and_swagger_form(tmp_path):
    _, client = load_app(tmp_path)

    assert client.post(
        "/api/v1/auth/token",
        json={"person_id": "client_demo", "role": "client"},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/token",
        data={"username": "client_demo", "password": "Client"},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/token",
        data={"client_id": "client_demo", "client_secret": "client"},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/token",
        json={"person_id": "server_demo", "role": "server"},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/token",
        json={"person_id": "viewer_demo", "role": "viewer"},
    ).status_code == 422


def test_seed_has_projection_data_across_topics_and_districts(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, role="server")

    response = client.get("/api/v1/client/messages?year=2025&limit=10", headers=headers)
    all_rows = client.get("/api/v1/client/messages?year=2025&limit=10&page=2", headers=headers)

    assert response.status_code == 200
    assert response.json()["total"] >= 20
    assert all_rows.status_code == 200


def test_database_schema_uses_date_column_not_datetime_column(tmp_path):
    main, _ = load_app(tmp_path)

    with sqlite3.connect(main.DB_PATH) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(community_messages)").fetchall()
        }

    assert "date" in columns
    assert "message_datetime" not in columns
    assert "datetime" not in columns


def test_server_can_submit_healthmind_incident_format(tmp_path):
    main, client = load_app(tmp_path)
    main.rewrite_public_message = fake_rewriter
    headers = auth_header(client, person_id="server_001", role="server")

    created = client.post(
        "/api/v1/server/messages",
        headers=headers,
        json={
            "person_id": "HM-003",
            "year": 2025,
            "raw_message": (
                "HEALTHMIND  |  Case #4  |  HM-003\n\n"
                "================================================================\n\n"
                "Patient : 29yo FEMALE, Nurse\n\n"
                "Location: Pima District\n\n"
                "Reported: 2025-03-16 00:00:00    Illness: 2025-03-14 00:00:00\n\n"
                "----------------------------------------------------------------\n\n"
                "[ LAYER 1 ]  INCIDENT REPORTED\n\n"
                "Symptoms : Cough/Congestion, Nauseas/Vomiting, Sore Throat, Fever, Diarrhea, Muscle or Body Aches and Pains\n\n"
                "Severity : MODERATE\n\n"
                "A 29-year-old female nurse reported becoming ill on March 14, 2025, with symptoms including cough, congestion, nausea, vomiting, sore throat, fever, diarrhea, and body aches. Severity level: MODERATE\n"
                "Seasonal Illness"
            ),
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["category"] == "Human"
    assert body["topic"] == "Seasonal Illness"
    assert body["district"] == "Pima District"
    assert body["date"] == "2025-03-16"
    assert body["time"] == "12:00 AM"
    assert body["issue"] == "Seasonal Illness is being monitored in Pima District."
    assert body["guidance"] == "Use routine prevention steps and watch for local health alert signals."
    assert body["caption"] == f"{body['issue']} {body['guidance']}"
    assert "Cough/Congestion" not in body["caption"]
    assert "MODERATE" not in body["caption"]
    assert "Patient" not in body["display_text"]


def test_llm_rewrite_retries_invalid_json_then_accepts_clean_output(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.parse_raw_message(
        (
            "HEALTHMIND  |  Case #4  |  HM-003\n"
            "Category : Human\n"
            "Topic : Seasonal Illness\n"
            "Location: Pima District\n"
            "Reported: 2025-03-16 00:00:00    Illness: 2025-03-14 00:00:00\n"
            "Symptoms : Cough/Congestion, Fever\n"
            "Severity : MODERATE\n"
            "A 29-year-old female nurse reported fever and cough. Severity level: MODERATE\n"
            "Seasonal Illness"
        ),
        2025,
    )
    calls = iter(
        [
            "not json",
            '{"issue":"Seasonal illness activity is being monitored in Pima District.","guidance":"Stay home when sick, wash hands often, and watch for local health alert signals."}',
        ]
    )

    rewritten = main.rewrite_public_message(parsed, llm_call=lambda _: next(calls))

    assert rewritten.issue == "Seasonal illness activity is being monitored in Pima District."
    assert rewritten.guidance == "Stay home when sick, wash hands often, and watch for local health alert signals."
    assert "MODERATE" not in rewritten.caption
    assert "29-year-old" not in rewritten.caption


def test_rewrite_prompt_sends_only_safe_row_fields(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.ParsedMessage(
        category="Human",
        topic="Community Meal GI Illness",
        date="2025-03-18",
        message_date="Mar 18",
        message_time="5:45 PM",
        timezone="MST",
        year=2025,
        district="85901",
        caption="raw",
        sort_timestamp="2025-03-18T17:45:00",
        raw_narrative=(
            "Notified accounts: ADHS Epi; County EM\n"
            "Internal notes: do not name households\n"
            "Symptoms/Signals : VOMITING/DIARRHEA\n"
            "Severity : MODERATE\n"
            "Report : Vomiting and diarrhea were reported after a community meal."
        ),
        raw_signals="VOMITING/DIARRHEA",
        raw_severity="MODERATE",
    )

    prompt = main.build_rewrite_prompt(parsed)

    assert "Community Meal GI Illness" in prompt
    assert "85901" in prompt
    assert "vomiting, diarrhea" in prompt
    assert "VOMITING/DIARRHEA" not in prompt
    assert "MODERATE" not in prompt
    assert "ADHS" not in prompt
    assert "County EM" not in prompt
    assert "Internal notes" not in prompt


def test_rewrite_prompt_generalizes_sensitive_animal_condition_terms(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.ParsedMessage(
        category="Animal",
        topic="Dog Head Shaking/Disorientation - New World Screwworm Concern",
        date="2025-03-17",
        message_date="Mar 17",
        message_time="10:30 AM",
        timezone="MST",
        year=2025,
        district="85701",
        caption="raw",
        sort_timestamp="2025-03-17T10:30:00",
        raw_narrative=(
            "Report : Multiple dogs were reported with head shaking and suspected ear involvement. "
            "New World screwworm was listed as a concern requiring specimen handling. "
            "Cochliomyia hominivorax was under consideration."
        ),
        raw_signals="NEURO/EAR INVOLVEMENT",
        raw_severity="HIGH",
    )

    prompt = main.build_rewrite_prompt(parsed)

    assert "screwworm" not in prompt.lower()
    assert "cochliomyia" not in prompt.lower()
    assert "specimen" not in prompt.lower()
    assert "movement" not in prompt.lower()
    assert "animal health condition" in prompt.lower()


def test_bedrock_rewrite_uses_converse_api_without_real_aws(tmp_path, monkeypatch):
    main, _ = load_app(tmp_path)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-token")

    class FakeBedrock:
        def __init__(self):
            self.calls = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "usage": {"inputTokens": 111, "outputTokens": 22, "totalTokens": 133},
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": (
                                    '{"issue":"Meal-related stomach illness reports are being reviewed.",'
                                    '"guidance":"Drink fluids and follow local health updates."}'
                                )
                            }
                        ]
                    }
                }
            }

    fake = FakeBedrock()
    logs = []
    monkeypatch.setattr(main.llm_usage_logger, "info", lambda message, *args: logs.append(message % args))

    response = main.call_bedrock_rewrite("Rewrite this row.", client=fake)

    assert "Meal-related stomach illness" in response
    assert fake.calls[0]["modelId"] == main.BEDROCK_MODEL_ID
    assert fake.calls[0]["messages"][0]["role"] == "user"
    assert fake.calls[0]["inferenceConfig"]["maxTokens"] == main.LLM_MAX_OUTPUT_TOKENS
    assert "temperature" not in fake.calls[0]["inferenceConfig"]
    assert "use only facts present" in fake.calls[0]["system"][0]["text"].lower()
    assert any("input_tokens=111" in line and "output_tokens=22" in line for line in logs)


def test_bedrock_rewrite_accepts_standard_aws_credentials(tmp_path, monkeypatch):
    main, _ = load_app(tmp_path)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")

    class FakeBedrock:
        def converse(self, **_):
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": (
                                    '{"issue":"A community health signal is being reviewed.",'
                                    '"guidance":"Follow routine prevention steps and local health updates."}'
                                )
                            }
                        ]
                    }
                }
            }

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: FakeBedrock()),
    )

    response = main.call_bedrock_rewrite("Rewrite this row.")

    assert "community health signal" in response


def test_llm_rewrite_allows_two_sentence_guidance_for_three_sentence_public_text(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.ParsedMessage(
        category="Human",
        topic="Community Meal GI Illness",
        date="2025-03-18",
        message_date="Mar 18",
        message_time="5:45 PM",
        timezone="MST",
        year=2025,
        district="85901",
        caption="raw",
        sort_timestamp="2025-03-18T17:45:00",
        raw_narrative="Report : Vomiting and diarrhea were reported after a community meal.",
        raw_signals="VOMITING/DIARRHEA",
        raw_severity="MODERATE",
    )

    rewritten = main.rewrite_public_message(
        parsed,
        llm_call=lambda _: (
            '{"issue":"Stomach illness reports after a community meal are being reviewed.",'
            '"guidance":"Drink fluids and wash hands often. Watch for local health updates if more reports appear."}'
        ),
    )

    assert main.sentence_count(rewritten.caption) == 3
    assert "MODERATE" not in rewritten.caption
    assert "VOMITING/DIARRHEA" not in rewritten.caption


def test_llm_rewrite_rejects_hallucinated_location(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.ParsedMessage(
        category="Human",
        topic="Community Meal GI Illness",
        date="2025-03-18",
        message_date="Mar 18",
        message_time="5:45 PM",
        timezone="MST",
        year=2025,
        district="85901",
        caption="raw",
        sort_timestamp="2025-03-18T17:45:00",
        raw_narrative="Report : Vomiting and diarrhea were reported after a community meal.",
        raw_signals="VOMITING/DIARRHEA",
        raw_severity="MODERATE",
    )

    try:
        main.rewrite_public_message(
            parsed,
            llm_call=lambda _: (
                '{"issue":"A confirmed outbreak in Phoenix is being reviewed.",'
                '"guidance":"Drink fluids and watch for local updates."}'
            ),
        )
    except Exception as exc:
        assert getattr(exc, "status_code") == 502
        assert "LLM rewrite failed" in getattr(exc, "detail")
    else:
        raise AssertionError("Expected hallucinated location to be rejected.")


def test_llm_rewrite_rejects_repeated_invalid_output(tmp_path):
    main, _ = load_app(tmp_path)
    parsed = main.parse_raw_message(
        (
            "HEALTHMIND  |  Case #4  |  HM-003\n"
            "Category : Human\n"
            "Topic : Seasonal Illness\n"
            "Location: Pima District\n"
            "Reported: 2025-03-16 00:00:00    Illness: 2025-03-14 00:00:00\n"
            "Symptoms : Cough/Congestion, Fever\n"
            "Severity : MODERATE\n"
            "A 29-year-old female nurse reported fever and cough. Severity level: MODERATE\n"
            "Seasonal Illness"
        ),
        2025,
    )

    try:
        main.rewrite_public_message(parsed, llm_call=lambda _: '{"issue":"Severity is MODERATE.","guidance":"Panic now."}')
    except Exception as exc:
        assert getattr(exc, "status_code") == 502
        assert "LLM rewrite failed" in getattr(exc, "detail")
    else:
        raise AssertionError("Expected invalid LLM output to be rejected.")


def test_seed_focuses_on_environment_human_animal_categories(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, role="client")

    response = client.get("/api/v1/client/messages?year=2025&limit=10", headers=headers)
    body = response.json()
    categories = set()
    topics = set()
    for page in range(1, 5):
        page_body = client.get(
            f"/api/v1/client/messages?year=2025&limit=10&page={page}",
            headers=headers,
        ).json()
        for item in page_body["items"]:
            categories.add(item["category"])
            topics.add(item["topic"])

    assert response.status_code == 200
    assert body["total"] >= 30
    assert {"Environment", "Human", "Animal"} <= categories
    assert "Water Contamination Notice" in topics
    assert "School Absenteeism Spike" in topics
    assert "Bat Found on Ground - Rabies Risk" in topics


def test_all_sample_rows_can_be_rewritten_with_mocked_bedrock(tmp_path):
    main, _ = load_app(tmp_path)
    seed_db = importlib.import_module("seed_db")

    rewritten = []
    for raw in seed_db.SAMPLE_RAW_MESSAGES:
        _, parsed = seed_db.report_to_parsed_message(raw)
        updated = main.rewrite_public_message(
            parsed,
            llm_call=lambda _: (
                '{"issue":"A community health signal is being reviewed.",'
                '"guidance":"Use routine prevention steps and watch for local health updates."}'
            ),
        )
        rewritten.append(updated)

    assert len(rewritten) == 1000
    combined = " ".join(item.caption for item in rewritten)
    assert "MEDIUM" not in combined
    assert "HIGH" not in combined
    assert "NAUSEA, VOMITING, DIARRHEA" not in combined


def test_frontend_supplied_filters_return_arranged_messages_without_private_fields(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, person_id="client_interest", role="client")

    response = client.get(
        "/api/v1/client/messages"
        "?category=Human&topic=School&district=Second%20Mesa"
        "&year=2025&sort=latest&page=1&limit=10",
        headers=headers,
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == 1
    for item in body["items"]:
        assert item["category"] == "Human"
        assert "School" in item["topic"]
        assert "Second Mesa" in item["district"]
        assert item["year"] == 2025
        assert item["date"].startswith("2025-03-")
        assert "time" in item
        assert "Human" in item["display_text"]
        assert "School" in item["display_text"]
        assert "Second Mesa" in item["display_text"]

    response_text = str(body)
    assert "issue" in body["items"][0]
    assert "guidance" in body["items"][0]
    assert "person_id" not in response_text
    assert "source_person_id" not in response_text
    assert "MODERATE" not in response_text
    assert "HIGH" not in response_text
    assert "LOW" not in response_text
    assert "COUGH/FEVER/ABSENTEEISM" not in response_text
    assert "Case #" not in response_text
    assert "name" not in response_text
    assert "email" not in response_text
    assert "news_id" not in response_text
    assert "url" not in response_text.lower()
    assert "id" not in body["items"][0]


def test_public_response_uses_safe_caption_even_if_database_caption_is_raw(tmp_path):
    main, client = load_app(tmp_path)
    headers = auth_header(client, person_id="client_interest", role="client")
    with sqlite3.connect(main.DB_PATH) as connection:
        message_ref = connection.execute(
            "SELECT message_ref FROM community_messages WHERE category = 'Human' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE community_messages
            SET caption = 'Symptoms/Signals : VOMITING/DIARRHEA\n\nSeverity : HIGH\n\nReport : Raw government risk input.',
                raw_narrative = 'Symptoms/Signals : VOMITING/DIARRHEA\n\nSeverity : HIGH\n\nReport : Raw government risk input.'
            WHERE message_ref = ?
            """,
            (message_ref,),
        )

    response = client.get("/api/v1/client/messages?category=Human&year=2025", headers=headers)
    body = response.json()
    response_text = str(body)

    assert response.status_code == 200
    assert "Symptoms/Signals" not in response_text
    assert "VOMITING/DIARRHEA" not in response_text
    assert "Severity" not in response_text
    assert body["items"][0]["caption"] == f"{body['items'][0]['issue']} {body['items'][0]['guidance']}"
    assert body["items"][0]["caption"] in body["items"][0]["display_text"]


def test_limit_is_capped_at_10(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, role="client")

    response = client.get("/api/v1/client/messages?year=2025&limit=50", headers=headers)

    assert response.status_code == 422


def test_since_timestamp_supports_polling_newer_messages(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, role="client")

    response = client.get(
        "/api/v1/client/messages"
        "?category=Human&topic=School%20Absenteeism%20Spike"
        "&since_timestamp=2025-03-16T00:00:00",
        headers=headers,
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] > 0
    assert all(item["date"] >= "2025-03-16" for item in body["items"])


def test_upstamp_returns_only_count_and_does_not_double_count(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, person_id="person_client", role="client")
    messages = client.get(
        "/api/v1/client/messages?category=Human&topic=School%20Absenteeism%20Spike",
        headers=headers,
    ).json()
    message_ref = messages["items"][0]["message_ref"]

    first = client.post(f"/api/v1/client/messages/{message_ref}/upstamp", headers=headers)
    second = client.post(f"/api/v1/client/messages/{message_ref}/upstamp", headers=headers)

    assert first.status_code == 200
    assert first.json() == {"upstamp_count": 1}
    assert second.status_code == 200
    assert second.json() == {"upstamp_count": 1}


def test_server_can_list_but_cannot_upstamp(tmp_path):
    _, client = load_app(tmp_path)
    server_headers = auth_header(client, person_id="server_1", role="server")
    messages = client.get("/api/v1/client/messages?year=2025", headers=server_headers).json()

    response = client.post(
        f"/api/v1/client/messages/{messages['items'][0]['message_ref']}/upstamp",
        headers=server_headers,
    )

    assert response.status_code == 403


def test_sort_upstamped_returns_highest_count_first_for_selected_interest(tmp_path):
    _, client = load_app(tmp_path)
    first_client = auth_header(client, person_id="person_up_1", role="client")
    second_client = auth_header(client, person_id="person_up_2", role="client")

    messages = client.get(
        "/api/v1/client/messages?category=Human&topic=School%20Absenteeism%20Spike",
        headers=first_client,
    ).json()
    older_ref = messages["items"][1]["message_ref"]
    client.post(f"/api/v1/client/messages/{older_ref}/upstamp", headers=first_client)
    client.post(f"/api/v1/client/messages/{older_ref}/upstamp", headers=second_client)
    sorted_response = client.get(
        "/api/v1/client/messages"
        "?category=Human&topic=School%20Absenteeism%20Spike"
        "&sort=upstamped",
        headers=first_client,
    )

    assert sorted_response.status_code == 200
    assert sorted_response.json()["items"][0]["message_ref"] == older_ref
    assert sorted_response.json()["items"][0]["upstamp_count"] == 2


def test_server_can_submit_raw_reviewed_message_and_lookup_one_message(tmp_path):
    main, client = load_app(tmp_path)
    main.rewrite_public_message = fake_rewriter
    headers = auth_header(client, person_id="server_001", role="server")

    created = client.post(
        "/api/v1/server/messages",
        headers=headers,
        json={
            "person_id": "reviewer_new",
            "year": 2026,
            "raw_message": (
                "Wildlife\n"
                "May 19 Â· 4:10 PM MST â€” Coconino District\n"
                '"Multiple dead deer reported by residents."'
            ),
        },
    )
    message_ref = created.json()["message_ref"]
    lookup = client.get(f"/api/v1/server/messages/{message_ref}", headers=headers)

    assert created.status_code == 201
    assert created.json()["category"] == "Wildlife"
    assert created.json()["topic"] is None
    assert "id" not in created.json()
    assert lookup.status_code == 200
    assert lookup.json()["source_person_id"] == "reviewer_new"
    assert lookup.json()["message_ref"] == message_ref
    assert "name" not in str(lookup.json())
    assert "email" not in str(lookup.json())
    assert "url" not in str(lookup.json()).lower()
    assert "news_id" not in str(lookup.json())


def test_client_cannot_access_server_endpoints(tmp_path):
    _, client = load_app(tmp_path)
    headers = auth_header(client, person_id="client_001", role="client")

    create_response = client.post(
        "/api/v1/server/messages",
        headers=headers,
        json={
            "person_id": "reviewer_new",
            "year": 2026,
            "raw_message": 'Wildlife\nMay 19 Â· 4:10 PM MST â€” Coconino District\n"Caption."',
        },
    )
    lookup_response = client.get("/api/v1/server/messages/anything", headers=headers)

    assert create_response.status_code == 403
    assert lookup_response.status_code == 403


def test_openapi_has_explainable_current_endpoints_only(tmp_path):
    _, client = load_app(tmp_path)

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    property_names = set()
    for model in schema.get("components", {}).get("schemas", {}).values():
        property_names.update(model.get("properties", {}).keys())

    assert "issue" in property_names
    assert "guidance" in property_names
    assert "/api/v1/client/messages" in paths
    assert "/api/v1/client/messages/{message_ref}/upstamp" in paths
    assert "/api/v1/server/messages" in paths
    assert "/api/v1/server/messages/{message_ref}" in paths
    assert "/api/v1/community/messages" not in paths
    assert "/api/v1/admin/messages" not in paths
    assert "/api/v1/internal/messages" not in paths
    assert "/api/v1/client/interests/random" not in paths
    assert "/api/v1/client/feed" not in paths
    assert "/api/v1/messages" not in paths
    assert "/api/v1/internal/messages" not in paths
    assert "email" not in property_names
    assert "url" not in property_names
    assert "news_id" not in property_names

