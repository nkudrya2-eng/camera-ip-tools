# Camera IP tools

Scripts for configuring and inventorying IP cameras that appear at a known default address.

## Files

- `outputs/configure_cameras_loop.py` - waits for a camera at `192.168.0.250`, assigns IP addresses from a pool, verifies the new IP, and logs inventory.
- `outputs/collect_camera_info.py` - scans a pool or a CSV of assigned IPs and collects model, serial number, MAC, firmware, and raw API responses.
- `outputs/probe_lapi_endpoints.py` - checks which LAPI endpoints are supported by a camera or a pool.
- `outputs/README_configure_cameras_loop.txt` - operator instructions in Russian.
- `outputs/ESC_static_analysis.txt` - notes from static analysis of the manufacturer ESC utility.

CSV inventory files, packet captures, bytecode dumps, and local Codex metadata are intentionally ignored.

## Example

```powershell
py -3 "outputs\configure_cameras_loop.py" --username Admin --start-ip 10.53.240.88 --end-ip 10.53.240.254 --gateway 10.53.240.1 --empty-timeout 120
```

```powershell
py -3 "outputs\collect_camera_info.py" --username Admin --start-ip 10.53.240.87 --end-ip 10.53.240.93 --api auto
```

## Camera frame comparison

`compare_cameras.py` extracts old frames from `Камеры.pdf`, compares them with new gallery frames, and writes an Excel report plus side-by-side review images.

Install dependencies:

```powershell
py -3 -m pip install -r requirements.txt
```

Run with the current local files:

```powershell
py -3 .\compare_cameras.py
```

On this workstation `py -3` may point to Python 3.14, where some Excel/PDF wheels can be unavailable. The verified Codex runtime command is:

```powershell
& "C:\Users\n.kudrya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" ".\compare_cameras.py"
```

Default inputs:

- Excel registry: `Копия Перечень IP адресов камер видеонаблюдения на Ермаковском ГОК от 01.07.2026 (002).xlsx`
- PDF with old frames: `Камеры.pdf`
- New frames: `outputs\gallery_2026-07-03_30-168\assets`

Default outputs:

- `outputs\camera_compare_2026-07-03\result_camera_mapping.xlsx`
- `outputs\camera_compare_2026-07-03\old_frames_from_pdf`
- `outputs\camera_compare_2026-07-03\review`

In the Excel report, start with `matches`, then manually check `low_confidence`. The `review` folder has one image per match: new frame on the left, old PDF frame on the right.
