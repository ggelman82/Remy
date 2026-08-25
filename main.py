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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from anthropic import Anthropic

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
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


class ItemUpdate(BaseModel):
    task: str
    details: str = ""


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
    <meta name="theme-color" content="#5b4fd6">
    <link rel="manifest" href="/manifest.webmanifest">
    <link rel="icon" href="/icon.svg" type="image/svg+xml">
    <title>Remy</title>
    <style>
        :root {
            color-scheme: light;
            --ink: #24223a;
            --muted: #6b6880;
            --surface: #ffffff;
            --soft: #f3f1ff;
            --line: #dedbea;
            --primary: #5b4fd6;
            --primary-dark: #4438b8;
            --danger: #b42318;
        }

        * { box-sizing: border-box; }

        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background: #faf9ff;
            max-width: 600px;
            margin: auto;
            padding: 20px 18px 48px;
        }

        h1 {
            text-align: center;
            margin: 6px 0 20px;
            color: var(--primary-dark);
        }

        button, textarea { font: inherit; }

        .capture-card {
            padding: 16px;
            border: 1px solid var(--line);
            border-radius: 18px;
            background: var(--surface);
            box-shadow: 0 6px 24px rgba(52, 43, 115, 0.08);
        }

        #typed-entry {
            width: 100%;
            min-height: 90px;
            resize: vertical;
            padding: 13px 14px;
            border: 1px solid var(--line);
            border-radius: 12px;
            color: var(--ink);
            background: #fff;
        }

        #typed-entry:focus {
            outline: 3px solid rgba(91, 79, 214, 0.16);
            border-color: var(--primary);
        }

        .capture-actions {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-top: 10px;
        }

        .primary-button, .secondary-button, #install {
            border: 0;
            border-radius: 12px;
            padding: 13px 14px;
            font-weight: 700;
            cursor: pointer;
        }

        .primary-button {
            color: white;
            background: var(--primary);
        }

        .primary-button:hover { background: var(--primary-dark); }

        .secondary-button {
            color: var(--primary-dark);
            background: var(--soft);
        }

        button:disabled { opacity: .55; cursor: wait; }

        #install {
            display: none;
            width: 100%;
            margin-top: 12px;
            color: var(--primary-dark);
            border: 1px solid var(--line);
            background: white;
        }

        #status {
            text-align: center;
            min-height: 24px;
            margin: 12px 0 4px;
            color: var(--muted);
        }

        .task {
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 12px;
            align-items: start;
            padding: 15px 4px;
            border-bottom: 1px solid var(--line);
            font-size: 18px;
        }

        .task-checkbox {
            width: 24px;
            height: 24px;
            margin-top: 2px;
            accent-color: var(--primary);
        }

        .task-copy { min-width: 0; }
        .task-title { overflow-wrap: anywhere; }

        .task-details {
            margin-top: 4px;
            color: var(--muted);
            font-size: 14px;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
        }

        .task-actions { display: flex; gap: 5px; }

        .icon-button {
            width: 40px;
            height: 40px;
            padding: 0;
            border: 0;
            border-radius: 10px;
            color: var(--muted);
            background: transparent;
            cursor: pointer;
            font-size: 19px;
        }

        .icon-button:hover { background: var(--soft); }
        .delete-button:hover { color: var(--danger); background: #fff0ee; }

        .empty { color: var(--muted); }

        details { margin-top: 30px; }
        summary { font-size: 21px; font-weight: 700; cursor: pointer; }

        dialog {
            width: min(520px, calc(100% - 30px));
            border: 0;
            border-radius: 18px;
            padding: 20px;
            color: var(--ink);
            box-shadow: 0 20px 60px rgba(25, 20, 65, .28);
        }

        dialog::backdrop { background: rgba(25, 20, 50, .48); }

        dialog label {
            display: block;
            margin: 12px 0 6px;
            font-weight: 700;
        }

        dialog input, dialog textarea {
            width: 100%;
            padding: 12px;
            border: 1px solid var(--line);
            border-radius: 10px;
        }

        #edit-details { min-height: 100px; resize: vertical; }

        .dialog-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            margin-top: 16px;
        }

        @media (max-width: 420px) {
            .capture-actions { grid-template-columns: 1fr; }
            .task { grid-template-columns: auto 1fr; }
            .task-actions { grid-column: 2; }
        }
    </style>
</head>

<body>
    <h1>Remy</h1>

    <section class="capture-card" aria-label="Add a task">
        <textarea id="typed-entry" placeholder="Type something for Remy..." maxlength="5000"></textarea>
        <div class="capture-actions">
            <button id="add" class="primary-button">Add item</button>
            <button id="talk" class="secondary-button">🎙️ Talk to Remy</button>
        </div>
    </section>

    <button id="install">Install Remy on this device</button>

    <p id="status" role="status" aria-live="polite"></p>

    <h2>To Do</h2>
    <div id="tasks">Loading...</div>
    <details>
        <summary>Completed</summary>
        <div id="completed">Loading...</div>
    </details>

    <dialog id="edit-dialog">
        <form id="edit-form">
            <h2>Edit item</h2>
            <label for="edit-task">Task</label>
            <input id="edit-task" maxlength="500" required>
            <label for="edit-details">Details</label>
            <textarea id="edit-details" maxlength="5000"></textarea>
            <div class="dialog-actions">
                <button type="button" id="edit-cancel" class="secondary-button">Cancel</button>
                <button type="submit" class="primary-button">Save</button>
            </div>
        </form>
    </dialog>

    <script>
        const talkButton = document.getElementById("talk");
        const addButton = document.getElementById("add");
        const typedEntry = document.getElementById("typed-entry");
        const status = document.getElementById("status");
        const tasks = document.getElementById("tasks");
        const completed = document.getElementById("completed");
        const editDialog = document.getElementById("edit-dialog");
        const editForm = document.getElementById("edit-form");
        const editTask = document.getElementById("edit-task");
        const editDetails = document.getElementById("edit-details");
        const installButton = document.getElementById("install");
        let editingId = null;
        let installPrompt = null;

        async function api(url, options = {}) {
            const response = await fetch(url, options);
            let result = {};
            try { result = await response.json(); } catch (_) {}
            if (response.status === 401) {
                window.location.reload();
                throw new Error("Please unlock Remy again.");
            }
            if (!response.ok) {
                throw new Error(result.detail || result.error || "Something went wrong.");
            }
            return result;
        }

        function makeIconButton(icon, label, className, handler) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "icon-button " + (className || "");
            button.innerText = icon;
            button.setAttribute("aria-label", label);
            button.title = label;
            button.onclick = handler;
            return button;
        }

        function renderTask(item, isCompleted = false) {
            const row = document.createElement("div");
            row.className = "task";

            if (!isCompleted) {
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.className = "task-checkbox";
                checkbox.setAttribute("aria-label", `Mark ${item.task} done`);
                checkbox.onchange = async () => {
                    checkbox.disabled = true;
                    try {
                        await api(`/items/${item.id}/complete`, {method: "POST"});
                        await Promise.all([loadTasks(), loadCompleted()]);
                    } catch (error) {
                        checkbox.checked = false;
                        checkbox.disabled = false;
                        status.innerText = error.message;
                    }
                };
                row.appendChild(checkbox);
            } else {
                const spacer = document.createElement("span");
                spacer.innerText = "✓";
                spacer.style.color = "var(--primary)";
                row.appendChild(spacer);
            }

            const copy = document.createElement("div");
            copy.className = "task-copy";
            const title = document.createElement("div");
            title.className = "task-title";
            title.innerText = item.task;
            copy.appendChild(title);

            if (item.details) {
                const details = document.createElement("div");
                details.className = "task-details";
                details.innerText = item.details;
                copy.appendChild(details);
            }
            if (isCompleted && item.completed) {
                const date = document.createElement("div");
                date.className = "task-details";
                date.innerText = "Completed " + item.completed;
                copy.appendChild(date);
            }
            row.appendChild(copy);

            const actions = document.createElement("div");
            actions.className = "task-actions";
            actions.appendChild(makeIconButton("✏️", "Edit item", "", () => openEdit(item)));
            actions.appendChild(makeIconButton("🗑️", "Delete item", "delete-button", () => deleteItem(item)));
            row.appendChild(actions);

            return row;
        }

        async function loadTasks() {
            try {
                const items = await api("/items");
                tasks.innerHTML = "";
                if (items.length === 0) {
                    tasks.innerHTML = '<p class="empty">Nothing to do. 🎉</p>';
                    return;
                }
                for (const item of items) tasks.appendChild(renderTask(item));
            } catch (error) {
                tasks.innerHTML = `<p class="empty">${error.message}</p>`;
            }
        }

        async function loadCompleted() {
            try {
                const items = await api("/completed");
                completed.innerHTML = "";
                if (items.length === 0) {
                    completed.innerHTML = '<p class="empty">No completed items yet.</p>';
                    return;
                }
                for (const item of items) completed.appendChild(renderTask(item, true));
            } catch (error) {
                completed.innerHTML = `<p class="empty">${error.message}</p>`;
            }
        }

        async function submitCapture(text) {
            text = text.trim();
            if (!text) {
                typedEntry.focus();
                return;
            }
            addButton.disabled = true;
            talkButton.disabled = true;
            status.innerText = "Thinking...";
            try {
                const result = await api("/capture", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({text})
                });
                typedEntry.value = "";
                status.innerText = "✓ " + result.task;
                await loadTasks();
            } catch (error) {
                status.innerText = error.message;
            } finally {
                addButton.disabled = false;
                talkButton.disabled = false;
            }
        }

        addButton.onclick = () => submitCapture(typedEntry.value);
        typedEntry.onkeydown = (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submitCapture(typedEntry.value);
            }
        };

        talkButton.onclick = () => {
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

            recognition.onresult = (event) => {
                const text = event.results[0][0].transcript;
                typedEntry.value = text;
                submitCapture(text);
            };
            recognition.onerror = () => {
                status.innerText = "I could not hear that. Please try again or type it.";
            };
        };

        function openEdit(item) {
            editingId = item.id;
            editTask.value = item.task || "";
            editDetails.value = item.details || "";
            editDialog.showModal();
            editTask.focus();
        }

        document.getElementById("edit-cancel").onclick = () => editDialog.close();
        editForm.onsubmit = async (event) => {
            event.preventDefault();
            const task = editTask.value.trim();
            if (!task) return;
            try {
                await api(`/items/${editingId}`, {
                    method: "PUT",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({task, details: editDetails.value.trim()})
                });
                editDialog.close();
                status.innerText = "✓ Item updated";
                await Promise.all([loadTasks(), loadCompleted()]);
            } catch (error) {
                status.innerText = error.message;
            }
        };

        async function deleteItem(item) {
            if (!window.confirm(`Delete “${item.task}” permanently?`)) return;
            try {
                await api(`/items/${item.id}`, {method: "DELETE"});
                status.innerText = "Item deleted";
                await Promise.all([loadTasks(), loadCompleted()]);
            } catch (error) {
                status.innerText = error.message;
            }
        }

        window.addEventListener("beforeinstallprompt", (event) => {
            event.preventDefault();
            installPrompt = event;
            installButton.style.display = "block";
        });
        installButton.onclick = async () => {
            if (!installPrompt) return;
            installPrompt.prompt();
            await installPrompt.userChoice;
            installPrompt = null;
            installButton.style.display = "none";
        };
        window.addEventListener("appinstalled", () => {
            status.innerText = "Remy installed";
            installButton.style.display = "none";
        });

        if ("serviceWorker" in navigator) {
            window.addEventListener("load", () => navigator.serviceWorker.register("/service-worker.js"));
        }

        loadTasks();
        loadCompleted();
    </script>
</body>
</html>
"""


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(
        {
            "name": "Remy",
            "short_name": "Remy",
            "description": "Private voice and typed task capture",
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#faf9ff",
            "theme_color": "#5b4fd6",
            "icons": [
                {
                    "src": "/static/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                }
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/icon.svg")
def icon():
    return Response(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#5b4fd6"/>
<circle cx="256" cy="256" r="150" fill="#ffffff" opacity=".14"/>
<path fill="#fff" d="M142 130h121c78 0 127 39 127 105 0 43-23 75-63 92l71 85h-94l-58-72h-28v72h-76V130zm76 65v81h42c35 0 53-14 53-41 0-27-18-40-53-40h-42z"/>
</svg>""",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/service-worker.js")
def service_worker():
    return Response(
        """const CACHE = "remy-shell-v1";
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(["/manifest.webmanifest", "/icon.svg", "/static/icon-192.png", "/static/icon-512.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname === "/manifest.webmanifest" || url.pathname === "/icon.svg" || url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
  }
});""",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.post("/capture")
def capture(capture: Capture, request: Request):
    require_web_auth(request)

    capture_text = capture.text.strip()
    if not capture_text:
        raise HTTPException(status_code=400, detail="Please enter something for Remy.")
    if len(capture_text) > 5000:
        raise HTTPException(status_code=400, detail="Please keep entries under 5,000 characters.")

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
            {"role": "user", "content": capture_text}
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
        capture_text,
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


@app.put("/items/{item_id}")
def update_item(item_id: int, update: ItemUpdate, request: Request):
    require_web_auth(request)

    task = update.task.strip()
    details = update.details.strip()
    if not task:
        raise HTTPException(status_code=400, detail="Task cannot be empty.")
    if len(task) > 500 or len(details) > 5000:
        raise HTTPException(status_code=400, detail="The edited item is too long.")

    sheet = get_sheet()
    rows = sheet.get_all_records()

    for index, row in enumerate(rows, start=2):
        if str(row["ID"]) == str(item_id):
            sheet.update_cell(index, 4, task)
            sheet.update_cell(index, 5, details)
            return {
                "id": item_id,
                "task": task,
                "details": details,
                "status": row["Status"],
            }

    raise HTTPException(status_code=404, detail="Item not found.")


@app.delete("/items/{item_id}")
def delete_item(item_id: int, request: Request):
    require_web_auth(request)

    sheet = get_sheet()
    rows = sheet.get_all_records()

    for index, row in enumerate(rows, start=2):
        if str(row["ID"]) == str(item_id):
            sheet.delete_rows(index)
            return {"id": item_id, "deleted": True}

    raise HTTPException(status_code=404, detail="Item not found.")


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

    raise HTTPException(status_code=404, detail="Item not found.")


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
