#!/usr/bin/env python3
"""
PRitsme Telegram Ecosystem Bot (Hermes Engine v3.8)
- Token: 8732358914:AAH5Z79O0uHIz39I4F8UMRVMzJt2aVSEHdw (@masiksssgegai_bot)
- Single-instance process lock
- 10-minute rate limit on map generation/requests
- Multi-group broadcaster for Airdrops (every 2h) & Captcha (19:00 MSK)
- Integrated with @ndjdhfbdbs_bot, @PBMap_bot, and group -1003949456661
- Full offline calculations and region selector
"""

import os
import sys
import json
import time
import math
import sqlite3
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# Force UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Setup Proxy
PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10809")
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    proxy_handler = urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(opener)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN = "8732358914:AAH5Z79O0uHIz39I4F8UMRVMzJt2aVSEHdw"
ADMIN_IDS = [8871602493, 8262143167]
DEFAULT_GROUP_ID = -1003949456661  # Союз Е-Р-О-П
DB_FILE = os.path.join(BASE_DIR, "pritsme_data.db")
MAP_FILE = os.path.join(BASE_DIR, "map_ndjdhfbdbs.png")
if not os.path.exists(MAP_FILE):
    MAP_FILE = os.path.join(BASE_DIR, "map_pbmap.png")

# 10-minute rate limit storage
MAP_COOLDOWN_SECONDS = 600  # 10 minutes
last_map_requests = {}      # {chat_or_user_id: timestamp}

REGIONS_LIST = [
    "Азия", "Антарктида", "Африка", "Евразия", "Европа (ЕС)",
    "Океания (Австралия)", "Сааремаа", "Северная Америка (СА)", "Южная Америка (ЮА)",
    "Бангладеш", "Бразилия", "Израиль", "Индия", "Казахстан",
    "Канада", "Китай", "Конго (ДРК)", "Монголия", "Россия (РФ)",
    "США", "Того", "Украина", "Шри-Ланка"
]

def enforce_single_instance(script_name="PRitsme_bot.py"):
    my_pid = os.getpid()
    pid_file = os.path.join(BASE_DIR, f"{script_name}.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                old_pid = int(pf.read().strip())
            if old_pid != my_pid and old_pid > 0:
                import subprocess
                subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
                time.sleep(0.5)
        except Exception:
            pass
    try:
        with open(pid_file, "w", encoding="utf-8") as pf:
            pf.write(str(my_pid))
    except Exception:
        pass

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_groups (
        chat_id INTEGER PRIMARY KEY,
        chat_title TEXT,
        added_at INTEGER
    )
    """)
    # Ensure default group is registered
    c.execute("""
    INSERT OR IGNORE INTO broadcast_groups (chat_id, chat_title, added_at)
    VALUES (?, ?, ?)
    """, (DEFAULT_GROUP_ID, "Союз Е-Р-О-П", int(time.time())))
    conn.commit()
    conn.close()

def db_get_groups():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        c = conn.cursor()
        c.execute("SELECT chat_id, chat_title FROM broadcast_groups")
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return [(DEFAULT_GROUP_ID, "Союз Е-Р-О-П")]

def db_add_group(chat_id: int, title: str):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO broadcast_groups (chat_id, chat_title, added_at) VALUES (?, ?, ?)",
                  (chat_id, title, int(time.time())))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def db_del_group(chat_id: int):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        c = conn.cursor()
        c.execute("DELETE FROM broadcast_groups WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

# Telegram API wrapper
def tg_api(method: str, params: dict = None) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    headers = {"Content-Type": "application/json"}
    data = json.dumps(params).encode("utf-8") if params else None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
            return json.loads(err_body)
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def send_tg_message(chat_id, text: str, reply_to_message_id=None, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)

def send_tg_photo(chat_id, photo_path: str, caption: str = None, reply_markup=None, reply_to_message_id=None):
    if not os.path.exists(photo_path):
        return {"ok": False, "error": "file_not_found"}
    boundary = "----WebKitFormBoundaryPRitsmeMap" + hex(int(time.time() * 1000))[2:]
    parts = []

    def add_field(name, val):
        parts.append(f"--{boundary}\r\n".encode('utf-8'))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8'))
        parts.append(f"{val}\r\n".encode('utf-8'))

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption[:1024])
        add_field("parse_mode", "HTML")
    if reply_to_message_id:
        add_field("reply_to_message_id", reply_to_message_id)
    if reply_markup:
        add_field("reply_markup", json.dumps(reply_markup))

    with open(photo_path, "rb") as f:
        fb = f.read()

    fname = os.path.basename(photo_path)
    parts.append(f"--{boundary}\r\n".encode('utf-8'))
    parts.append(f'Content-Disposition: form-data; name="photo"; filename="{fname}"\r\n'.encode('utf-8'))
    parts.append(b"Content-Type: image/png\r\n\r\n")
    parts.append(fb)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode('utf-8'))

    body = b"".join(parts)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body))
    }
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"send_tg_photo error: {e}")
        return {"ok": False, "error": str(e)}

# Local Offline Schedule Calculation (MSK Time UTC+3)
def get_msk_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def get_next_events():
    now = get_msk_now()
    cur_sec = now.hour * 3600 + now.minute * 60 + now.second

    # Airdrop schedule: every 2 hours at minute 00
    airdrop_hours = [1, 3, 5, 7, 9, 11, 13, 15, 17, 20, 21, 23]
    next_drop_sec = None
    for h in airdrop_hours:
        s = h * 3600
        if s > cur_sec:
            next_drop_sec = s - cur_sec
            break
    if next_drop_sec is None:
        # Wrap to tomorrow's 01:00
        next_drop_sec = (24 * 3600 - cur_sec) + (1 * 3600)

    # Captcha schedule: daily at 19:00:00 MSK
    captcha_target_sec = 19 * 3600
    if captcha_target_sec > cur_sec:
        next_captcha_sec = captcha_target_sec - cur_sec
    else:
        next_captcha_sec = (24 * 3600 - cur_sec) + captcha_target_sec

    def fmt_dur(total_s):
        h = int(total_s // 3600)
        m = int((total_s % 3600) // 60)
        s = int(total_s % 60)
        if h > 0:
            return f"{h} ч {m:02d} мин {s:02d} сек"
        return f"{m} мин {s:02d} сек"

    nearest_name = "📦 Аирдроп-сундук" if next_drop_sec < next_captcha_sec else "⚡ Вечерняя Капча"
    nearest_time = fmt_dur(min(next_drop_sec, next_captcha_sec))

    text = (
        f"⏳ <b>До ближайшего события:</b> <b>{nearest_time}</b> ({nearest_name})\n\n"
        f"📦 <b>До следующего Аирдропа:</b> {fmt_dur(next_drop_sec)}\n"
        f"⚡ <b>До Капчи (19:00:00 МСК):</b> {fmt_dur(next_captcha_sec)}\n"
        f"🕒 <i>Текущее время по МСК: {now.strftime('%H:%M:%S')}</i>"
    )
    return text, next_drop_sec, next_captcha_sec

def check_map_rate_limit(chat_id, user_id):
    now = time.time()
    # Key by user_id for individual cooldown, or chat_id for groups
    key = f"{chat_id}_{user_id}"
    last_t = last_map_requests.get(key, 0)
    elapsed = now - last_t
    if elapsed < MAP_COOLDOWN_SECONDS:
        rem = MAP_COOLDOWN_SECONDS - elapsed
        m = int(rem // 60)
        s = int(rem % 60)
        return False, f"{m:02d}:{s:02d}"
    last_map_requests[key] = now
    return True, "00:00"

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🗺️ Получить карту игры"}, {"text": "📅 Расписание ивентов"}],
            [{"text": "🌍 Регионы карты"}, {"text": "📱 Приложение PRitsme"}],
            [{"text": "👥 Игровой чат Союз Е-Р-О-П"}]
        ],
        "resize_keyboard": True
    }

def get_inline_map_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🎮 Игровой бот (@ndjdhfbdbs_bot)", "url": "https://t.me/ndjdhfbdbs_bot"},
                {"text": "🗺️ PBMap (@PBMap_bot)", "url": "https://t.me/PBMap_bot"}
            ],
            [
                {"text": "👥 Группа Союз Е-Р-О-П", "url": "https://t.me/c/3949456661/1"},
                {"text": "📱 GitHub Релиз PRitsme", "url": "https://github.com/ivanmaspa/PRitsme/releases"}
            ]
        ]
    }

# Background Broadcaster for Groups
def run_broadcaster():
    print("[⏰] Фоновый планировщик рассылки запущен (Аирдропы каждые 2ч, Капча 19:00 МСК).")
    alerted_drops = set()
    alerted_captcha = set()

    while True:
        try:
            now = get_msk_now()
            today_str = now.strftime('%Y-%m-%d')
            h, m, s = now.hour, now.minute, now.second

            # 1. Airdrops: alert at (H-1):55 (5 minutes before drop)
            airdrop_hours = [1, 3, 5, 7, 9, 11, 13, 15, 17, 20, 21, 23]
            for ah in airdrop_hours:
                # 5 minutes before drop
                pre_h = (ah - 1) % 24
                if h == pre_h and m == 55 and s < 15:
                    key = f"drop_5m_{today_str}_{ah}"
                    if key not in alerted_drops:
                        alerted_drops.add(key)
                        groups = db_get_groups()
                        msg = (
                            f"🚨 <b>Внимание игрокам Pixel Royale!</b>\n\n"
                            f"📦 До сброса Аирдроп-сундука осталось <b>5 минут</b> (Старт ровно в <b>{ah:02d}:00 МСК</b>)!\n\n"
                            f"📍 Готовьтесь к высадке и сбору лута!"
                        )
                        for gid, gtitle in groups:
                            send_tg_message(gid, msg, reply_markup=get_inline_map_keyboard())

            # 2. Captcha: 19:00 MSK daily. Alert at 18:55 (5 min) and 18:59 (1 min)
            if h == 18 and m == 55 and s < 15:
                key = f"cap_5m_{today_str}"
                if key not in alerted_captcha:
                    alerted_captcha.add(key)
                    groups = db_get_groups()
                    msg = (
                        "⚡ <b>Внимание! Вечерняя Капча стартует через 5 минут (19:00:00 МСК)!</b>\n\n"
                        "🔑 Проверьте готовность бота и таймеры в приложении PRitsme!"
                    )
                    for gid, gtitle in groups:
                        send_tg_message(gid, msg, reply_markup=get_inline_map_keyboard())

            if h == 18 and m == 59 and s < 15:
                key = f"cap_1m_{today_str}"
                if key not in alerted_captcha:
                    alerted_captcha.add(key)
                    groups = db_get_groups()
                    msg = "🚨🚨🚨 <b>КАПЧА ЧЕРЕЗ 60 СЕКУНД! (19:00:00 МСК)</b>\nОткрывайте игровой чат!"
                    for gid, gtitle in groups:
                        send_tg_message(gid, msg, reply_markup=get_inline_map_keyboard())

            # Cleanup old keys at midnight
            if h == 0 and m == 5:
                alerted_drops.clear()
                alerted_captcha.clear()

        except Exception as e:
            print(f"[!] Broadcaster error: {e}")
        time.sleep(5)

def main():
    enforce_single_instance("PRitsme_bot.py")
    init_db()

    print("=" * 65)
    print("🚀 PRitsme Telegram Bot & Ecosystem Gateway (v3.8)")
    print("Token:", TOKEN[:10] + "...")
    print("Default Group: Союз Е-Р-О-П (-1003949456661)")
    print("Rate Limit: 10 minutes per map generation")
    print("=" * 65)

    threading.Thread(target=run_broadcaster, daemon=True).start()

    offset = 0
    while True:
        try:
            updates = tg_api("getUpdates", {"offset": offset, "timeout": 25, "limit": 50})
            if not updates.get("ok"):
                time.sleep(3)
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                chat_type = chat.get("type", "private")
                chat_title = chat.get("title", "")
                from_u = msg.get("from", {})
                user_id = from_u.get("id")
                username = from_u.get("username", "")
                user_name = from_u.get("first_name", "User")
                text = (msg.get("text") or msg.get("caption") or "").strip()
                msg_id = msg.get("message_id")

                if not text:
                    continue

                low_t = text.lower()

                # --- 1. /start ---
                if low_t.startswith("/start"):
                    welcome = (
                        f"👋 <b>Привет, {user_name}! Добро пожаловать в экосистему PRitsme!</b>\n\n"
                        f"🎮 <b>Pixel Royale Reminders & Map Center</b>\n"
                        f"• Свежая карта игры (синхронизирована с @ndjdhfbdbs_bot и @PBMap_bot)\n"
                        f"• Расписание и локальные таймеры аирдропов и капчи\n"
                        f"• Оповещения в чатах и группах (включая «Союз Е-Р-О-П»)\n"
                        f"• Автономное мобильное приложение PRitsme (без необходимости HTTPS)\n\n"
                        f"<i>Нажмите кнопку внизу или введите <b>/map</b> для получения карты!</i>"
                    )
                    send_tg_message(chat_id, welcome, reply_to_message_id=msg_id, reply_markup=get_main_keyboard())
                    continue

                # --- 2. Map Request (/map, карта, карты) with 10-minute rate limit ---
                is_map_query = (
                    low_t in ["/map", "карта", "карту", "карты", "🗺️ получить карту игры", "дай карту", "покажи карту", "скинь карту"]
                    or low_t.startswith("/map")
                    or ("карт" in low_t and len(low_t) <= 25 and not any(w in low_t for w in ["что такое", "почему", "зачем", "банков"]))
                )

                if is_map_query:
                    allowed, rem_time = check_map_rate_limit(chat_id, user_id)
                    if not allowed and user_id not in ADMIN_IDS:
                        limit_msg = (
                            f"⏳ <b>Лимит запросов карты!</b>\n\n"
                            f"Генерация и отправка актуальной карты ограничена <b>1 разом в 10 минут</b>, чтобы не перегружать сервер и чат.\n\n"
                            f"🕒 До следующего запроса карты осталось: <b>{rem_time}</b>.\n"
                            f"📱 В мобильном приложении PRitsme карта доступна локально в любое время!"
                        )
                        send_tg_message(chat_id, limit_msg, reply_to_message_id=msg_id)
                        continue

                    # Send map photo
                    map_caption = (
                        "🗺️ <b>Актуальная карта Pixel Royale (PRitsme)</b>\n\n"
                        "📍 <b>Ключевые точки:</b>\n"
                        "• <b>Вышка & Ангары:</b> Основной сектор сброса аирдроп-бомб\n"
                        "• <b>Капча:</b> Ежедневно в <b>19:00:00 (МСК)</b>\n\n"
                        f"{get_next_events()[0]}\n\n"
                        "⏱ <i>Следующий запрос карты доступен через 10 минут.</i>"
                    )
                    if os.path.exists(MAP_FILE):
                        res_p = send_tg_photo(chat_id, MAP_FILE, caption=map_caption,
                                              reply_markup=get_inline_map_keyboard(),
                                              reply_to_message_id=msg_id)
                        if not res_p.get("ok"):
                            # Retry without reply_to_message_id
                            send_tg_photo(chat_id, MAP_FILE, caption=map_caption,
                                          reply_markup=get_inline_map_keyboard())
                    else:
                        send_tg_message(chat_id, map_caption, reply_to_message_id=msg_id,
                                        reply_markup=get_inline_map_keyboard())
                    continue

                # --- 3. Events Schedule ---
                if low_t in ["/events", "/schedule", "📅 расписание ивентов", "расписание", "ивенты", "таймер", "таймеры"]:
                    events_info, _, _ = get_next_events()
                    sched_msg = (
                        f"📅 <b>Расписание и таймеры событий Pixel Royale (МСК):</b>\n\n"
                        f"{events_info}\n\n"
                        f"📦 <b>Сетка Аирдропов (каждые 2 часа):</b>\n"
                        f"• 01:00 • 03:00 • 05:00 • 07:00\n"
                        f"• 09:00 • 11:00 • 13:00 • 15:00\n"
                        f"• 17:00 • 20:00 • 21:00 • 23:00\n\n"
                        f"⚡ <b>Капча:</b> Ежедневно ровно в <b>19:00:00 МСК</b>\n\n"
                        f"🔔 <i>Оповещения в чаты приходят за 5 минут до каждого ивента!</i>"
                    )
                    send_tg_message(chat_id, sched_msg, reply_to_message_id=msg_id, reply_markup=get_main_keyboard())
                    continue

                # --- 4. Regions List (from @ndjdhfbdbs_bot) ---
                if low_t in ["/regions", "🌍 регионы карты", "регионы", "страны"]:
                    reg_text = "🌍 <b>Доступные регионы карты Pixel Royale:</b>\n\n"
                    for r in REGIONS_LIST:
                        reg_text += f" • {r}\n"
                    reg_text += "\n<i>Напишите название региона, чтобы получить тактическую подсказку!</i>"
                    send_tg_message(chat_id, reg_text, reply_to_message_id=msg_id)
                    continue

                # Check if user mentioned a specific region
                matched_reg = None
                for r in REGIONS_LIST:
                    if r.lower() in low_t:
                        matched_reg = r
                        break
                if matched_reg:
                    reg_info = (
                        f"📍 <b>Тактический сектор: {matched_reg}</b>\n\n"
                        f"Игровая зона активна на глобальной карте. Следите за сбросом сундуков и движением сужающейся зоны в этом квадрате!\n\n"
                        f"Для получения полной карты введите <b>/map</b>."
                    )
                    send_tg_message(chat_id, reg_info, reply_to_message_id=msg_id)
                    continue

                # --- 5. Application Link ---
                if low_t in ["/app", "📱 приложение pritsme", "приложение", "скачать"]:
                    app_msg = (
                        "📱 <b>Автономное мобильное приложение PRitsme:</b>\n\n"
                        "✨ <b>Особенности:</b>\n"
                        "• <b>100% Offline / Без HTTPS:</b> Все таймеры, звуки и вибросигналы работают прямо на устройстве.\n"
                        "• <b>Интерактивная карта:</b> Масштабирование, секторы и лимит обновления 10 минут.\n"
                        "• <b>Bento Grid дизайн:</b> Тёмная стильная тема в стиле YouTube и TikTok.\n"
                        "• <b>Прямой переход</b> в игровой бот @ndjdhfbdbs_bot и чат «Союз Е-Р-О-П».\n\n"
                        "📥 <b>Скачать APK из GitHub:</b>\n"
                        "https://github.com/ivanmaspa/PRitsme/releases"
                    )
                    send_tg_message(chat_id, app_msg, reply_to_message_id=msg_id, reply_markup=get_inline_map_keyboard())
                    continue

                # --- 6. Group link ---
                if low_t in ["👥 игровой чат союз е-р-о-п", "чат", "группа"]:
                    grp_msg = (
                        "👥 <b>Игровой чат «Союз Е-Р-О-П»:</b>\n\n"
                        "Чат для координации, поиска тимейтов и обмена информацией по карте и капче.\n\n"
                        "🔗 <b>Вступить в группу:</b>\n"
                        "https://t.me/c/3949456661/1\n"
                        "🤖 <b>Игровой бот:</b> @ndjdhfbdbs_bot"
                    )
                    send_tg_message(chat_id, grp_msg, reply_to_message_id=msg_id, reply_markup=get_inline_map_keyboard())
                    continue

                # --- 7. Admin Commands (/addgroup, /delgroup, /groups) ---
                if low_t.startswith("/addgroup"):
                    if user_id in ADMIN_IDS:
                        parts = low_t.split()
                        target_id = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else chat_id
                        g_name = chat_title if target_id == chat_id else f"Группа {target_id}"
                        db_add_group(target_id, g_name)
                        send_tg_message(chat_id, f"✅ Группа <b>{g_name}</b> (<code>{target_id}</code>) добавлена в рассылку ивентов!")
                    continue

                if low_t.startswith("/delgroup"):
                    if user_id in ADMIN_IDS:
                        parts = low_t.split()
                        target_id = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else chat_id
                        db_del_group(target_id)
                        send_tg_message(chat_id, f"❌ Группа <code>{target_id}</code> удалена из рассылки.")
                    continue

                if low_t in ["/groups", "/listgroups"]:
                    if user_id in ADMIN_IDS:
                        grps = db_get_groups()
                        lines = [f"• <b>{g[1]}</b> (<code>{g[0]}</code>)" for g in grps]
                        send_tg_message(chat_id, "📢 <b>Подключенные группы для рассылки:</b>\n\n" + "\n".join(lines))
                    continue

        except KeyboardInterrupt:
            print("\n[+] Остановка бота пользователем.")
            break
        except Exception as e:
            print(f"[!] Polling error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
