import os, re, sys, uuid, shutil, sqlite3, hashlib, logging, subprocess, threading, time, resource, base64
import httpx
from flask import Flask, request, redirect, g, abort, render_template_string
from cryptography.fernet import Fernet

def pick_data_dir(path):
    try:
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        test = os.path.join(path, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return path
    except OSError:
        return os.path.abspath(".")

PORT = int(os.environ.get("PORT", "10000"))
DATA_DIR = pick_data_dir(os.environ.get("DATA_DIR", "."))
BOTS_DIR = os.path.abspath(os.path.join(DATA_DIR, "bots"))
DB_PATH = os.path.abspath(os.path.join(DATA_DIR, "platform.db"))
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.environ["SECRET_KEY"].encode()).digest()))
MAX_BOTS_PER_VISITOR = 3
MAX_RUNNING = 40
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
BLOCKED = ["subprocess", "os.system", "os.popen", "ctypes", "socket", "__import__",
           "platform.db", "DATA_DIR", "/var/data", "shutil", "eval(", "exec("]

GROQ_MODELS = [m for m in [os.environ.get("GROQ_MODEL", "").strip(),
                           "openai/gpt-oss-120b",
                           "llama-3.3-70b-versatile",
                           "meta-llama/llama-4-scout-17b-16e-instruct",
                           "llama-3.1-8b-instant"] if m]
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("hosting")
app = Flask(__name__)
procs = {}
lock = threading.Lock()
os.makedirs(BOTS_DIR, exist_ok=True)
log.info("Ma'lumotlar papkasi: %s", DATA_DIR)
log.info("GROQ key: %s, OPENROUTER key: %s", "bor" if GROQ_KEY else "yo'q", "bor" if OPENROUTER_KEY else "yo'q")

# ---------- DB ----------
def run_sql(sql, args=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.lastrowid
    finally:
        conn.close()

run_sql("""CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, visitor TEXT, username TEXT,
    description TEXT, token_enc BLOB, token_hash TEXT UNIQUE,
    status TEXT DEFAULT 'stopped', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

# ---------- Process manager ----------
def set_status(bot_id, status):
    run_sql("UPDATE bots SET status=? WHERE id=?", (status, bot_id))

def limits():
    fsize = 50 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

def running_count():
    return sum(1 for p in procs.values() if p.poll() is None)

def bot_dir(bot_id):
    return os.path.join(BOTS_DIR, str(bot_id))

def bot_log_tail(bot_id, n=1500):
    path = os.path.join(bot_dir(bot_id), "bot.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""

def start_bot(bot_id):
    """Botni ishga tushiradi. Xato bo'lsa xato matnini qaytaradi, muvaffaqiyatda None."""
    with lock:
        old = procs.get(bot_id)
        if old and old.poll() is None:
            return None
        r = run_sql("SELECT * FROM bots WHERE id=?", (bot_id,), "one")
        token = FERNET.decrypt(r["token_enc"]).decode()
        bdir = bot_dir(bot_id)
        os.makedirs(bdir, exist_ok=True)
        script = os.path.join(bdir, "bot.py")
        env = {"PATH": os.environ.get("PATH", ""), "BOT_TOKEN": token,
               "PYTHONUNBUFFERED": "1", "HOME": bdir}
        logf = open(os.path.join(bdir, "bot.log"), "a", encoding="utf-8")
        logf.write("\n--- start ---\n")
        logf.flush()
        p = subprocess.Popen([sys.executable, script],
                             cwd=bdir, env=env, stdout=logf,
                             stderr=subprocess.STDOUT, preexec_fn=limits)
        procs[bot_id] = p
    time.sleep(4)
    if p.poll() is not None:
        set_status(bot_id, "crashed")
        log.warning("Bot %s darhol to'xtadi", bot_id)
        return bot_log_tail(bot_id) or "Noma'lum xato"
    set_status(bot_id, "running")
    return None

def stop_bot(bot_id):
    with lock:
        p = procs.pop(bot_id, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    set_status(bot_id, "stopped")

def watchdog():
    while True:
        time.sleep(60)
        for r in run_sql("SELECT id FROM bots WHERE status='running'", fetch="all"):
            p = procs.get(r["id"])
            if p is None or p.poll() is not None:
                try:
                    start_bot(r["id"])
                    log.info("Restarted bot %s", r["id"])
                except Exception:
                    log.exception("Restart failed: %s", r["id"])

# ---------- AI code generation ----------
SYSTEM_PROMPT = """You are an expert Python developer. Write a COMPLETE, working Telegram bot in ONE Python file.
Rules:
- Use python-telegram-bot version 21 (async API). It is installed. Use only python-telegram-bot, the standard library and httpx.
- Read the token only with os.environ["BOT_TOKEN"].
- Build with Application.builder().token(os.environ["BOT_TOKEN"]).build(), register handlers, and end with app.run_polling(drop_pending_updates=True).
- Do NOT use subprocess, os.system, os.popen, socket, ctypes, eval, exec, shutil or __import__.
- Do NOT access files outside the current directory.
- Store data in memory or in a local SQLite file named data.db.
- Reply to users in the same language as the user's description.
- Include /start and /help.
- Add useful extra features, inline button menus, input validation and error handling.
- Output ONLY the Python code. No explanations, no markdown."""

def call_llm(messages):
    errors = []
    with httpx.Client(timeout=120) as c:
        if GROQ_KEY:
            for model in GROQ_MODELS:
                try:
                    r = c.post("https://api.groq.com/openai/v1/chat/completions",
                               headers={"Authorization": f"Bearer {GROQ_KEY}"},
                               json={"model": model, "messages": messages, "temperature": 0.2})
                except Exception as e:
                    errors.append(f"Groq {model}: {e}")
                    break
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
                log.warning("Groq %s -> %s: %s", model, r.status_code, r.text[:200])
                errors.append(f"Groq {model} {r.status_code}: {r.text[:150]}")
                if r.status_code not in (400, 404):
                    break
        if OPENROUTER_KEY:
            try:
                r = c.post("https://openrouter.ai/api/v1/chat/completions",
                           headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                           json={"model": OPENROUTER_MODEL, "messages": messages, "temperature": 0.2})
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
                errors.append(f"OpenRouter {r.status_code}: {r.text[:150]}")
            except Exception as e:
                errors.append(f"OpenRouter: {e}")
    if errors:
        raise RuntimeError(" | ".join(errors))
    raise RuntimeError("AI kaliti sozlanmagan")

def extract_code(text):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()

def generate_code(desc):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": desc}]
    for _ in range(3):
        raw = call_llm(messages)
        code = extract_code(raw)
        bad = [b for b in BLOCKED if b in code]
        if bad:
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Bu so'zlarni ishlatma: {bad}. Qaytadan to'liq kod yoz."}]
            continue
        try:
            compile(code, "bot.py", "exec")
            return code
        except SyntaxError as e:
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Syntax error: {e}. Faqat to'liq tuzatilgan kodni yoz."}]
    raise RuntimeError("AI to'g'ri kod yoza olmadi")

def check_token(token):
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    except Exception:
        return None
    if r.status_code != 200 or not r.json().get("ok"):
        return None
    return r.json()["result"]["username"]

# ---------- Visitor identity (cookie) ----------
@app.before_request
def set_visitor():
    vid = request.cookies.get("vid")
    g.new_vid = False
    if not vid or len(vid) != 32:
        vid = uuid.uuid4().hex
        g.new_vid = True
    g.vid = vid

@app.after_request
def save_visitor(resp):
    if getattr(g, "new_vid", False):
        resp.set_cookie("vid", g.vid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax")
    return resp

def own_bot(bot_id):
    r = run_sql("SELECT * FROM bots WHERE id=? AND visitor=?", (bot_id, g.vid), "one")
    if not r:
        abort(404)
    return r

# ---------- Pages ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot Yarat</title>
<style>
body{font-family:system-ui;max-width:640px;margin:24px auto;padding:0 16px;background:#f5f6fa}
.card{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 2px 8px #0001}
textarea,input{width:100%;padding:10px;margin:6px 0 12px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}
button,.btn{background:#2b7de9;color:#fff;border:0;padding:8px 14px;border-radius:8px;cursor:pointer;margin:4px 4px 0 0;text-decoration:none;display:inline-block;font-size:15px}
.danger{background:#e04848}.muted{color:#777;font-size:14px}
.msg{background:#fff3cd;padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-word;font-size:14px}
pre{background:#111;color:#0f0;padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-word;font-size:13px}
form.inline{display:inline}
</style></head><body>
<h2>🤖 AI Bot Yaratish</h2>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
<div class="card">
<form method="post" action="/create">
<label>Botingiz nima qilsin?</label>
<textarea name="desc" rows="4" required minlength="10" maxlength="1500" placeholder="Masalan: foydalanuvchilarga kunlik horoskop yuboradigan bot"></textarea>
<label>BotFather'dan olingan token</label>
<input name="token" required placeholder="123456789:AAH..." autocomplete="off">
<button type="submit">Yaratish va ishga tushirish</button>
<p class="muted">Bot 1-2 daqiqada tayyor bo'ladi. Tokenni hech kimga bermang. Botlar shu brauzerga bog'lanadi.</p>
</form></div>
<h3>Sizning botlaringiz</h3>
{% for b in bots %}
<div class="card"><b>@{{ b.username }}</b> —
{% if b.status == 'running' %}🟢 ishlayapti{% elif b.status == 'crashed' %}🔴 xato bilan to'xtagan{% else %}⚪ to'xtagan{% endif %}
<p class="muted">{{ b.description[:200] }}</p>
<form class="inline" method="post" action="/bot/{{ b.id }}/start"><button>▶️ Ishga tushirish</button></form>
<form class="inline" method="post" action="/bot/{{ b.id }}/stop"><button>⏹ To'xtatish</button></form>
<a class="btn" href="/bot/{{ b.id }}/log">📄 Log</a>
<form class="inline" method="post" action="/bot/{{ b.id }}/delete" onsubmit='return confirm("Botni o`chirasizmi?")'><button class="danger">🗑 O'chirish</button></form>
</div>
{% else %}<p class="muted">Hozircha botlar yo'q.</p>{% endfor %}
</body></html>"""

LOG_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Log</title>
<style>body{font-family:system-ui;max-width:640px;margin:24px auto;padding:0 16px;background:#f5f6fa}
pre{background:#111;color:#0f0;padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-word;font-size:13px}
a{color:#2b7de9}</style></head><body>
<p><a href="/">← Orqaga</a></p><h3>@{{ b.username }} — log</h3>
<pre>{{ log_text or "Log bo'sh" }}</pre>
</body></html>"""

def render_index(msg=""):
    bots = run_sql("SELECT * FROM bots WHERE visitor=? ORDER BY id DESC", (g.vid,), "all")
    return render_template_string(PAGE, bots=bots, msg=msg)

@app.get("/")
def index():
    return render_index(request.args.get("msg", ""))

@app.get("/health")
def health():
    return "ok"

@app.post("/create")
def create():
    desc = request.form.get("desc", "").strip()[:1500]
    token = request.form.get("token", "").strip()
    if len(desc) < 10:
        return render_index("Botingiz haqida kamida 10 ta belgi yozing.")
    if not TOKEN_RE.match(token):
        return render_index("Token formati noto'g'ri.")
    n = run_sql("SELECT COUNT(*) AS n FROM bots WHERE visitor=?", (g.vid,), "one")["n"]
    if n >= MAX_BOTS_PER_VISITOR:
        return render_index(f"Maksimal {MAX_BOTS_PER_VISITOR} ta bot. Avval bittasini o'chiring.")
    if running_count() >= MAX_RUNNING:
        return render_index("Server band. Keyinroq urinib ko'ring.")
    th = hashlib.sha256(token.encode()).hexdigest()
    if run_sql("SELECT id FROM bots WHERE token_hash=?", (th,), "one"):
        return render_index("Bu token allaqachon ro'yxatdan o'tgan.")
    username = check_token(token)
    if not username:
        return render_index("Token yaroqsiz. BotFather'dan to'g'ri tokenni oling.")
    try:
        code = generate_code(desc)
    except Exception as e:
        log.exception("Generation failed")
        return render_index(f"AI xatosi: {str(e)[:300]}")
    bot_id = run_sql("INSERT INTO bots (visitor, username, description, token_enc, token_hash) VALUES (?,?,?,?,?)",
                     (g.vid, username, desc, FERNET.encrypt(token.encode()), th))
    bdir = bot_dir(bot_id)
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "bot.py"), "w", encoding="utf-8") as f:
        f.write(code)
    err = start_bot(bot_id)
    if err:
        return render_index(f"Bot yaratildi, lekin ishga tushmadi. Xato:\n{err}")
    return redirect("/")

@app.route("/bot/<int:bot_id>/<action>", methods=["GET", "POST"])
def bot_action(bot_id, action):
    if request.method == "GET":
        return redirect("/")
    own_bot(bot_id)
    if action == "start":
        if running_count() >= MAX_RUNNING:
            return render_index("Server band. Keyinroq urinib ko'ring.")
        err = start_bot(bot_id)
        if err:
            return render_index(f"Bot ishga tushmadi. Xato:\n{err}")
    elif action == "stop":
        stop_bot(bot_id)
    elif action == "delete":
        stop_bot(bot_id)
        run_sql("DELETE FROM bots WHERE id=?", (bot_id,))
        shutil.rmtree(bot_dir(bot_id), ignore_errors=True)
    else:
        abort(404)
    return redirect("/")

@app.get("/bot/<int:bot_id>/log")
def bot_log_page(bot_id):
    r = own_bot(bot_id)
    return render_template_string(LOG_PAGE, b=r, log_text=bot_log_tail(bot_id, 4000))

# ---------- Main ----------
if __name__ == "__main__":
    for r in run_sql("SELECT id FROM bots WHERE status='running'", fetch="all"):
        try:
            start_bot(r["id"])
        except Exception:
            log.exception("Startup failed: %s", r["id"])
    threading.Thread(target=watchdog, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
