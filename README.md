# Community Page API

FastAPI microservice for the Community Tab integration. This repository is API-only and does not include the local dummy demo frontend.

## Roles

The API has two active sides:

- `client`: the frontend/community page reads public messages and upstamps useful ones.
- `server`: the data/ingestion side submits reviewed raw messages and inspects one saved message.

There is no active `viewer` role and no active `admin` role.

## Endpoints

```http
GET /
GET /health
POST /api/v1/auth/token
GET /api/v1/client/messages
POST /api/v1/client/messages/{message_ref}/upstamp
POST /api/v1/server/messages
GET /api/v1/server/messages/{message_ref}
```

## Install

```powershell
pip install -r requirements.txt
```

## Seed Data

The synthetic One Health dataset is stored in:

```text
data/one_health_reports.json
```

Seed SQLite:

```powershell
python seed_db.py
```

This creates `community_messages.db` locally. The database file is intentionally ignored by git.

## Run

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Docs:

```text
http://127.0.0.1:8000/docs
```

## Frontend Integration

Create a client token:

```http
POST /api/v1/auth/token
```

```json
{
  "person_id": "client_demo",
  "role": "client"
}
```

Use the returned token:

```http
Authorization: Bearer <token>
```

Fetch messages:

```http
GET /api/v1/client/messages?topic=heat&sort=latest&page=1&limit=10
```

Supported filters:

- `category`: `Human`, `Animal`, or `Environment`
- `topic`: partial interest keyword, such as `heat`, `mosquito`, `respiratory`, or `water`
- `district`: ZIP code or district/location keyword
- `year`: optional year
- `sort`: `latest` or `upstamped`
- `page`: page number
- `limit`: max `10`

The frontend should render:

- `category`
- `topic`
- `district`
- `date`
- `time`
- `issue`
- `guidance`
- `upstamp_count`

The frontend should keep `message_ref` hidden and use it only for:

```http
POST /api/v1/client/messages/{message_ref}/upstamp
```

## Server Ingestion

Create a server token:

```json
{
  "person_id": "server_demo",
  "role": "server"
}
```

Submit one reviewed message:

```http
POST /api/v1/server/messages
```

Inspect one saved message:

```http
GET /api/v1/server/messages/{message_ref}
```

## LLM Token Logging

Normal client reads use SQLite and do not call Claude.

When server ingestion calls Bedrock Claude, token usage is logged to:

```text
llm_usage.log
```

Each line includes:

```text
input_tokens=...
output_tokens=...
total_tokens=...
```

## Tests

```powershell
python -m pytest -q
```
