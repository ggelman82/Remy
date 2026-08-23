import os
import json
import sqlite3
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from anthropic import Anthropic

app = FastAPI()
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

DB_FILE = "remy.db"
def get_sheet():
    gc = gspread.service_account(filename="google-service-account.json")
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


@app.get("/", response_class=HTMLResponse)
def home():
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

    <script>
        const button = document.getElementById("talk");
        const status = document.getElementById("status");
        const tasks = document.getElementById("tasks");

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
                };

                row.appendChild(checkbox);
                row.appendChild(label);
                tasks.appendChild(row);
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
    </script>
</body>
</html>
"""
@app.post("/capture")
def capture(capture: Capture):
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

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        """
        INSERT INTO items
        (raw_text, intent, task, details, needs_research, needs_email_draft)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            capture.text,
            item["intent"],
            item["task"],
            item["details"],
            int(item["needs_research"]),
            int(item["needs_email_draft"]),
        ),
    )
    item_id = cursor.lastrowid

    created_at = conn.execute(
        "SELECT created_at FROM items WHERE id = ?",
        (item_id,)
    ).fetchone()[0]

    conn.commit()
    conn.close()

    sheet = get_sheet()
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
def get_items():
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
def complete_item(item_id: int):
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