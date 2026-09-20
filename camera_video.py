"""Discover an ONVIF RTSP stream; launch the user's VLC without writing credentials."""
import ipaddress
import os
import pathlib
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPDigestAuth
from probe_onvif_time import NS, wsse_header


def find_vlc():
    candidates = [shutil.which("vlc"), str(pathlib.Path(__file__).parent / "vlc" / "vlc.exe")]
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        candidates.append(str(pathlib.Path(os.environ.get(env, "C:/Program Files")) / "VideoLAN/VLC/vlc.exe"))
    return next((p for p in candidates if p and pathlib.Path(p).is_file()), None)


def camera_url(value, ip, schemes):
    """Some cameras advertise an old IP; always connect to the selected device."""
    ipaddress.IPv4Address(ip)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in schemes:
        raise ValueError("Камера вернула неподдерживаемый адрес потока.")
    host = ip + (f":{parsed.port}" if parsed.port else "")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, parsed.path, parsed.query, ""))


def stream_uri(ip, username, password):
    ipaddress.IPv4Address(ip)
    media_ns = "http://www.onvif.org/ver10/media/wsdl"
    device_ns = "http://www.onvif.org/ver10/device/wsdl"

    def post(url, namespace, action, body):
        envelope = (f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{device_ns}" '
                    f'xmlns:trt="{media_ns}" xmlns:tt="{NS["tt"]}" '
                    f'xmlns:wsse="{NS["wsse"]}" xmlns:wsu="{NS["wsu"]}">'
                    + wsse_header(username, password) + f'<s:Body>{body}</s:Body></s:Envelope>')
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(url, data=envelope.encode("utf-8"),
                                    auth=HTTPDigestAuth(username, password), timeout=(4, 6),
                                    allow_redirects=False,
                                    headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{action}"'})
            response.raise_for_status()
            root = ET.fromstring(response.content)
        if root.find(f'.//{{{NS["s"]}}}Fault') is not None:
            raise ValueError("Камера отклонила запрос ONVIF.")
        return root

    capabilities = post(f"http://{ip}/onvif/device_service", device_ns, "GetCapabilities",
                        "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>")
    media = capabilities.find(f'.//{{{NS["tt"]}}}Media/{{{NS["tt"]}}}XAddr')
    if media is None or not media.text:
        raise ValueError("Камера не сообщила адрес ONVIF Media.")
    url = camera_url(media.text.strip(), ip, {"http", "https"})
    profiles = post(url, media_ns, "GetProfiles", "<trt:GetProfiles/>")
    tokens = [element.get("token") for element in profiles.iter()
              if element.tag.rsplit("}", 1)[-1] == "Profiles" and element.get("token")]
    if not tokens:
        raise ValueError("Камера не вернула видеопрофили ONVIF.")
    root = post(url, media_ns, "GetStreamUri", '<trt:GetStreamUri><trt:StreamSetup>'
                '<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
                '</tt:Transport></trt:StreamSetup><trt:ProfileToken>' + escape(tokens[0]) +
                '</trt:ProfileToken></trt:GetStreamUri>')
    uri = next((e.text for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "Uri" and e.text), None)
    if not uri:
        raise ValueError("Камера не вернула RTSP-поток.")
    return camera_url(uri.strip(), ip, {"rtsp", "rtsps"})


def launch_video(vlc, uri, username, password):
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme not in {"rtsp", "rtsps"}:
        raise ValueError("Нужен адрес rtsp:// или rtsps://.")
    credentials = urllib.parse.quote(username, safe="") + ":" + urllib.parse.quote(password, safe="") + "@" if username else ""
    uri = urllib.parse.urlunsplit((parsed.scheme, credentials + parsed.netloc, parsed.path, parsed.query, ""))
    subprocess.Popen([vlc, "--no-one-instance", "--no-qt-recentplay", "--rtsp-tcp", uri],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
