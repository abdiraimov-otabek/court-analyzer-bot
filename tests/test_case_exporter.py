import datetime
from io import BytesIO

from openpyxl import load_workbook

from src.domain.entities import CaseDecision, CaseOutcome
from src.services.case_exporter import build_cases_excel


def test_build_cases_excel_contains_link_and_fields():
    decision = CaseDecision(
        case_number="A40-1/2023",
        decision_date=datetime.date(2023, 3, 15),
        outcome=CaseOutcome.SATISFIED,
        reasons=("оценка обстоятельств дела",),
        raw_number="123",
        raw_date=datetime.date(2023, 3, 15),
        raw_case_number="A40-1/2023",
        raw_place="АС города Москвы",
        raw_judge="Иванов И.И.",
        raw_url="https://kad.arbitr.ru/Card/case-1",
        raw_article="статья 61.2",
        raw_text="Полный текст решения...",
    )
    
    payload = build_cases_excel([decision])
    workbook = load_workbook(filename=BytesIO(payload))
    sheet = workbook.active

    assert sheet["A1"].value == "Номер"
    assert sheet["B1"].value == "Дата"
    assert sheet["C1"].value == "Номер дела"
    assert sheet["D1"].value == "Суд/Место"
    assert sheet["E1"].value == "Судья"
    assert sheet["F1"].value == "Ссылка"
    assert sheet["G1"].value == "Статья"
    assert sheet["H1"].value == "Текст"

    assert sheet["A2"].value == "123"
    assert sheet["B2"].value == "15.03.2023"
    assert sheet["C2"].value == "A40-1/2023"
    assert sheet["D2"].value == "АС города Москвы"
    assert sheet["E2"].value == "Иванов И.И."
    assert sheet["F2"].value == "https://kad.arbitr.ru/Card/case-1"
    assert sheet["G2"].value == "статья 61.2"
    assert sheet["H2"].value == "Полный текст решения..."
