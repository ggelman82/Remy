import os
import json
import sqlite3

from fastapi import FastAPI
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


@app.get("/")
def home():
    return {"message": "Hi. I'm Remy."}


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