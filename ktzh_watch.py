"""
Следит за свободными местами в ОБЫЧНОМ КУПЕ на Талго Астана -> Актобе (bilet.railways.kz)
и шлёт уведомление в Telegram, когда места появились.
Купе для пассажиров с инвалидностью не учитывается.

Два режима:
  python ktzh_watch.py          - работает в цикле (для ПК / VPS)
  python ktzh_watch.py --once   - один проверочный запуск (для GitHub Actions по расписанию)

Настройки через переменные окружения (всё, кроме токена, необязательно):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  - обязательно
  DATES            - даты через запятую, ДД-ММ-ГГГГ (по умолчанию 26-12-2026,27-12-2026)
  STATE_FILE       - где хранить состояние (по умолчанию state.json рядом со скриптом)
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

# ---------- настройки ----------
DATES = [d.strip() for d in os.environ.get("DATES", "26-12-2026,27-12-2026,28-12-2026").split(",") if d.strip()]
FROM_STATION = "2708001"                # Астана
TO_STATION = "2704600"                  # Актобе-1
CAR_TYPE = "Купе"
ONLY_TALGO = True                       # брать только поезда с "Тальго" в названии
INTERVAL_SEC = 90                       # интервал опроса в циклическом режиме
HEARTBEAT_HOUR = 9                      # во сколько (по Астане) раз в сутки писать "жив"
STATE_FILE = Path(os.environ.get("STATE_FILE") or Path(__file__).with_name("state.json"))
# -------------------------------

ASTANA = timezone(timedelta(hours=5))
BASE = "https://bilet.railways.kz/sale/default"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
})


def search_url(date: str) -> str:
    q = {
        "route_search_form[departureStation]": FROM_STATION,
        "route_search_form[arrivalStation]": TO_STATION,
        "route_search_form[forwardDepartureDate]": date,
    }
    return f"{BASE}/route/search?{urlencode(q)}"


def car_url(train: str, departure: str, el_reg: str) -> str:
    p = "car_search_form"
    q = {
        f"{p}[departureStation]": FROM_STATION,
        f"{p}[arrivalStation]": TO_STATION,
        f"{p}[forwardDirection][departureTime]": departure,
        f"{p}[forwardDirection][fluentDeparture]": "",
        f"{p}[forwardDirection][train]": train,
        f"{p}[forwardDirection][isObligativeElReg]": el_reg,
        f"{p}[backwardDirection][departureTime]": "",
        f"{p}[backwardDirection][fluentDeparture]": "",
        f"{p}[backwardDirection][train]": "",
        f"{p}[backwardDirection][isObligativeElReg]": "",
    }
    return f"{BASE}/car/search?{urlencode(q)}"


def get(url: str, attempts: int = 3) -> str:
    """GET с повторами: сайт иногда отвечает ошибкой или тормозит."""
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1) + random.uniform(0, 1))
    raise last


def parse_search(html: str):
    """Поезда со страницы поиска: [{train, departure, el_reg, kupe_total}]."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.result-card[data-train]"):
        title = re.sub(r"\s+", " ", card.get_text(" "))
        if ONLY_TALGO and "альго" not in title:
            continue
        total = 0
        for row in card.select("tr"):
            t = row.select_one(".result-card__details-table-car-type")
            c = row.select_one(".result-card__details-table-seats-count")
            if t and c and t.get_text(strip=True) == CAR_TYPE:
                total = int(re.sub(r"\D", "", c.get_text()) or 0)
        out.append({
            "train": card["data-train"],
            "departure": card.get("data-departure-date", ""),
            "el_reg": card.get("data-is_obligative_el_reg", "0"),
            "kupe_total": total,
        })
    return out


def parse_cars(html: str):
    """Обычные (не для инвалидов) вагоны нужного типа: [(номер, мест, цена)]."""
    soup = BeautifulSoup(html, "html.parser")
    cars = []
    for t in soup.select("div.ticket[data-car-type]"):
        if t["data-car-type"] != CAR_TYPE:
            continue
        label = t.select_one(".ticket__type")
        if label and "инвалид" in label.get_text().lower():
            continue
        beds = t.select_one(".ticket__beds")
        seats = int(re.sub(r"\D", "", beds.get_text()) or 0) if beds else 0
        price = t.select_one(".ticket__price")
        price = re.sub(r"[\s ]+", " ", price.get_text()).strip() if price else ""
        cars.append((t.get("data-car-number", "?"), seats, price))
    return cars


def check_date(date: str):
    """Возвращает [(поезд, мест_в_обычных_купе, [(вагон, мест, цена), ...])]."""
    result = []
    for tr in parse_search(get(search_url(date))):
        if tr["kupe_total"] == 0:
            result.append((tr["train"], 0, []))
            continue
        time.sleep(random.uniform(1, 3))
        cars = parse_cars(get(car_url(tr["train"], tr["departure"], tr["el_reg"])))
        result.append((tr["train"], sum(c[1] for c in cars), cars))
    return result


def send_telegram(msg: str):
    if not TOKEN or not CHAT_ID:
        print("[нет Telegram-настроек] " + msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=20,
        ).raise_for_status()
    except Exception as e:
        print("Не удалось отправить в Telegram:", e)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def run_check(state: dict, error_threshold: int):
    """Одна проверка всех дат. Меняет state, шлёт уведомления."""
    summary = []
    try:
        for date in DATES:
            for train, seats, cars in check_date(date):
                key = f"{date}|{train}"
                if seats > state.get(key, 0):
                    wagons = "; ".join(f"вагон {n}: {s} мест, {p}" for n, s, p in cars)
                    send_telegram(
                        f"КУПЕ появилось!\nПоезд {train}, отправление {date}\n"
                        f"Свободно: {seats} ({wagons})\n{search_url(date)}\n"
                        f"На оплату брони обычно даётся 15 минут."
                    )
                print(time.strftime("%H:%M:%S"), date, train, "обычное купе:", seats)
                state[key] = seats
                summary.append(f"{date}: {seats}")
            time.sleep(random.uniform(2, 5))
        state["_errors"] = 0
        state["_warned"] = False
    except Exception as e:  # сеть, 403, смена вёрстки и т.п.
        state["_errors"] = state.get("_errors", 0) + 1
        print("Ошибка:", e)
        if state["_errors"] >= error_threshold and not state.get("_warned"):
            send_telegram(f"Скрипт {state['_errors']} раз подряд не может получить данные: {e}")
            state["_warned"] = True
        return

    # раз в сутки сообщаем, что скрипт жив
    now = datetime.now(ASTANA)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR and state.get("_heartbeat") != today:
        send_telegram("Скрипт работает. Обычное купе сейчас: " + (", ".join(summary) or "поезд не найден"))
        state["_heartbeat"] = today


def main():
    state = load_state()
    if "--once" in sys.argv:
        run_check(state, error_threshold=3)
        save_state(state)
        return
    send_telegram(f"Слежка за купе запущена: {', '.join(DATES)}, Астана → Актобе")
    while True:
        run_check(state, error_threshold=10)
        save_state(state)
        time.sleep(INTERVAL_SEC + random.uniform(-15, 15))


if __name__ == "__main__":
    main()
