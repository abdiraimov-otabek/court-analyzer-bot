from io import BytesIO

from openpyxl import load_workbook

from src.services.case_exporter import build_cases_excel


def test_build_cases_excel_contains_link_and_fields():
    case_list = (
        "A40-1/2023 | 15.03.2023 | Удовлетворено | Суд: АС города Москвы | "
        "Основание: применение статьи 61.2 | Ссылка: https://kad.arbitr.ru/Card/case-1 | "
        "Цитата: Суд установил основания для применения ст. 61.2 | "
        "Акт: Решение от 15.03.2023 | Тип акта: merits_act | PDF: verified | "
        "Проверка: verified | Анализ: quote-backed | Документы: https://kad.arbitr.ru/Document/Pdf/1"
    )
    payload = build_cases_excel(case_list)
    workbook = load_workbook(filename=BytesIO(payload))
    sheet = workbook.active

    assert sheet["A1"].value == "Номер дела"
    assert sheet["H1"].value == "Решающий акт"
    assert sheet["I1"].value == "Тип акта"
    assert sheet["J1"].value == "Статус PDF"
    assert sheet["K1"].value == "Статус проверки"
    assert sheet["A2"].value == "A40-1/2023"
    assert sheet["D2"].value == "АС города Москвы"
    assert sheet["E2"].value == "применение статьи 61.2"
    assert sheet["F2"].value == "https://kad.arbitr.ru/Card/case-1"
    assert sheet["H2"].value == "Решение от 15.03.2023"
    assert sheet["I2"].value == "merits_act"
    assert sheet["J2"].value == "verified"
    assert sheet["K2"].value == "verified"
