import os
import json
import sqlite3

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from anthropic import Anthropic

app = FastAPI()
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

DB_FILE = "remy.db"


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
</head>
<body style="font-family: sans-serif; text-align: center; padding: 40px;">
    <h1>Remy</h1>
    <button
        id="talk"
        style="font-size: 32px; padding: 30px; border-radius: 20px;"
    >
        🎙️ Talk to Remy
    </button>

    <p id="status"></p>

    <script>
        const button = document.getElementById("talk");
        const status = document.getElementById("status");

        button.onclick = () => {
            const SpeechRecognition =
                window.SpeechRecognition || window.webkitSpeechRecognition;

            if (!SpeechRecognition) {
                status.innerText = "Voice recognition is not supported in this browser.";
                return;
            }

            const recognition = new SpeechRecognition();
            recognition.lang = "en-US";

            status.innerText = "Listening...";
            recognition.start();

            recognition.onresult = async (event) => {
                const text = event.results[0][0].transcript;
                status.innerText = "You said: " + text;

                const response = await fetch("/capture", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({text: text})
                });

                const result = await response.json();
                status.innerText = "✓ " + result.task;
            };
        };
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
    conn.commit()
    conn.close()

    item["id"] = item_id
    item["status"] = "open"

    return item


@app.get("/items")
def get_items():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM items WHERE status = 'open' ORDER BY id DESC"
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]
@app.post("/items/{item_id}/complete")
def complete_item(item_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE items SET status = 'done' WHERE id = ?",
        (item_id,)
    )
    conn.commit()
    conn.close()

    return {"id": item_id, "status": "done"}