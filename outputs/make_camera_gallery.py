import argparse
import csv
import datetime as dt
import getpass
import html
import http.client
import ipaddress
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request


NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionError,
    OSError,
)

SNAPSHOT_PATHS = [
    "/ISAPI/Streaming/channels/101/picture",
    "/cgi-bin/snapshot.cgi?channel=1",
    "/cgi-bin/snapshot.cgi?chn=0",
    "/cgi-bin/viewer/video.jpg?streamid=0",
    "/image/jpeg.cgi",
    "/snapshot.jpg",
    "/jpg/image.jpg",
    "/LAPI/V1.0/Channels/0/Media/Video/Streams/0/Snapshot",
    "/LAPI/V1.0/Channels/1/Media/Video/Streams/1/Snapshot",
]

QUICK_SNAPSHOT_PATHS = [
    "/ISAPI/Streaming/channels/101/picture",
    "/cgi-bin/snapshot.cgi?channel=1",
    "/cgi-bin/viewer/video.jpg?streamid=0",
    "/jpg/image.jpg",
]

SUNELL_SNAPSHOT_TEMPLATES = [
    "/cgi-bin/snapshot.cgi?userName={username}&password={password}&channel=1",
    "/cgi-bin/snapshot.cgi?userName={username}&password={password}&chn=0",
    "/cgi-bin/viewer/video.jpg?userName={username}&password={password}&streamid=0",
    "/cgi-bin/snapshot.cgi?user={username}&pwd={password}",
]

QUICK_SUNELL_SNAPSHOT_TEMPLATES = [
    "/cgi-bin/snapshot.cgi?userName={username}&password={password}&channel=1",
    "/cgi-bin/viewer/video.jpg?userName={username}&password={password}&streamid=0",
]

RTSP_TEMPLATES = [
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/main",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/snl/live/1/1/cx/sido=-A0my1A==",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/snl/live/1/1",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/snl/live/1/2",
    "rtsp://{ip}:{rtsp_port}/snl/live/1/1/cx/sido=-A0my1A==",
    "rtsp://{ip}:{rtsp_port}/snl/live/1/1",
    "rtsp://{ip}:{rtsp_port}/snl/live/1/2",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/sub",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/third",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/media/video1/video",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/media/video2/video",
    "rtsp://{username}:{password}@{ip}:{rtsp_port}/h264",
]


def ping(ip: str, timeout_ms: int) -> bool:
    result = subprocess.run(
        ["ping", "-n", "1", "-w", str(timeout_ms), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def iter_ips(start_ip: str, end_ip: str):
    start = int(ipaddress.IPv4Address(start_ip))
    end = int(ipaddress.IPv4Address(end_ip))
    if end < start:
        raise SystemExit("--end-ip must be greater than or equal to --start-ip.")
    for value in range(start, end + 1):
        yield str(ipaddress.IPv4Address(value))


def parse_credential(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use username:password, for example Admin:password")
    username, password = value.split(":", 1)
    username = username.strip()
    password = password.strip()
    if not username:
        raise argparse.ArgumentTypeError("Credential username is empty.")
    return username, password


def load_known_camera_info(paths: list[pathlib.Path]) -> dict[str, dict[str, str]]:
    cameras = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            for row in reader:
                ip = (row.get("ip") or row.get("assigned_ip") or "").strip()
                if not ip:
                    continue
                current = cameras.setdefault(ip, {})
                for source, target in (
                    ("model", "model"),
                    ("DeviceModel", "model"),
                    ("serial_number", "serial_number"),
                    ("SerialNumber", "serial_number"),
                    ("mac", "mac"),
                    ("MAC", "mac"),
                ):
                    value = (row.get(source) or "").strip()
                    if value and not current.get(target):
                        current[target] = value
    return cameras


def load_ip_credentials(path: pathlib.Path) -> dict[str, list[tuple[str, str]]]:
    credentials = {}
    if not path.exists():
        return credentials
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            ip = (row.get("ip") or "").strip()
            username = (row.get("username") or "").strip()
            password = (row.get("password") or "").strip()
            if ip and username:
                credentials.setdefault(ip, []).append((username, password))
    return credentials


def merge_credentials(first: list[tuple[str, str]], second: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    seen = set()
    for username, password in first + second:
        key = (username, password)
        if key in seen:
            continue
        seen.add(key)
        result.append((username, password))
    return result


def build_opener(ip: str, username: str, password: str):
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, f"http://{ip}", username, password)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(password_manager),
        urllib.request.HTTPBasicAuthHandler(password_manager),
    )


def is_image(content_type: str, body: bytes) -> bool:
    content_type = (content_type or "").lower()
    return (
        content_type.startswith("image/")
        or body.startswith(b"\xff\xd8\xff")
        or body.startswith(b"\x89PNG")
    )


def snapshot_paths_for(args: argparse.Namespace, username: str, password: str) -> list[str]:
    quoted_username = urllib.parse.quote(username, safe="")
    quoted_password = urllib.parse.quote(password, safe="")
    paths = list(args.snapshot_path or (QUICK_SNAPSHOT_PATHS if args.quick else SNAPSHOT_PATHS))
    templates = QUICK_SUNELL_SNAPSHOT_TEMPLATES if args.quick else SUNELL_SNAPSHOT_TEMPLATES
    paths.extend(
        template.format(username=quoted_username, password=quoted_password)
        for template in templates
    )
    return paths


def scrub_secret(value: str, password: str) -> str:
    return value.replace(password, "***") if password else value


def safe_rtsp_url(url: str, password: str) -> str:
    return scrub_secret(url, password).replace(":***@", ":***@")


def fetch_snapshot(
    args: argparse.Namespace,
    ip: str,
    credentials: list[tuple[str, str]],
) -> tuple[bytes | None, str]:
    errors = []
    for username, password in credentials:
        print(f"{ip}: trying HTTP as {username}", flush=True)
        opener = build_opener(ip, username, password)
        for path in snapshot_paths_for(args, username, password):
            url = f"http://{ip}{path}"
            request = urllib.request.Request(url, method="GET")
            try:
                with opener.open(request, timeout=args.http_timeout) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    if response.status == 200 and is_image(content_type, body):
                        return body, f"ok http: {scrub_secret(path, password)}"
                    errors.append(f"{scrub_secret(path, password)}: not image")
            except NETWORK_ERRORS as exc:
                errors.append(f"{scrub_secret(path, password)}: {exc}")
    return None, "; ".join(errors[-3:]) if errors else "no snapshot candidates tried"


def rtsp_urls_for(args: argparse.Namespace, ip: str, username: str, password: str) -> list[str]:
    quoted_username = urllib.parse.quote(username, safe="")
    quoted_password = urllib.parse.quote(password, safe="")
    templates = args.rtsp_template or ([] if args.no_rtsp else RTSP_TEMPLATES)
    return [
        template.format(
            ip=ip,
            rtsp_port=args.rtsp_port,
            username=quoted_username,
            password=quoted_password,
        )
        for template in templates
    ]


def fetch_rtsp_snapshot(
    args: argparse.Namespace,
    ip: str,
    credentials: list[tuple[str, str]],
) -> tuple[bytes | None, str]:
    try:
        import cv2
    except ImportError:
        return None, "OpenCV is not installed; RTSP skipped"

    previous_options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;tcp|stimeout;{int(args.rtsp_timeout * 1000000)}"
    errors = []
    try:
        for username, password in credentials:
            print(f"{ip}: trying RTSP as {username}", flush=True)
            for url in rtsp_urls_for(args, ip, username, password):
                print(f"{ip}: RTSP {safe_rtsp_url(url, password)}", flush=True)
                capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                try:
                    if not capture.isOpened():
                        errors.append(f"{scrub_secret(url, password)}: cannot open")
                        continue
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        errors.append(f"{scrub_secret(url, password)}: no frame")
                        continue
                    encoded, buffer = cv2.imencode(".jpg", frame)
                    if encoded:
                        return bytes(buffer), f"ok rtsp: {scrub_secret(url, password)}"
                    errors.append(f"{scrub_secret(url, password)}: encode failed")
                finally:
                    capture.release()
    finally:
        if previous_options is None:
            os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
        else:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous_options

    return None, "; ".join(errors[-3:]) if errors else "no RTSP candidates tried"


def write_html(rows: list[dict], output_path: pathlib.Path) -> None:
    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    items = []
    for row in rows:
        image_html = (
            f'<img src="{html.escape(row["image_rel"])}" alt="{html.escape(row["ip"])}">'
            if row.get("image_rel")
            else f'<div class="no-image">{html.escape(row.get("expected_image_rel") or "Нет кадра")}</div>'
        )
        items.append(
            "<tr>"
            f"<td><div class=\"ip\">{html.escape(row['ip'])}</div>"
            f"<div class=\"model\">{html.escape(row.get('model') or '')}</div>"
            f"<div class=\"status\">{html.escape(row.get('status') or '')}</div></td>"
            f"<td>{image_html}</td>"
            "</tr>"
        )

    output_path.write_text(
        """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Камеры</title>
<style>
body { font-family: Arial, sans-serif; margin: 24px; color: #111; }
h1 { font-size: 22px; margin: 0 0 6px; }
.meta { color: #555; margin-bottom: 18px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 8px; vertical-align: top; }
th { background: #f3f3f3; text-align: left; }
td:first-child { width: 260px; }
.ip { font-size: 18px; font-weight: 700; margin-bottom: 6px; }
.model { font-size: 14px; margin-bottom: 8px; }
.status { font-size: 12px; color: #666; word-break: break-word; }
img { display: block; max-width: min(1100px, 100%); max-height: 620px; width: auto; height: auto; }
.no-image { width: min(900px, 100%); height: 340px; display: flex; align-items: center; justify-content: center; background: #eee; color: #555; }
</style>
</head>
<body>
<h1>Камеры</h1>
<div class="meta">Сформировано: """
        + html.escape(generated_at)
        + """</div>
<table>
<thead><tr><th>IP / модель</th><th>Кадр</th></tr></thead>
<tbody>
"""
        + "\n".join(items)
        + """
</tbody>
</table>
</body>
</html>
""",
        encoding="utf-8",
    )


def markdown_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", "<br>")


def write_markdown_report(rows: list[dict], output_path: pathlib.Path) -> None:
    generated_at = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        "<style>",
        "@page { size: A4 portrait; margin: 20mm 10mm 20mm 30mm; }",
        "body { font-family: 'Times New Roman', serif; font-size: 12pt; color: #111; }",
        "h1, h2, h3 { text-align: center; margin: 0; font-weight: 700; }",
        ".object-title { text-align: center; font-weight: 700; line-height: 1.25; margin-bottom: 14mm; }",
        ".system-title { text-align: center; line-height: 1.25; margin-bottom: 10mm; }",
        ".report-meta { text-align: right; font-size: 10pt; margin: 0 0 5mm; }",
        "table { width: 100%; border-collapse: collapse; table-layout: fixed; page-break-inside: avoid; }",
        "th, td { border: 1px solid #000; padding: 3mm 2mm; vertical-align: middle; }",
        "th { text-align: center; font-weight: 700; }",
        "td.num { text-align: center; width: 8mm; }",
        "td.ip { text-align: center; width: 27mm; font-weight: 700; }",
        "td.info { width: 60mm; font-size: 10pt; line-height: 1.25; }",
        "td.frame { text-align: center; }",
        "td.frame img { max-width: 82mm; max-height: 33mm; object-fit: contain; }",
        ".no-frame { height: 30mm; display: flex; align-items: center; justify-content: center; color: #555; }",
        "</style>",
        "",
        '<div class="object-title">',
        "ГОРНО-ОБОГАТИТЕЛЬНЫЙ КОМПЛЕКС ПО ДОБЫЧЕ<br>",
        "И ПЕРЕРАБОТКЕ ФЛЮОРИТ-БЕРИЛЛИЕВЫХ РУД<br>",
        "МЕСТОРОЖДЕНИЯ «ЕРМАКОВСКОЕ»",
        "</div>",
        "",
        '<div class="system-title">',
        "Комплексная система безопасности<br>",
        "(Система охранная телевизионная)",
        "</div>",
        "",
        "## Сводная таблица видеокамер",
        "",
        f'<div class="report-meta">Сформировано: {html.escape(generated_at)}</div>',
        "",
    ]

    for page_index, start in enumerate(range(0, len(rows), 5), start=1):
        chunk = rows[start : start + 5]
        lines.extend(
            [
                f"### Лист {page_index}",
                "",
                "| № | IP | Данные видеокамеры | Кадр |",
                "|---:|---|---|---|",
            ]
        )
        for index, row in enumerate(chunk, start=start + 1):
            model = markdown_cell(row.get("model", ""))
            serial_number = markdown_cell(row.get("serial_number", ""))
            mac = markdown_cell(row.get("mac", ""))
            info = (
                f"**DeviceModel:** {model}<br>"
                f"**SerialNumber:** {serial_number}<br>"
                f"**MAC:** {mac}"
            )
            if row.get("image_rel"):
                image = (
                    f'<img src="{html.escape(row["image_rel"])}" '
                    f'alt="{html.escape(row["ip"])}" '
                    'style="max-width:82mm;max-height:33mm;object-fit:contain;">'
                )
            else:
                expected_image = html.escape(row.get("expected_image_rel") or "Нет кадра")
                image = (
                    '<div style="height:30mm;display:flex;align-items:center;'
                    f'justify-content:center;color:#555;font-size:9pt;">{expected_image}</div>'
                )
            lines.append(
                f'| <span class="num">{index}</span> '
                f'| **{markdown_cell(row["ip"])}** '
                f"| {info} "
                f"| {image} |"
            )
        lines.append("")
        if start + 5 < len(rows):
            lines.append('<div style="page-break-after: always;"></div>')
            lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build an HTML table with camera IP/model and a saved snapshot.")
    parser.add_argument("--start-ip", default="10.53.240.30")
    parser.add_argument("--end-ip", default="10.53.240.132")
    parser.add_argument("--credential", action="append", type=parse_credential, default=None)
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--ping-timeout-ms", type=int, default=700)
    parser.add_argument("--http-timeout", type=float, default=4.0)
    parser.add_argument("--rtsp-port", type=int, default=554)
    parser.add_argument("--rtsp-timeout", type=float, default=5.0)
    parser.add_argument("--quick", action="store_true", help="Try fewer snapshot URLs.")
    parser.add_argument(
        "--snapshot-path",
        action="append",
        default=None,
        help="Snapshot path to try, for example /ISAPI/Streaming/channels/101/picture. Can be used more than once.",
    )
    parser.add_argument(
        "--rtsp-template",
        action="append",
        default=None,
        help="RTSP URL template. Available fields: {ip}, {rtsp_port}, {username}, {password}. Can be used more than once.",
    )
    parser.add_argument("--no-rtsp", action="store_true", help="Do not try RTSP fallback.")
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--report-only", action="store_true", help="Use existing images and CSV data without connecting to cameras.")
    parser.add_argument("--output-html", default=str(script_dir / "camera_gallery.html"))
    parser.add_argument("--output-md", default=str(script_dir / "camera_pnr_report.md"))
    parser.add_argument("--no-md", action="store_true", help="Do not write Markdown PNR report.")
    parser.add_argument("--assets-dir", default=str(script_dir / "camera_gallery_assets"))
    parser.add_argument("--credentials-csv", default=str(script_dir / "camera_credentials.csv"))
    parser.add_argument(
        "--models-csv",
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
    credentials = args.credential
    if args.report_only:
        credentials = []
    elif not credentials:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
        credentials = [(args.username, password)]
    if credentials:
        print("Credentials to try: " + ", ".join(username for username, _ in credentials), flush=True)

    output_path = pathlib.Path(args.output_html)
    markdown_path = pathlib.Path(args.output_md)
    assets_dir = pathlib.Path(args.assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    known_cameras = load_known_camera_info([pathlib.Path(path) for path in args.models_csv])
    ip_credentials = load_ip_credentials(pathlib.Path(args.credentials_csv))

    rows = []
    for ip in iter_ips(args.start_ip, args.end_ip):
        camera_info = known_cameras.get(ip, {})
        image_name = f"{ip.replace('.', '_')}.jpg"
        image_path = assets_dir / image_name
        expected_image_rel = f"{assets_dir.name}/{image_name}"
        existing_image_rel = expected_image_rel if image_path.exists() else ""
        if args.report_only:
            rows.append(
                {
                    "ip": ip,
                    "model": camera_info.get("model", ""),
                    "serial_number": camera_info.get("serial_number", ""),
                    "mac": camera_info.get("mac", ""),
                    "status": "existing frame" if existing_image_rel else "no existing frame",
                    "image_rel": existing_image_rel,
                    "expected_image_rel": expected_image_rel,
                }
            )
            continue

        if not args.skip_ping and not ping(ip, args.ping_timeout_ms):
            print(f"{ip}: no ping reply", flush=True)
            rows.append(
                {
                    "ip": ip,
                    "model": camera_info.get("model", ""),
                    "serial_number": camera_info.get("serial_number", ""),
                    "mac": camera_info.get("mac", ""),
                    "status": "no ping reply; using existing frame" if existing_image_rel else "no ping reply",
                    "image_rel": existing_image_rel,
                    "expected_image_rel": expected_image_rel,
                }
            )
            continue

        camera_credentials = merge_credentials(ip_credentials.get(ip, []), credentials)
        print(f"{ip}: fetching snapshot", flush=True)
        body, status = fetch_snapshot(args, ip, camera_credentials)
        if body is None and not args.no_rtsp:
            print(f"{ip}: fetching RTSP frame", flush=True)
            body, rtsp_status = fetch_rtsp_snapshot(args, ip, camera_credentials)
            status = rtsp_status if body else f"{status}; {rtsp_status}"
        image_rel = ""
        if body:
            image_path.write_bytes(body)
            image_rel = f"{assets_dir.name}/{image_name}"
        elif existing_image_rel:
            image_rel = existing_image_rel
            status = f"{status}; using existing frame"
        rows.append(
            {
                "ip": ip,
                "model": camera_info.get("model", ""),
                "serial_number": camera_info.get("serial_number", ""),
                "mac": camera_info.get("mac", ""),
                "status": status,
                "image_rel": image_rel,
                "expected_image_rel": expected_image_rel,
            }
        )

    write_html(rows, output_path)
    print(f"Written {output_path}", flush=True)
    if not args.no_md:
        write_markdown_report(rows, markdown_path)
        print(f"Written {markdown_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
