"""Разбор фразы мастера вида «Аня, пт 14:00, коррекция» в структуру визита."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вто": 1, "вторник": 1,
    "ср": 2, "сре": 2, "среда": 2, "среду": 2,
    "чт": 3, "чет": 3, "четверг": 3,
    "пт": 4, "пят": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "вос": 6, "воскресенье": 6,
}

RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
TIME_RE = re.compile(r"(?<![\d.])(?:в\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.])")
TIME_HOUR_RE = re.compile(r"(?<![\d.:])в\s+([01]?\d|2[0-3])(?![\d.:])")
NUM_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d.])")
WORD_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё\-]+")


@dataclass
class ParsedVisit:
    name: str
    starts_at: datetime  # наивное локальное время мастера
    service: str
    phone: str | None = None


class ParseError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _cut(text: str, match: re.Match) -> str:
    return text[: match.start()] + " " + text[match.end():]


def _resolve_date(text: str, today: date) -> tuple[date | None, str]:
    """Возвращает дату и текст без найденного фрагмента."""
    m = WORD_DATE_RE.search(text)
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else today.year
        d = _safe_date(year, month, day)
        if not m.group(3) and d < today:
            d = _safe_date(year + 1, month, day)
        return d, _cut(text, m)

    m = NUM_DATE_RE.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            year = year + 2000 if year < 100 else year
            d = _safe_date(year, month, day)
        else:
            d = _safe_date(today.year, month, day)
            if d < today:
                d = _safe_date(today.year + 1, month, day)
        return d, _cut(text, m)

    for m in WORD_RE.finditer(text):
        word = m.group(0).lower()
        if word in RELATIVE_DAYS:
            return today + timedelta(days=RELATIVE_DAYS[word]), _cut(text, m)
        if word in WEEKDAYS:
            delta = (WEEKDAYS[word] - today.weekday()) % 7
            return today + timedelta(days=delta), _cut(text, m)
    return None, text


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as e:
        raise ParseError(f"Не понимаю дату {day:02d}.{month:02d}") from e


def parse_visit(text: str, now: datetime) -> ParsedVisit:
    """Разбирает фразу мастера. `now` — текущее локальное время мастера (наивное)."""
    original = text.strip()
    if not original:
        raise ParseError("Пустое сообщение")
    text = original

    phone = None
    m = PHONE_RE.search(text)
    if m:
        phone = normalize_phone(m.group(0))
        text = _cut(text, m)

    # «14:00» однозначно время; «14.00» может быть датой, поэтому берём последнее совпадение.
    matches = list(TIME_RE.finditer(text))
    colon = [x for x in matches if ":" in x.group(0)]
    m = colon[0] if colon else (matches[-1] if matches else None)
    if m:
        t = time(int(m.group(1)), int(m.group(2)))
        text = _cut(text, m)
    else:
        m = TIME_HOUR_RE.search(text)
        if not m:
            raise ParseError("Не вижу время визита — напишите, например, 14:00")
        t = time(int(m.group(1)), 0)
        text = _cut(text, m)

    d, text = _resolve_date(text, now.date())
    if d is None:
        raise ParseError("Не вижу день визита — напишите, например, «пт», «завтра» или 25.10")

    starts_at = datetime.combine(d, t)
    # «пт 14:00», сказанное в пятницу в 15:00, означает следующую пятницу.
    if starts_at <= now and re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", original.lower()):
        starts_at += timedelta(days=7)
    if starts_at <= now:
        raise ParseError("Это время уже прошло")

    parts = [p.strip(" ,;-—") for p in re.split(r"[,;]", text)]
    parts = [p for p in parts if p]
    if not parts:
        raise ParseError("Не вижу имя клиентки — начните сообщение с имени")

    if len(parts) >= 2:
        name, service = parts[0], " ".join(parts[1:])
    else:
        words = parts[0].split()
        name, service = words[0], " ".join(words[1:])

    name = re.sub(r"\s+", " ", name).strip()
    service = re.sub(r"\s+", " ", service).strip() or "визит"
    if not WORD_RE.search(name):
        raise ParseError("Не вижу имя клиентки — начните сообщение с имени")
    return ParsedVisit(name=name[:1].upper() + name[1:], starts_at=starts_at, service=service, phone=phone)
