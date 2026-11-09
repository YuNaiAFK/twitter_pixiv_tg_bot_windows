"""只在本機開啟 QR Code／兩步驟驗證登入頁，保存專用使用者工作階段。"""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import traceback

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

HTML = """<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>exviewer 專用帳號登入</title>
<style>body{font:18px system-ui;background:#f5f6fa;color:#18233b;max-width:540px;margin:48px auto;padding:24px}img{width:300px;max-width:100%;background:white}input,button{font:inherit;padding:12px;margin:8px 0}p{line-height:1.7}button{cursor:pointer}form{display:none}</style>
<h1>exviewer 專用帳號登入</h1><p>在 Telegram 開啟「設定 → 裝置 → 連結桌面裝置」，掃描下方 QR Code。</p>
<img id="qr" hidden><p id="state">正在連接 Telegram……</p>
<form id="form"><label>兩步驟驗證密碼<br><input id="password" type="password" autocomplete="off" required></label><br><button>登入</button></form>
<p>此頁只在本機開啟。密碼不會寫入日誌或設定檔。</p>
<script>const csrf='__CSRF__';async function refresh(){const s=await(await fetch('/status')).json();document.getElementById('state').textContent=s.message;const q=document.getElementById('qr');q.hidden=s.phase!=='qr';if(s.qr)q.src=s.qr;document.getElementById('form').style.display=s.phase==='password'?'block':'none';}setInterval(refresh,1500);refresh();document.getElementById('form').onsubmit=async e=>{e.preventDefault();const p=document.getElementById('password');const value=p.value;p.value='';await fetch('/password',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({password:value})});refresh();};</script></html>"""


async def wait_rate_limit(state, seconds):
    print(f"EXVIEWER_LOGIN_WAIT_SECONDS={seconds}", flush=True)
    state.clear()
    for remaining in range(max(1, int(seconds)) + 1, 0, -1):
        state.update(phase="waiting", message=f"Telegram 暫時限制登入，請等待 {remaining} 秒。此頁會自動繼續，請勿重複提交。")
        await asyncio.sleep(1)


async def retry_login_rpc(call, state):
    from telethon.errors import FloodWaitError
    while True:
        try:
            return await call()
        except FloodWaitError as exc:
            await wait_rate_limit(state, exc.seconds)


async def resolve_qr_response(client, qr, state):
    """QR 更新也可能直接回傳跨資料中心或登入完成，而非新權杖。"""
    from telethon import functions, types
    response = qr._resp
    while isinstance(response, types.auth.LoginTokenMigrateTo):
        await client._switch_dc(response.dc_id)
        token = response.token
        response = await retry_login_rpc(lambda: client(functions.auth.ImportLoginTokenRequest(token)), state)
    if isinstance(response, types.auth.LoginTokenSuccess):
        await client._on_login(response.authorization.user)
        return True
    if not isinstance(response, types.auth.LoginToken):
        raise RuntimeError("不支援的 QR 回應：" + type(response).__name__)
    qr._resp = response
    return False


async def enter_password(client, passwords, state):
    from telethon.errors import PasswordHashInvalidError, FloodWaitError
    state.clear()
    state.update(phase="password", message="請在此本機頁面輸入兩步驟驗證密碼。")
    while True:
        password = await passwords.get()
        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            state.update(phase="password", message="密碼不正確，請重新輸入。")
        except FloodWaitError as exc:
            password = None
            await wait_rate_limit(state, exc.seconds)
            state.update(phase="password", message="等待已結束，請重新輸入 Telegram 兩步驟驗證密碼。")
        finally:
            password = None


async def login(port=8876):
    load_dotenv(BASE_DIR.parent / ".env")
    load_dotenv(BASE_DIR / ".env")
    import qrcode
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError, FloodWaitError
    from telegram_documents import proxy_settings
    import ehentai

    api_id, api_hash = os.getenv("TELEGRAM_API_ID", ""), os.getenv("TELEGRAM_API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        raise ValueError("請先設定 TELEGRAM_API_ID 與 TELEGRAM_API_HASH")
    directory = BASE_DIR / ".mtproto"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    client = TelegramClient(str(directory / "exviewer-user"), int(api_id), api_hash,
        proxy=proxy_settings(ehentai.proxy_url()), request_retries=1, connection_retries=1,
        auto_reconnect=False, flood_sleep_threshold=0, device_model="EH exviewer helper")
    state = {"phase": "connecting", "message": "正在連接 Telegram……"}
    csrf = secrets.token_urlsafe(32)
    loop = asyncio.get_running_loop()
    passwords = asyncio.Queue(maxsize=1)
    origin = f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                return self.reply(403, b"Forbidden", "text/plain")
            if self.path == "/":
                return self.reply(200, HTML.replace("__CSRF__", csrf).encode(), "text/html; charset=utf-8")
            if self.path == "/status":
                return self.reply(200, json.dumps(state, ensure_ascii=False).encode(), "application/json")
            self.reply(404, b"Not found", "text/plain")

        def do_POST(self):
            if (self.path != "/password" or self.headers.get("Host") != f"127.0.0.1:{port}"
                    or self.headers.get("Origin") != origin or self.headers.get("X-CSRF-Token") != csrf):
                return self.reply(403, b"Forbidden", "text/plain")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096 or state["phase"] != "password":
                    raise ValueError()
                password = json.loads(self.rfile.read(size))["password"]
                if not isinstance(password, str) or not password:
                    raise ValueError()
                state.update(phase="checking", message="正在驗證，請稍候……")
                future = asyncio.run_coroutine_threadsafe(passwords.put(password), loop)
                future.result(timeout=2)
                self.reply(200, b"{}", "application/json")
            except Exception:
                self.reply(400, b"Invalid input", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"EXVIEWER_LOGIN_URL={origin}", flush=True)
    try:
        await client.connect()
        if not await retry_login_rpc(client.is_user_authorized, state):
            while True:
                try:
                    qr = await retry_login_rpc(client.qr_login, state)
                    print("EXVIEWER_QR_RESPONSE=" + type(qr._resp).__name__, flush=True)
                    if await resolve_qr_response(client, qr, state):
                        break
                    image = qrcode.make(qr.url)
                    stream = io.BytesIO()
                    image.save(stream, format="PNG")
                    state.update(phase="qr", message="請使用專用帳號掃描 QR Code；過期時會自動更新。",
                        qr="data:image/png;base64," + base64.b64encode(stream.getvalue()).decode())
                    await qr.wait()
                    break
                except asyncio.TimeoutError:
                    continue
                except FloodWaitError as exc:
                    await wait_rate_limit(state, exc.seconds)
                except SessionPasswordNeededError:
                    await enter_password(client, passwords, state)
                    break
        me = await retry_login_rpc(client.get_me, state)
        if not me or me.bot:
            raise ValueError("需要 Telegram 使用者帳號，不能使用 bot 身分")
        state.clear()
        state.update(phase="done", message="登入完成，可以關閉此頁面。")
        print("EXVIEWER_USER_LOGIN_SUCCESS", flush=True)
        await asyncio.sleep(15)
    except Exception as exc:
        state.clear()
        state.update(phase="error", message=f"登入失敗：{type(exc).__name__}，請重新啟動登入工具。")
        print("EXVIEWER_LOGIN_ERROR=" + type(exc).__name__, flush=True)
        if isinstance(exc, AttributeError):
            print("EXVIEWER_ATTRIBUTE=" + str(exc.name) + ";OBJECT_TYPE=" + type(exc.obj).__name__, flush=True)
        for frame in traceback.extract_tb(exc.__traceback__):
            # 只記錄位置，不包含變數、密碼、QR 權杖或請求內容。
            print(f"EXVIEWER_ERROR_LOCATION={Path(frame.filename).name}:{frame.lineno}:{frame.name}", flush=True)
        await asyncio.sleep(300)
        raise SystemExit(1)
    finally:
        await client.disconnect()
        for path in directory.glob("exviewer-user.session*"):
            path.chmod(0o600)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    asyncio.run(login(parser.parse_args().port))
