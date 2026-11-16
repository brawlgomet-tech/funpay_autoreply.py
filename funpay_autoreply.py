#!/usr/bin/env python3
"""
Автоответчик FunPay: отвечает людям, которые пишут вам в чат с вопросом.
Текст ответа подбирается по ключевым словам (replies.json).

Переменные окружения:
  FUNPAY_GOLDEN_KEY   (обязательно) cookie golden_key
  FUNPAY_PHPSESSID    (необязательно)
  FUNPAY_USER_AGENT   (необязательно)
  FUNPAY_REPLIES      путь к файлу правил (по умолчанию replies.json рядом со скриптом)
  FUNPAY_STATE        путь к файлу состояния (по умолчанию state.json рядом со скриптом)

Флаги:
  --once      один проход и выход (для GitHub Actions). Без флага работает в цикле.
  --dry-run   ничего не отправлять, а писать в консоль, что было бы отправлено

Зависимости: pip install requests beautifulsoup4
"""
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://funpay.com"
HERE = Path(__file__).resolve().parent
REPLIES_PATH = Path(os.environ.get("FUNPAY_REPLIES") or HERE / "replies.json")
STATE_PATH = Path(os.environ.get("FUNPAY_STATE") or HERE / "state.json")
UA = os.environ.get("FUNPAY_USER_AGENT") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
ONCE = "--once" in sys.argv
DRY_RUN = "--dry-run" in sys.argv


def log(*args):
    # Тексты сообщений и имена людей в лог НЕ пишем (логи Actions в публичном репо видны всем)
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, flush=True)


# ---------- сессия FunPay ----------

def make_session() -> requests.Session:
    key = os.environ.get("FUNPAY_GOLDEN_KEY", "").strip()
    if not key:
        sys.exit("Не задана переменная FUNPAY_GOLDEN_KEY")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
    s.cookies.set("golden_key", key, domain="funpay.com")
    s.cookies.set("cookie_prefs", "1", domain="funpay.com")
    sid = os.environ.get("FUNPAY_PHPSESSID", "").strip()
    if sid:
        s.cookies.set("PHPSESSID", sid, domain="funpay.com")
    return s


def get_app_data(s: requests.Session) -> dict:
    r = s.get(BASE + "/", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Главная вернула HTTP {r.status_code}")
    m = re.search(r'data-app-data="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Не нашёл app-data (golden_key неверный или защитная страница)")
    data = json.loads(html.unescape(m.group(1)))
    if not (data.get("userId") or data.get("user-id")):
        raise RuntimeError("Не авторизован: проверьте golden_key")
    return data


# ---------- правила ----------

def load_rules(previous=None):
    try:
        cfg = json.loads(REPLIES_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"Не смог прочитать {REPLIES_PATH.name}: {e}")
        if previous is not None:
            return previous
        sys.exit("Нет рабочего replies.json")
    cfg.setdefault("rules", [])
    cfg.setdefault("poll_seconds", 30)
    cfg.setdefault("cooldown_hours", 12)
    cfg.setdefault("default_message", None)
    cfg.setdefault("ignore_if_contains", [])
    return cfg


def match_rule(cfg: dict, text: str):
    t = text.lower()
    for rule in cfg["rules"]:
        if any(k.lower() in t for k in rule.get("match", [])):
            return rule.get("name", "?"), rule["message"]
    return None, None


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ---------- чаты ----------

def fetch_chats(s: requests.Session) -> list:
    r = s.get(f"{BASE}/chat/", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    chats = []
    for el in soup.select("a.contact-item"):
        node = el.get("data-id")
        if not node:
            continue
        prev = el.select_one(".contact-item-message")
        name = el.select_one(".media-user-name")
        preview = prev.get_text(" ", strip=True) if prev else ""
        chats.append({
            "node": str(node),
            "msg_id": str(el.get("data-node-msg") or preview),
            "unread": "unread" in (el.get("class") or []),
            "preview": preview,
            "name": name.get_text(strip=True) if name else "",
        })
    return chats


def chat_text(s: requests.Session, node: str) -> str:
    """Последние сообщения чата - запасной источник слов для подбора товара."""
    try:
        r = s.get(f"{BASE}/chat/", params={"node": node}, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        msgs = [e.get_text(" ", strip=True) for e in soup.select(".chat-msg-text")]
        if msgs:
            return " ".join(msgs[-5:])
        chat = soup.select_one(".chat")
        return chat.get_text(" ", strip=True)[-1500:] if chat else ""
    except Exception as e:  # noqa: BLE001
        log(f"[{node}] не смог открыть чат: {e}")
        return ""


def send_message(s: requests.Session, csrf: str, node: str, text: str) -> bool:
    objects = [{"type": "chat_node", "id": node, "tag": "00000000",
                "data": {"node": node, "last_message": -1, "content": ""}}]
    request = {"action": "chat_message",
               "data": {"node": node, "last_message": -1, "content": text}}
    r = s.post(
        f"{BASE}/runner/",
        data={"objects": json.dumps(objects), "request": json.dumps(request),
              "csrf_token": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE,
                 "Referer": f"{BASE}/chat/?node={node}"},
        timeout=30,
    )
    if r.status_code != 200:
        log(f"[{node}] отправка: HTTP {r.status_code}")
        return False
    try:
        err = (r.json().get("response") or {}).get("error")
    except ValueError:
        log(f"[{node}] отправка: не-JSON ответ")
        return False
    if err:
        log(f"[{node}] отправка: FunPay вернул ошибку: {err!r}")
        return False
    return True


# ---------- состояние ----------

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8")), False
        except Exception:  # noqa: BLE001
            pass
    return {"chats": {}}, True


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


# ---------- один проход ----------

def run_pass(s, csrf, cfg, state, first_run) -> None:
    chats = fetch_chats(s)
    known = state["chats"]

    if first_run:
        for c in chats:
            known[c["node"]] = {"last_msg": c["msg_id"], "replied_at": 0}
        log(f"Первый запуск: запомнил {len(chats)} чатов, на старые сообщения отвечать не буду.")
        return

    now = time.time()
    for c in chats:
        st = known.setdefault(c["node"], {"last_msg": "", "replied_at": 0})
        if c["msg_id"] == st["last_msg"]:
            continue  # ничего нового

        def remember():
            st["last_msg"] = c["msg_id"]

        if not c["unread"]:
            remember()  # вы уже прочитали/ответили сами
            continue
        low = c["preview"].lower()
        if any(p.lower() in low for p in cfg["ignore_if_contains"]):
            remember()  # служебные сообщения FunPay (заказ, отзыв и т.п.)
            continue
        if now - st.get("replied_at", 0) < cfg["cooldown_hours"] * 3600:
            remember()  # недавно уже отвечали этому человеку
            continue

        name, template = match_rule(cfg, c["preview"])
        if not template:
            name, template = match_rule(cfg, chat_text(s, c["node"]))
        if not template and cfg.get("default_message"):
            name, template = "по умолчанию", cfg["default_message"]
        if not template:
            remember()
            continue

        text = template.format_map(_SafeDict(buyer=c["name"]))
        if DRY_RUN:
            log(f"[DRY-RUN] чат {c['node']} -> правило «{name}»:\n{text}")
            remember()
            continue
        if send_message(s, csrf, c["node"], text):
            log(f"[{c['node']}] отправлено по правилу «{name}»")
            st["replied_at"] = now
            # перечитываем список, чтобы запомнить id нашего же сообщения и не ответить себе
            after = {x["node"]: x for x in fetch_chats(s)}
            st["last_msg"] = after.get(c["node"], c)["msg_id"]
            time.sleep(2)
        # при неудаче last_msg не трогаем - попробуем в следующий проход


def main():
    s = make_session()
    app = get_app_data(s)
    csrf = app.get("csrf-token", "")
    csrf_time = time.time()
    log("Авторизован" + (" [DRY-RUN]" if DRY_RUN else ""))

    state, first_run = load_state()
    cfg = load_rules()

    while True:
        try:
            cfg = load_rules(cfg)
            if time.time() - csrf_time > 1800:
                csrf = get_app_data(s).get("csrf-token", csrf)
                csrf_time = time.time()
            run_pass(s, csrf, cfg, state, first_run)
            first_run = False
            save_state(state)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка прохода: {e}")
            if ONCE:
                sys.exit(1)
            time.sleep(60)
        if ONCE:
            return
        time.sleep(max(10, int(cfg.get("poll_seconds", 30))))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
