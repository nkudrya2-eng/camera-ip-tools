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
