from io import BytesIO

from openpyxl import load_workbook

from src.services.case_exporter import build_cases_excel


def test_build_cases_excel_contains_link_and_fields():
    case_list = (
        "A40-1/2023 | 15.03.2023 | Удовлетворено | Суд: АС города Москвы | "
        "Основание: применение статьи 61.2 | Ссылка: https://kad.arbitr.ru/Card/case-1"
    )
    payload = build_cases_excel(case_list)
    workbook = load_workbook(filename=BytesIO(payload))
    sheet = workbook.active

    assert sheet["A2"].value == "A40-1/2023"
    assert sheet["D2"].value == "АС города Москвы"
    assert sheet["E2"].value == "применение статьи 61.2"
    assert sheet["F2"].value == "https://kad.arbitr.ru/Card/case-1"
