from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet


def build_cases_excel(case_list: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert isinstance(sheet, Worksheet)
    sheet.title = "Cases"

    headers = [
        "Номер дела",
        "Дата акта",
        "Результат",
        "Суд",
        "Основание",
        "Ссылка",
        "Решающий акт",
        "Тип акта",
        "Статус PDF",
        "Статус проверки",
        "Уверенность (Анализ)",
        "Документы",
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

    for line in case_list.splitlines():
        if not line.strip():
            continue
        if " | " not in line or line.lstrip().startswith("⚠️"):
            continue
        (
            case_number,
            d_date,
            outcome,
            court,
            reason,
            link,
            quote,
            decisive_act,
            act_type,
            pdf_status,
            verification_status,
            analysis_col,
            docs,
        ) = _parse_case_line(line)
        sheet.append([
            case_number,
            d_date,
            outcome,
            court,
            reason,
            link,
            decisive_act,
            act_type,
            pdf_status,
            verification_status,
            analysis_col,
            docs,
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
    column_widths = [16, 12, 16, 30, 40, 30, 40, 20, 18, 22, 35, 40]
    for i, width in enumerate(column_widths):
        col_letter = get_column_letter(i + 1)
        sheet.column_dimensions[col_letter].width = width

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _parse_case_line(
    line: str,
) -> tuple[str, str, str, str, str, str, str, str, str, str, str, str, str]:
    parts = [part.strip() for part in line.split(" | ")]
    case_number = parts[0] if len(parts) > 0 else ""
    decision_date = parts[1] if len(parts) > 1 else ""
    outcome = parts[2].rstrip(".") if len(parts) > 2 else ""

    court = ""
    reason = ""
    link = ""
    quote = ""
    decisive_act = ""
    act_type = ""
    pdf_status = ""
    verification_status = ""
    analysis_col = ""
    docs = ""
    for part in parts[3:]:
        if part.startswith("Суд:"):
            court = part.replace("Суд:", "", 1).strip()
        elif part.startswith("Основание:"):
            reason = part.replace("Основание:", "", 1).strip()
        elif part.startswith("Ссылка:"):
            link = part.replace("Ссылка:", "", 1).strip()
        elif part.startswith("Цитата:"):
            quote = part.replace("Цитата:", "", 1).strip()
        elif part.startswith("Акт:"):
            decisive_act = part.replace("Акт:", "", 1).strip()
        elif part.startswith("Тип акта:"):
            act_type = part.replace("Тип акта:", "", 1).strip()
        elif part.startswith("PDF:"):
            pdf_status = part.replace("PDF:", "", 1).strip()
        elif part.startswith("Проверка:"):
            verification_status = part.replace("Проверка:", "", 1).strip()
        elif part.startswith("Анализ:"):
            analysis_col = part.replace("Анализ:", "", 1).strip()
        elif part.startswith("Документы:"):
            docs = part.replace("Документы:", "", 1).strip()

    return (
        case_number,
        decision_date,
        outcome,
        court,
        reason,
        link,
        quote,
        decisive_act,
        act_type,
        pdf_status,
        verification_status,
        analysis_col,
        docs,
    )
