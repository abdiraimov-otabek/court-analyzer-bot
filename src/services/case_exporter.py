from __future__ import annotations

from io import BytesIO
from typing import Sequence

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.domain.entities import CaseDecision


def build_cases_excel(decisions: Sequence[CaseDecision]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert isinstance(sheet, Worksheet)
    sheet.title = "Cases"

    # Only the 8 requested columns
    headers = [
        "Номер",
        "Дата",
        "Номер дела",
        "Суд/Место",
        "Судья",
        "Ссылка",
        "Статья",
        "Текст",
    ]
    sheet.append(headers)

    from openpyxl.styles import Alignment, Font, PatternFill

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(
        start_color="4F81BD", end_color="4F81BD", fill_type="solid"
    )
    header_alignment = Alignment(horizontal="center", vertical="center")

    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment

    for decision in decisions:
        sheet.append([
            decision.raw_number,
            decision.raw_date.strftime("%d.%m.%Y") if decision.raw_date else "",
            decision.raw_case_number,
            decision.raw_place,
            decision.raw_judge,
            decision.raw_url,
            decision.raw_article,
            decision.raw_text,
        ])

    # Styling content
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            # Column 6 is "Ссылка"
            if (
                cell.column == 6
                and isinstance(cell.value, str)
                and cell.value.startswith("http")
            ):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"

    # Column widths
    column_widths = [16, 12, 20, 30, 20, 30, 20, 50]
    for i, width in enumerate(column_widths):
        col_letter = get_column_letter(i + 1)
        sheet.column_dimensions[col_letter].width = width

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()

