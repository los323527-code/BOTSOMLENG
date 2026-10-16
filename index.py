import os
import json
import hashlib
import secrets
import asyncio
from typing import Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="Telegram AI Voice Bot Platform")

TELEGRAM_API = "https://api.telegram.org"
KIRI_API = "https://api.kiritts.com/v1/audio/speech"

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN", "").strip()
MANAGER_BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN", "").strip()
KIRI_API_KEY = os.getenv("KIRI_API_KEY", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
SETUP_SECRET = os.getenv("SETUP_SECRET", "").strip()
MAIN_WEBHOOK_SECRET = os.getenv("MAIN_WEBHOOK_SECRET", "").strip()
MANAGER_WEBHOOK_SECRET = os.getenv("MANAGER_WEBHOOK_SECRET", "").strip()
KIRI_VOICE = os.getenv("KIRI_VOICE", "Maly").strip() or "Maly"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()

# Speech-to-text (SRT) via somlengsrt.com  (optional; needs API Starter/Pro plan)
SRT_API = "https://somlengsrt.com/api/v1"
SOMLENG_SRT_TOKEN = os.getenv("SOMLENG_SRT_TOKEN", "").strip()
SRT_LANGS = {"km-KH", "en-US", "ja-JP", "zh-CN"}
SRT_MAX_BYTES = 20 * 1024 * 1024  # Telegram getFile limit for bots

def required_env():
    missing = []
    for name, value in [
        ("MAIN_BOT_TOKEN", MAIN_BOT_TOKEN),
        ("MANAGER_BOT_TOKEN", MANAGER_BOT_TOKEN),
        ("KIRI_API_KEY", KIRI_API_KEY),
        ("APP_BASE_URL", APP_BASE_URL),
        ("SETUP_SECRET", SETUP_SECRET),
        ("MAIN_WEBHOOK_SECRET", MAIN_WEBHOOK_SECRET),
        ("MANAGER_WEBHOOK_SECRET", MANAGER_WEBHOOK_SECRET),
        ("SUPABASE_URL", SUPABASE_URL),
        ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY),
        ("FERNET_KEY", FERNET_KEY),
    ]:
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))

def fernet():
    if not FERNET_KEY:
        raise RuntimeError("FERNET_KEY is not configured")
    return Fernet(FERNET_KEY.encode())

def encrypt_token(token: str) -> str:
    return fernet().encrypt(token.encode()).decode()

def decrypt_token(value: str) -> str:
    try:
        return fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt stored bot token; check FERNET_KEY") from exc

def bot_key_for(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]

def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()

async def tg_call(token: str, method: str, *, json_body=None, data=None, files=None):
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f"{TELEGRAM_API}/bot{token}/{method}",
            json=json_body,
            data=data,
            files=files,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:300]}
    if response.status_code >= 400 or not payload.get("ok", False):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', response.status_code)}")
    return payload.get("result")

async def send_text(token: str, chat_id: int, text: str):
    try:
        await tg_call(token, "sendMessage", json_body={"chat_id": chat_id, "text": text})
    except Exception:
        # Do not log tokens or full API response data.
        pass

async def db_request(method: str, path: str, *, params=None, body=None, prefer=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase environment variables are missing")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method,
            f"{SUPABASE_URL}/rest/v1/{path}",
            params=params,
            json=body,
            headers=headers,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Database request failed ({response.status_code})")
    if not response.content:
        return None
    return response.json()

async def find_connected_bot(bot_key: str):
    rows = await db_request(
        "GET", "connected_bots",
        params={
            "select": "bot_key,owner_tg_id,bot_token_enc,bot_username,webhook_secret_hash",
            "bot_key": f"eq.{bot_key}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None

async def save_connected_bot(record: dict):
    await db_request(
        "POST", "connected_bots",
        params={"on_conflict": "bot_key"},
        body=record,
        prefer="resolution=merge-duplicates,return=minimal",
    )

async def create_speech(text: str) -> bytes:
    if not KIRI_API_KEY:
        raise RuntimeError("KIRI_API_KEY is not configured")
    payload = {
        "model": "kiritts",
        "input": text[:4000],
        "voice": KIRI_VOICE,
        "response_format": "mp3",
    }
    async with httpx.AsyncClient(timeout=55) as client:
        response = await client.post(
            KIRI_API,
            headers={
                "Authorization": f"Bearer {KIRI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        # Avoid returning the API key or raw sensitive headers.
        detail = "Kiritts API request failed."
        try:
            body = response.json()
            detail = str(body.get("detail", detail))[:250]
        except ValueError:
            pass
        raise RuntimeError(detail)
    if not response.content:
        raise RuntimeError("Kiritts returned an empty audio file")
    return response.content


def _find_key(obj, keys):
    """Search nested JSON for the first matching key (robust to response shape)."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            found = _find_key(v, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, keys)
            if found not in (None, ""):
                return found
    return None

async def download_telegram_file(token: str, file_id: str) -> bytes:
    info = await tg_call(token, "getFile", json_body={"file_id": file_id})
    file_path = info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram file path missing")
    async with httpx.AsyncClient(timeout=50) as client:
        response = await client.get(f"{TELEGRAM_API}/file/bot{token}/{file_path}")
    if response.status_code >= 400:
        raise RuntimeError("Could not download audio from Telegram")
    return response.content

async def transcribe_to_srt(audio: bytes, filename: str, language: str) -> bytes:
    if not SOMLENG_SRT_TOKEN:
        raise RuntimeError("SOMLENG_SRT_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {SOMLENG_SRT_TOKEN}"}
    async with httpx.AsyncClient(timeout=50) as client:
        up = await client.post(
            f"{SRT_API}/transcribe",
            headers=headers,
            files={"audio": (filename, audio)},
            data={"language": language},
        )
        if up.status_code in (401, 402, 403):
            raise RuntimeError("SRT API token invalid or subscription required")
        if up.status_code >= 400:
            raise RuntimeError(f"SRT upload failed ({up.status_code})")
        file_id = _find_key(up.json(), ["id", "file_id", "fileId"])
        if not file_id:
            raise RuntimeError("SRT API did not return a file id")

        # Poll status (Vercel function limit: stay under ~50s total)
        for _ in range(22):
            await asyncio.sleep(2)
            st = await client.get(f"{SRT_API}/files/{file_id}/status", headers=headers)
            if st.status_code >= 400:
                raise RuntimeError(f"SRT status failed ({st.status_code})")
            status = str(_find_key(st.json(), ["status"]) or "").lower()
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError("SRT transcription failed")
        else:
            raise RuntimeError("TIMEOUT")

        srt = await client.get(f"{SRT_API}/files/{file_id}/srt", headers=headers)
        if srt.status_code >= 400 or not srt.content:
            raise RuntimeError("Could not download SRT file")
        return srt.content

def extract_audio_ref(message: dict):
    """Return (file_id, filename, size) for voice/audio/video-note/audio documents."""
    for key, default_name in (("voice", "voice.ogg"), ("audio", "audio.mp3"), ("video_note", "video.mp4"), ("video", "video.mp4")):
        media = message.get(key)
        if media and media.get("file_id"):
            return media["file_id"], media.get("file_name") or default_name, media.get("file_size") or 0
    doc = message.get("document")
    if doc and doc.get("file_id") and str(doc.get("mime_type", "")).startswith(("audio/", "video/")):
        return doc["file_id"], doc.get("file_name") or "audio.mp3", doc.get("file_size") or 0
    return None

async def process_srt_message(token: str, message: dict, ref) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    file_id, filename, size = ref
    caption = (message.get("caption") or "").strip()
    language = caption if caption in SRT_LANGS else "km-KH"

    if not SOMLENG_SRT_TOKEN:
        await send_text(token, chat_id, "មុខងារបម្លែងសំឡេងទៅជា SRT មិនទាន់បើកទេ។")
        return
    if size and size > SRT_MAX_BYTES:
        await send_text(token, chat_id, "ឯកសារធំពេក។ សូមផ្ញើតិចជាង 20MB។")
        return

    await send_text(token, chat_id, f"កំពុងបម្លែងសំឡេងទៅជា SRT ({language}) សូមរង់ចាំ...")
    try:
        audio = await download_telegram_file(token, file_id)
        srt = await transcribe_to_srt(audio, filename, language)
        await tg_call(
            token,
            "sendDocument",
            data={"chat_id": str(chat_id)},
            files={"document": ("subtitle.srt", srt, "application/x-subrip")},
        )
    except Exception as exc:
        if str(exc) == "TIMEOUT":
            msg = "ការបម្លែងយូរពេក។ សូមសាកម្ដងទៀតជាមួយ audio ខ្លីជាងនេះ។"
        elif "subscription" in str(exc).lower():
            msg = "SRT API Token មិនត្រឹមត្រូវ ឬត្រូវការ Subscription។"
        else:
            msg = "សុំទោស! មិនអាចបង្កើត SRT បានទេ។ សូមសាកម្ដងទៀត។"
        await send_text(token, chat_id, msg)

async def process_voice_message(token: str, message: dict, *, is_manager: bool = False):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not is_manager and chat_id:
        ref = extract_audio_ref(message)
        if ref:
            await process_srt_message(token, message, ref)
            return

    if not chat_id or not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        if is_manager:
            reply = (
                "សួស្តី! នេះជា Bot Manager សម្រាប់ភ្ជាប់ Bot របស់អ្នក។\n\n"
                "របៀបប្រើ៖ ផ្ញើ Bot Token ដែលបានបង្កើតតាម @BotFather មកទីនេះ។ "
                "ប្រព័ន្ធនឹងផ្ទៀងផ្ទាត់ ហើយកំណត់ Webhook ដោយស្វ័យប្រវត្តិ។\n\n"
                "សុវត្ថិភាព៖ Token ជាពាក្យសម្ងាត់។ ផ្ញើវាតែក្នុង Private Chat នេះ ហើយកុំចែករំលែកវាជាសាធារណៈ។ "
                "បន្ទាប់ពីភ្ជាប់បាន អ្នកអាចប្រើ Bot ផ្ទាល់ខ្លួនរបស់អ្នកដើម្បីបម្លែងអត្ថបទទៅជាសំឡេង AI។"
            )
        else:
            reply = (
                "សួស្តី! ផ្ញើអត្ថបទដែលអ្នកចង់បម្លែងទៅជាសំឡេង AI មកខ្ញុំ។\n"
                "ឧទាហរណ៍៖ /voice Maly (សម្រាប់កំណត់សំឡេង មុខងារនេះអាស្រ័យលើការកំណត់របស់ម្ចាស់ប្រព័ន្ធ)\n"
                "ផ្ញើអត្ថបទធម្មតាដើម្បីបង្កើតឯកសារសំឡេង MP3។\n"
                "ផ្ញើ Voice/Audio ដើម្បីបម្លែងទៅជា SRT (Caption: km-KH, en-US, ja-JP, zh-CN)។"
            )
        await send_text(token, chat_id, reply)
        return

    if text.startswith("/voice"):
        await send_text(token, chat_id, "សូមផ្ញើអត្ថបទដែលអ្នកចង់បម្លែងទៅជាសំឡេង AI។")
        return

    if text.startswith("/"):
        await send_text(token, chat_id, "សូមផ្ញើអត្ថបទធម្មតា ដើម្បីបង្កើតសំឡេង AI។")
        return

    if len(text) > 4000:
        await send_text(token, chat_id, "អត្ថបទវែងពេក។ សូមកាត់ឱ្យតិចជាង 4,000 តួអក្សរ។")
        return

    await send_text(token, chat_id, "កំពុងបង្កើតសំឡេង AI សូមរង់ចាំបន្តិច...")
    try:
        audio = await create_speech(text)
        await tg_call(
            token,
            "sendAudio",
            data={"chat_id": str(chat_id), "title": "AI Voice", "performer": "AI Voice Bot"},
            files={"audio": ("ai-voice.mp3", audio, "audio/mpeg")},
        )
    except Exception:
        await send_text(token, chat_id, "សុំទោស! មិនអាចបង្កើតសំឡេងបានទេ។ សូមពិនិត្យ API Key ឬសាកម្ដងទៀត។")

@app.get("/")
async def home():
    return {
        "ok": True,
        "service": "Telegram AI Voice Bot Platform",
        "routes": ["/health", "/setup-webhooks", "/webhook/main", "/webhook/manager", "/webhook/{bot_key}"],
    }

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/setup-webhooks")
async def setup_webhooks(request: Request, x_setup_secret: Optional[str] = Header(default=None)):
    if not SETUP_SECRET or not secrets.compare_digest(x_setup_secret or "", SETUP_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not APP_BASE_URL or not MAIN_BOT_TOKEN or not MANAGER_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Configure APP_BASE_URL and both bot tokens first")
    try:
        main_result = await tg_call(
            MAIN_BOT_TOKEN, "setWebhook",
            json_body={
                "url": f"{APP_BASE_URL}/webhook/main",
                "secret_token": MAIN_WEBHOOK_SECRET,
                "allowed_updates": ["message"],
            },
        )
        manager_result = await tg_call(
            MANAGER_BOT_TOKEN, "setWebhook",
            json_body={
                "url": f"{APP_BASE_URL}/webhook/manager",
                "secret_token": MANAGER_WEBHOOK_SECRET,
                "allowed_updates": ["message"],
            },
        )
        return {"ok": True, "main_webhook": main_result, "manager_webhook": manager_result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])

async def handle_fixed_bot(request: Request, token: str, expected_secret: str, is_manager: bool):
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected_secret or not secrets.compare_digest(supplied, expected_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    message = update.get("message") or {}
    if is_manager:
        await handle_manager_message(token, message)
    else:
        await process_voice_message(token, message)
    return {"ok": True}

@app.post("/webhook/main")
async def main_webhook(request: Request):
    if not MAIN_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Main bot is not configured")
    return await handle_fixed_bot(request, MAIN_BOT_TOKEN, MAIN_WEBHOOK_SECRET, False)

@app.post("/webhook/manager")
async def manager_webhook(request: Request):
    if not MANAGER_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Manager bot is not configured")
    return await handle_fixed_bot(request, MANAGER_BOT_TOKEN, MANAGER_WEBHOOK_SECRET, True)

async def handle_manager_message(manager_token: str, message: dict):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_id = (message.get("from") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not user_id or not text:
        return

    # Only accept token submissions in private chats to avoid public token exposure.
    if chat.get("type") != "private":
        await send_text(manager_token, chat_id, "សូមប្រើ Bot Manager ក្នុង Private Chat ប៉ុណ្ណោះ។")
        return

    if text.startswith("/start") or text.startswith("/help"):
        await process_voice_message(manager_token, message, is_manager=True)
        return

    if text.startswith("/"):
        await send_text(manager_token, chat_id, "សូមផ្ញើ Bot Token តាមទម្រង់ដែលបានផ្ដល់ដោយ @BotFather ឬវាយ /start សម្រាប់ជំនួយ។")
        return

    candidate = text.split()[0]
    # Validate token with Telegram before storing it.
    try:
        bot_info = await tg_call(candidate, "getMe")
    except Exception:
        await send_text(manager_token, chat_id, "Token មិនត្រឹមត្រូវ ឬ Telegram មិនអាចផ្ទៀងផ្ទាត់បានទេ។ សូមពិនិត្យម្ដងទៀត។")
        return

    # Prevent replacing a bot already connected by a different account.
    bot_key = bot_key_for(candidate)
    try:
        existing = await find_connected_bot(bot_key)
        if existing and int(existing["owner_tg_id"]) != int(user_id):
            await send_text(manager_token, chat_id, "Bot នេះបានភ្ជាប់ជាមួយគណនី Telegram ផ្សេងរួចហើយ។")
            return

        hook_secret = secrets.token_urlsafe(32)
        record = {
            "bot_key": bot_key,
            "owner_tg_id": int(user_id),
            "bot_token_enc": encrypt_token(candidate),
            "bot_username": bot_info.get("username", ""),
            "webhook_secret_hash": secret_hash(hook_secret),
        }
        await save_connected_bot(record)
        await tg_call(
            candidate,
            "setWebhook",
            json_body={
                "url": f"{APP_BASE_URL}/webhook/{bot_key}",
                "secret_token": hook_secret,
                "allowed_updates": ["message"],
            },
        )
        await send_text(
            manager_token, chat_id,
            f"ភ្ជាប់បានជោគជ័យ! 🎉\nBot: @{bot_info.get('username', 'unknown')}\n"
            "ឥឡូវនេះ សាកផ្ញើអត្ថបទទៅ Bot របស់អ្នក ដើម្បីបង្កើតសំឡេង AI។\n"
            "កុំចែករំលែក Token របស់អ្នក។ ប្រសិនបើ Token លេចធ្លាយ សូមបង្កើត Token ថ្មីតាម @BotFather។"
        )
    except Exception:
        # Do not log the submitted token.
        await send_text(manager_token, chat_id, "ភ្ជាប់មិនបានទេ។ សូមពិនិត្យការកំណត់ Database, Encryption Key និង Webhook របស់ Vercel។")

@app.post("/webhook/{bot_key}")
async def user_bot_webhook(bot_key: str, request: Request):
    # Reserved route names are handled by the fixed routes above.
    if bot_key in {"main", "manager"}:
        raise HTTPException(status_code=404, detail="Not found")
    update = await request.json()
    try:
        record = await find_connected_bot(bot_key)
        if not record:
            raise HTTPException(status_code=404, detail="Bot not registered")
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(secret_hash(supplied), record["webhook_secret_hash"]):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        token = decrypt_token(record["bot_token_enc"])
        message = update.get("message") or {}
        await process_voice_message(token, message)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        # Do not expose credentials or internal exception details.
        raise HTTPException(status_code=500, detail="Webhook processing failed")
