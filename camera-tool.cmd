@echo off
set "PYTHONPATH=%~dp0python_packages"
"%~dp0runtime\python.exe" "%~dp0esc_vendor_bulk.py" %*
