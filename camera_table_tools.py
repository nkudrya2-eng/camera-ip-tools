"""Table filtering and dependency-free Excel export for the offline package."""
import csv
import datetime as dt
import re
import zipfile
from xml.sax.saxutils import escape

FIELDS = [
    ("ip", "IP", 18), ("new_ip", "Новый IP", 18), ("model", "Модель", 30),
    ("mac", "MAC", 22), ("serial_number", "Серийный номер", 25),
    ("device_id", "DeviceID", 25), ("mask", "Маска", 18),
    ("gateway_read", "Шлюз", 18), ("ntp_read", "NTP", 25),
    ("timezone_read", "Часовой пояс", 22), ("codec", "Кодек", 14),
    ("profile", "Профиль", 18), ("protocol", "Протокол", 18),
    ("last_seen", "Последний ответ", 24), ("details_read_at", "Чтение настроек", 24),
    ("status", "Статус", 44), ("details_message", "Описание ошибки", 44),
]
READ_FIELDS = ("mask", "gateway_read", "ntp_read", "timezone_read", "codec")


def field_value(row, key):
    if key == "serial":
        return row.get("serial_number") or row.get("device_id", "")
    if key == "seen":
        return str(row.get("seen_passes", 0)) if row.get("online") else ""
    value = row.get(key, "")
    if key in READ_FIELDS and not value:
        return "Не прочитано"
    return str(value or "")


def matches(row, query="", field="", column_filters=None):
    """All active column filters are ANDed; text matching is case insensitive."""
    keys = [field] if field else [key for key, _, _ in FIELDS]
    needle = query.strip().casefold()
    if needle and not any(needle in field_value(row, key).casefold() for key in keys):
        return False
    return all(value.casefold() in field_value(row, key).casefold()
               for key, value in (column_filters or {}).items() if value)


def clear_readings(row):
    for key in (*READ_FIELDS, "details_status", "details_source", "details_message", "details_read_at"):
        row[key] = ""


def export_rows(path, rows, project, scope):
    if str(path).lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow([label for _, label, _ in FIELDS])
            for row in rows:
                # Prevent spreadsheet applications interpreting device text as formulas.
                values = [field_value(row, key) for key, _, _ in FIELDS]
                writer.writerow(["'" + v if v.startswith(("=", "+", "-", "@")) else v for v in values])
        return
    if not str(path).lower().endswith(".xlsx"):
        raise ValueError("Выберите расширение .xlsx или .csv.")
    if len(rows) > 1048572:
        raise ValueError("Слишком много строк для одного листа Excel.")

    def cell(address, value, style=0):
        # Inline text preserves IP/MAC/serials, leading zeroes and literal '=' safely.
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(value))[:32767]
        return f'<c r="{address}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(value)}</t></is></c>'

    last = chr(64 + len(FIELDS))
    data = ['<row r="1">' + cell("A1", "Камеры — " + project, 1) + '</row>',
            '<row r="2">' + cell("A2", f"Выгрузка: {dt.datetime.now():%Y-%m-%d %H:%M:%S} | {scope} | Строк: {len(rows)}") + '</row>',
            '<row r="4">' + ''.join(cell(f'{chr(65+i)}4', label, 1) for i, (_, label, _) in enumerate(FIELDS)) + '</row>']
    for number, row in enumerate(rows, 5):
        data.append(f'<row r="{number}">' + ''.join(
            cell(f'{chr(65+i)}{number}', field_value(row, key)) for i, (key, _, _) in enumerate(FIELDS)) + '</row>')
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    sheet = (f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/>'
             '</sheetView></sheetViews><cols>' + ''.join(
                 f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, (_, _, width) in enumerate(FIELDS, 1)) +
             '</cols><sheetData>' + ''.join(data) + '</sheetData>' +
             f'<autoFilter ref="A4:{last}{len(rows)+4}"/></worksheet>')
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Камеры" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        'xl/styles.xml': f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF24476A"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
        'xl/worksheets/sheet1.xml': sheet,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content)
