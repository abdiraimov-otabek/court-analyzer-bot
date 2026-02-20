from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook


def build_cases_excel(case_list: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cases"
    sheet.append(["Номер дела", "Дата акта", "Результат", "Суд", "Основание", "Ссылка"])
    for line in case_list.splitlines():
        if not line.strip():
            continue
        case_number, decision_date, outcome, court, reason, link = _parse_case_line(line)
        sheet.append([case_number, decision_date, outcome, court, reason, link])

    for row in sheet.iter_rows(min_row=2, min_col=6, max_col=6):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _parse_case_line(line: str) -> tuple[str, str, str, str, str, str]:
    parts = [part.strip() for part in line.split(" | ")]
    case_number = parts[0] if len(parts) > 0 else ""
    decision_date = parts[1] if len(parts) > 1 else ""
    outcome = parts[2].rstrip(".") if len(parts) > 2 else ""

    court = ""
    reason = ""
    link = ""
    for part in parts[3:]:
        if part.startswith("Суд:"):
            court = part.replace("Суд:", "", 1).strip()
        elif part.startswith("Основание:"):
            reason = part.replace("Основание:", "", 1).strip()
        elif part.startswith("Ссылка:"):
            link = part.replace("Ссылка:", "", 1).strip()

    return case_number, decision_date, outcome, court, reason, link
