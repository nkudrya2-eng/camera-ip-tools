import argparse
import copy
import csv
import json
import pathlib
import zipfile
import xml.etree.ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
ET.register_namespace("w", W_NS)


def qn(name: str) -> str:
    prefix, tag = name.split(":", 1)
    if prefix != "w":
        raise ValueError(name)
    return f"{{{W_NS}}}{tag}"


def find_first(data, names: set[str]) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in names and value not in ("", None, "null"):
                return str(value)
        for value in data.values():
            found = find_first(value, names)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first(item, names)
            if found:
                return found
    return ""


def json_value(row: dict, *names: str) -> str:
    for column in ("device_info_json", "network_json"):
        raw = row.get(column) or ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = find_first(data, {name.lower() for name in names})
        if found:
            return found
    return ""


def compact_mac(value: str) -> str:
    value = (value or "").strip()
    if ":" in value:
        return value.upper()
    if len(value) == 12:
        return ":".join(value[i : i + 2] for i in range(0, 12, 2)).upper()
    return value


def load_camera_rows(paths: list[pathlib.Path], start_ip: str, end_ip: str) -> list[dict]:
    by_ip: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                ip = (row.get("ip") or row.get("assigned_ip") or "").strip()
                if not ip:
                    continue
                current = by_ip.setdefault(ip, {"ip_address": ip})
                model = json_value(row, "DeviceModel", "productModel") or row.get("model") or ""
                serial = json_value(row, "SerialNumber", "serialNumber", "sn") or row.get("serial_number") or ""
                mac = json_value(row, "MAC", "MACAddress", "macAddress") or row.get("mac") or ""
                values = {
                    "equipment_name": "Видеокамера",
                    "device_model": model,
                    "serial_number": serial,
                    "mac_address": compact_mac(mac),
                    "location": row.get("location") or "",
                    "ip_address": ip,
                }
                for key, value in values.items():
                    if value and not current.get(key):
                        current[key] = value

    start = tuple(int(part) for part in start_ip.split("."))
    end = tuple(int(part) for part in end_ip.split("."))
    rows = []
    for camera in by_ip.values():
        parts = tuple(int(part) for part in camera["ip_address"].split("."))
        if start <= parts <= end and camera.get("serial_number"):
            rows.append(camera)
    return sorted(rows, key=lambda item: tuple(int(part) for part in item["ip_address"].split(".")))


def paragraph_text(element: ET.Element) -> str:
    return "".join(text.text or "" for text in element.findall(".//w:t", NS)).strip()


def is_loop_marker(element: ET.Element) -> bool:
    text = paragraph_text(element)
    return text.startswith("{%") and text.endswith("%}")


def replace_text(element: ET.Element, replacements: dict[str, str]) -> None:
    paragraphs = []
    if element.tag == qn("w:p"):
        paragraphs.append(element)
    paragraphs.extend(element.findall(".//w:p", NS))
    for paragraph in paragraphs:
        text_nodes = paragraph.findall(".//w:t", NS)
        if not text_nodes:
            continue
        text = "".join(text_node.text or "" for text_node in text_nodes)
        if not any(source in text for source in replacements):
            continue
        for source, target in replacements.items():
            text = text.replace(source, target)
        text_nodes[0].text = text
        for text_node in text_nodes[1:]:
            text_node.text = ""


def replace_placeholders(element: ET.Element, camera: dict, mark: str) -> None:
    replacements = {
        "{{ camera.equipment_name }}": camera.get("equipment_name", ""),
        "{{ camera.device_model }}": camera.get("device_model", ""),
        "{{ camera.serial_number }}": camera.get("serial_number", ""),
        "{{ camera.location }}": camera.get("location", ""),
        "{{ camera.ip_address }}": camera.get("ip_address", ""),
        "{{ camera.mac_address }}": camera.get("mac_address", ""),
    }
    if mark == "yes":
        replacements["☐ Да / ☐ Нет"] = "☑ Да / ☐ Нет"
    elif mark == "no":
        replacements["☐ Да / ☐ Нет"] = "☐ Да / ☑ Нет"
    replace_text(element, replacements)


def page_break_paragraph() -> ET.Element:
    paragraph = ET.Element(qn("w:p"))
    run = ET.SubElement(paragraph, qn("w:r"))
    br = ET.SubElement(run, qn("w:br"))
    br.set(qn("w:type"), "page")
    return paragraph


def build_document_xml(template_xml: bytes, cameras: list[dict], mark: str) -> bytes:
    tree = ET.ElementTree(ET.fromstring(template_xml))
    root = tree.getroot()
    body = root.find("w:body", NS)
    if body is None:
        raise RuntimeError("Template has no document body.")

    sect_pr = body.find("w:sectPr", NS)
    template_elements = [copy.deepcopy(child) for child in list(body) if child.tag != qn("w:sectPr")]

    for child in list(body):
        body.remove(child)

    for index, camera in enumerate(cameras):
        if index:
            body.append(page_break_paragraph())
        for element in template_elements:
            if is_loop_marker(element):
                continue
            copied = copy.deepcopy(element)
            replace_placeholders(copied, camera, mark)
            body.append(copied)

    if sect_pr is not None:
        body.append(copy.deepcopy(sect_pr))

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fill_template(template_path: pathlib.Path, output_path: pathlib.Path, cameras: list[dict], mark: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template_path, "r") as source:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "word/document.xml":
                    data = build_document_xml(data, cameras, mark)
                target.writestr(item, data)


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    workspace_dir = script_dir.parent
    parser = argparse.ArgumentParser(description="Fill camera checklist DOCX template.")
    parser.add_argument("--template-docx", default=str(workspace_dir / "work" / "Чек.docx"))
    parser.add_argument("--output-docx", default=str(script_dir / "camera_checklists_from_template.docx"))
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.132")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--mark", choices=("yes", "no", "empty"), default="yes")
    parser.add_argument(
        "--input-csv",
        action="append",
        default=[
            str(script_dir / "camera_info_after.csv"),
            str(script_dir / "camera_info_clean.csv"),
            str(script_dir / "camera_inventory.csv"),
        ],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cameras = load_camera_rows([pathlib.Path(path) for path in args.input_csv], args.start_ip, args.end_ip)
    cameras = cameras[: args.limit]
    fill_template(pathlib.Path(args.template_docx), pathlib.Path(args.output_docx), cameras, args.mark)
    print(f"Written {len(cameras)} checklists to {args.output_docx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
