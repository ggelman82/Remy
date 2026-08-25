import os
import json
import sqlite3
import hashlib
import hmac
import gspread
import google.auth
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from anthropic import Anthropic

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

REMY_PASSWORD = os.environ.get("REMY_PASSWORD", "")
REMY_DIGEST_TOKEN = os.environ.get("REMY_DIGEST_TOKEN", "")
AUTH_COOKIE_NAME = "remy_auth"
AUTH_COOKIE_VALUE = (
    hashlib.sha256(("remy-session:" + REMY_PASSWORD).encode()).hexdigest()
    if REMY_PASSWORD
    else ""
)

DB_FILE = "remy.db"


def get_sheet():
    if os.path.exists("google-service-account.json"):
        gc = gspread.service_account(filename="google-service-account.json")
    else:
        credentials, _ = google.auth.default(
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
        )
        gc = gspread.authorize(credentials)

    return gc.open("Remy Tasks").sheet1


def setup_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            raw_text TEXT NOT NULL,
            intent TEXT,
            task TEXT,
            details TEXT,
            needs_research INTEGER DEFAULT 0,
            needs_email_draft INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()


setup_database()


class Capture(BaseModel):
    text: str


def has_web_auth(request: Request):
    if not AUTH_COOKIE_VALUE:
        return False
    supplied = request.cookies.get(AUTH_COOKIE_NAME, "")
    return bool(supplied) and hmac.compare_digest(supplied, AUTH_COOKIE_VALUE)


def require_web_auth(request: Request):
    if not has_web_auth(request):
        raise HTTPException(status_code=401, detail="Authentication required")


def has_digest_auth(request: Request):
    if not REMY_DIGEST_TOKEN:
        return False
    supplied = request.headers.get("X-Remy-Token", "")
    return bool(supplied) and hmac.compare_digest(supplied, REMY_DIGEST_TOKEN)


LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Remy Login</title>
    <style>
        body {
            font-family: sans-serif;
            max-width: 420px;
            margin: 70px auto;
            padding: 24px;
        }
        h1 { text-align: center; }
        input, button {
            box-sizing: border-box;
            width: 100%;
            font-size: 20px;
            padding: 16px;
            border-radius: 12px;
            margin-top: 12px;
        }
        #error { color: #b00020; text-align: center; min-height: 24px; }
    </style>
</head>
<body>
    <h1>Remy</h1>
    <input id="password" type="password" placeholder="Password" autocomplete="current-password">
    <button id="login">Unlock</button>
    <p id="error"></p>
    <script>
        const password = document.getElementById("password");
        const button = document.getElementById("login");
        const error = document.getElementById("error");

        async function login() {
            error.innerText = "";
            const response = await fetch("/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({password: password.value})
            });

            if (response.ok) {
                window.location.reload();
            } else {
                error.innerText = "Incorrect password";
                password.select();
            }
        }

        button.onclick = login;
        password.onkeydown = (event) => {
            if (event.key === "Enter") login();
        };
        password.focus();
    </script>
</body>
</html>
"""


@app.post("/login")
async def login(request: Request):
    if not REMY_PASSWORD:
        raise HTTPException(status_code=503, detail="Remy password is not configured")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid login request")

    supplied = str(body.get("password", ""))
    if not hmac.compare_digest(supplied, REMY_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect password")

    response = JSONResponse({"ok": True})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=AUTH_COOKIE_VALUE,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not has_web_auth(request):
        return HTMLResponse(LOGIN_HTML)

    return """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Remy</title>
    <style>
        body {
            font-family: sans-serif;
            max-width: 600px;
            margin: auto;
            padding: 24px;
        }

        h1 {
            text-align: center;
        }

        #talk {
            display: block;
            width: 100%;
            font-size: 28px;
            padding: 24px;
            border-radius: 18px;
            margin-bottom: 18px;
        }

        #status {
            text-align: center;
            min-height: 30px;
        }

        .task {
            display: flex;
            gap: 12px;
            align-items: flex-start;
            padding: 16px 4px;
            border-bottom: 1px solid #ddd;
            font-size: 20px;
        }

        .task input {
            width: 24px;
            height: 24px;
            margin-top: 2px;
        }
    </style>
</head>

<body>
    <h1>Remy</h1>

    <button id="talk">🎙️ Talk to Remy</button>

    <p id="status"></p>

    <h2>To Do</h2>
    <div id="tasks">Loading...</div>
<details style="margin-top: 30px;">
    <summary style="font-size: 22px; font-weight: bold; cursor: pointer;">
        Completed
    </summary>

    <div id="completed" style="margin-top: 12px;">
        Loading...
    </div>
</details>
    <script>
        const button = document.getElementById("talk");
        const status = document.getElementById("status");
        const tasks = document.getElementById("tasks");
        const completed = document.getElementById("completed");

        async function loadTasks() {
            const response = await fetch("/items");
            const items = await response.json();

            tasks.innerHTML = "";

            if (items.length === 0) {
                tasks.innerHTML = "<p>Nothing to do. 🎉</p>";
                return;
            }

            for (const item of items) {
                const row = document.createElement("div");
                row.className = "task";

                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";

                const label = document.createElement("span");
                label.innerText = item.task;

                checkbox.onchange = async () => {
                    await fetch(`/items/${item.id}/complete`, {
                        method: "POST"
                    });

                    loadTasks();
                    loadCompleted();
                };

                row.appendChild(checkbox);
                row.appendChild(label);
                tasks.appendChild(row);
            }
        }

        async function loadCompleted() {
            const response = await fetch("/completed");
            const items = await response.json();

            completed.innerHTML = "";

            if (items.length === 0) {
                completed.innerHTML = "<p>No completed items yet.</p>";
                return;
            }

            for (const item of items) {
                const row = document.createElement("div");
                row.className = "task";

                const label = document.createElement("span");

                if (item.completed) {
                    label.innerText = item.task + " — " + item.completed;
                } else {
                    label.innerText = item.task;
                }

                row.appendChild(label);
                completed.appendChild(row);
            }
        }

        button.onclick = () => {
            const SpeechRecognition =
                window.SpeechRecognition || window.webkitSpeechRecognition;

            if (!SpeechRecognition) {
                status.innerText =
                    "Voice recognition is not supported in this browser.";
                return;
            }

            const recognition = new SpeechRecognition();
            recognition.lang = "en-US";

            status.innerText = "Listening...";
            recognition.start();

            recognition.onresult = async (event) => {
                const text = event.results[0][0].transcript;

                status.innerText = "Thinking...";

                const response = await fetch("/capture", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({text: text})
                });

                const result = await response.json();

                status.innerText = "✓ " + result.task;

                loadTasks();
            };
        };

        loadTasks();
        loadCompleted();
    </script>
</body>
</html>
"""


@app.post("/capture")
def capture(capture: Capture, request: Request):
    require_web_auth(request)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="""
You are Remy, an intelligent personal assistant.

The user will give you a quick, messy thought captured by voice.

Determine what the user wants done.

Return ONLY valid JSON in this format:

{
  "intent": "task | email | research | calendar | note",
  "task": "a concise description of what needs to happen",
  "details": "important context from the user's original thought",
  "needs_research": true,
  "needs_email_draft": false
}
""",
        messages=[
            {"role": "user", "content": capture.text}
        ],
    )

    result = response.content[0].text.strip()

    if result.startswith("```"):
        result = result.split("\n", 1)[1]
        result = result.rsplit("```", 1)[0].strip()

    item = json.loads(result)

    sheet = get_sheet()
    rows = sheet.get_all_records()

    existing_ids = []

    for row in rows:
        try:
            existing_ids.append(int(row["ID"]))
        except (ValueError, TypeError):
            pass

    item_id = max(existing_ids, default=0) + 1

    created_at = datetime.now(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([
        item_id,
        created_at,
        capture.text,
        item["task"],
        item["details"],
        item["intent"],
        "open",
        "",
        item["needs_research"],
        item["needs_email_draft"],
    ])

    item["id"] = item_id
    item["status"] = "open"

    return item


@app.get("/items")
def get_items(request: Request):
    require_web_auth(request)

    sheet = get_sheet()
    rows = sheet.get_all_records()

    open_items = []

    for row in rows:
        if str(row["Status"]).lower() == "open":
            open_items.append({
                "id": row["ID"],
                "created_at": row["Created"],
                "raw_text": row["Raw Text"],
                "task": row["Task"],
                "details": row["Details"],
                "intent": row["Intent"],
                "status": row["Status"],
                "completed": row["Completed"],
                "needs_research": row["Needs Research"],
                "needs_email_draft": row["Needs Email Draft"],
            })

    return list(reversed(open_items))


@app.post("/items/{item_id}/complete")
def complete_item(item_id: int, request: Request):
    require_web_auth(request)

    sheet = get_sheet()
    rows = sheet.get_all_records()

    completed_at = datetime.now(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M:%S")

    for index, row in enumerate(rows, start=2):
        if str(row["ID"]) == str(item_id):
            sheet.update_cell(index, 7, "done")
            sheet.update_cell(index, 8, completed_at)

            return {
                "id": item_id,
                "status": "done",
                "completed": completed_at,
            }

    return {"error": "Item not found"}


@app.get("/completed")
def get_completed_items(request: Request):
    require_web_auth(request)

    sheet = get_sheet()
    rows = sheet.get_all_records()

    completed_items = []

    for row in rows:
        if str(row["Status"]).lower() == "done":
            completed_items.append({
                "id": row["ID"],
                "task": row["Task"],
                "details": row["Details"],
                "completed": row["Completed"],
            })

    return list(reversed(completed_items))


@app.get("/digest")
def get_digest(request: Request):
    if not (has_web_auth(request) or has_digest_auth(request)):
        raise HTTPException(status_code=401, detail="Authentication required")

    sheet = get_sheet()
    rows = sheet.get_all_records()

    now = datetime.now(ZoneInfo("America/New_York"))
    now_naive = now.replace(tzinfo=None)

    open_items = []
    recently_completed = []

    for row in rows:
        status = str(row["Status"]).lower()

        if status == "open":
            open_items.append(row)

        elif status == "done" and row["Completed"]:
            try:
                completed_at = datetime.strptime(
                    str(row["Completed"]),
                    "%Y-%m-%d %H:%M:%S"
                )

                hours_ago = (
                    now_naive - completed_at
                ).total_seconds() / 3600

                if 0 <= hours_ago <= 24:
                    recently_completed.append(row)

            except ValueError:
                pass

    context = {
        "current_datetime": now.strftime("%Y-%m-%d %H:%M"),
        "open_items": open_items,
        "completed_last_24_hours": recently_completed,
    }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system="""
You are Remy, an excellent executive assistant.

Create a concise daily task briefing from the supplied task data.

Rules:
- Do not invent tasks, deadlines, facts, or completed work.
- Prioritize what appears time-sensitive or important.
- Interpret relative dates such as "tomorrow" relative to the item's Created timestamp.
- Do not simply repeat the database.
- Combine closely related items when useful.
- Call attention to tasks that need research or an email draft.
- Recently completed work should be acknowledged briefly, not dominate the briefing.
- If a task is vague, say what clarification or next action would make it actionable.
- An open item represents work that has NOT been completed yet.
- Treat the Task field as an instruction or desired action, not evidence that the action already happened.
- Never say something was sent, scheduled, set, researched, drafted, contacted, or otherwise completed unless the data explicitly shows it as completed.
- If an open task says "Set a reminder...", describe the action as "Set a reminder...", not "the reminder is set."

Use these sections when relevant:

## Focus
The most important things to act on.

## Other open loops
Remaining actionable work.

## Needs preparation
Research, email drafting, or other work Remy could help prepare.

## Recently completed
A short summary of meaningful completions.

Keep the whole briefing useful, compact, and easy to scan.
""",
        messages=[
            {
                "role": "user",
                "content": json.dumps(context, default=str)
            }
        ],
    )

    return {"digest": response.content[0].text}
