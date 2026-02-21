from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook


def build_cases_excel(case_list: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cases"

    headers = [
        "Номер дела",
        "Дата акта",
        "Результат",
        "Суд",
        "Основание",
        "Ссылка",
        "Цитата",
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
        case_number, d_date, outcome, court, reason, link, quote, docs = (
            _parse_case_line(line)
        )
        sheet.append([case_number, d_date, outcome, court, reason, link, quote, docs])

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
    column_widths = [16, 12, 16, 30, 40, 30, 60, 40]
    for i, width in enumerate(column_widths):
        col_letter = sheet.cell(row=1, column=i + 1).column_letter
        sheet.column_dimensions[col_letter].width = width

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _parse_case_line(line: str) -> tuple[str, str, str, str, str, str, str, str]:
    parts = [part.strip() for part in line.split(" | ")]
    case_number = parts[0] if len(parts) > 0 else ""
    decision_date = parts[1] if len(parts) > 1 else ""
    outcome = parts[2].rstrip(".") if len(parts) > 2 else ""

    court = ""
    reason = ""
    link = ""
    quote = ""
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
        elif part.startswith("Документы:"):
            docs = part.replace("Документы:", "", 1).strip()

    return case_number, decision_date, outcome, court, reason, link, quote, docs
