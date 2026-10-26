from datetime import datetime

import pytest

from korrekciya.parser import ParseError, parse_visit

# Четверг, 22 октября 2026, 12:00
NOW = datetime(2026, 10, 22, 12, 0)


def test_basic_weekday_phrase():
    v = parse_visit("Аня, пт 14:00, коррекция", NOW)
    assert v.name == "Аня"
    assert v.starts_at == datetime(2026, 10, 23, 14, 0)
    assert v.service == "коррекция"
    assert v.phone is None


def test_without_commas_and_hour_only():
    v = parse_visit("аня завтра в 15 брови", NOW)
    assert v.name == "Аня"
    assert v.starts_at == datetime(2026, 10, 23, 15, 0)
    assert v.service == "брови"


def test_numeric_date_and_dot_time():
    v = parse_visit("Маша 12.11 14.30 наращивание 2D", NOW)
    assert v.starts_at == datetime(2026, 11, 12, 14, 30)
    assert v.name == "Маша"
    assert v.service == "наращивание 2D"


def test_word_date():
    v = parse_visit("Оля, 5 ноября 11:00, ламинирование", NOW)
    assert v.starts_at == datetime(2026, 11, 5, 11, 0)


def test_past_date_rolls_to_next_year():
    v = parse_visit("Оля 01.02 10:00", NOW)
    assert v.starts_at == datetime(2027, 2, 1, 10, 0)
    assert v.service == "визит"


def test_same_weekday_already_passed_means_next_week():
    v = parse_visit("Аня чт 10:00", NOW)
    assert v.starts_at == datetime(2026, 10, 29, 10, 0)


def test_same_weekday_later_today():
    v = parse_visit("Аня чт 18:00", NOW)
    assert v.starts_at == datetime(2026, 10, 22, 18, 0)


def test_phone_extracted():
    v = parse_visit("Аня +7 (912) 345-67-89, пт 14:00, коррекция", NOW)
    assert v.phone == "+79123456789"
    assert v.name == "Аня"


def test_phone_with_8():
    v = parse_visit("Аня 89123456789 пт 14:00", NOW)
    assert v.phone == "+79123456789"


def test_full_name():
    v = parse_visit("Анна Петрова, завтра 16:00, коррекция", NOW)
    assert v.name == "Анна Петрова"


@pytest.mark.parametrize(
    "text",
    ["", "Аня коррекция", "Аня 14:00", "сегодня 10:00 Аня", "пт 14:00"],
)
def test_errors(text):
    with pytest.raises(ParseError):
        parse_visit(text, NOW)
