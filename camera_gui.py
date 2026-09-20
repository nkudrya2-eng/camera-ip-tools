import copy
import csv
import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
import pathlib
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

import yaml

APP_DIR = pathlib.Path(__file__).resolve().parent
LOCAL_CONFIGURATOR_DIR = APP_DIR.parent / "AutoStartSettingWork_v0.02"
CONFIGURATOR_DIR = LOCAL_CONFIGURATOR_DIR if LOCAL_CONFIGURATOR_DIR.exists() else APP_DIR
sys.path.insert(0, str(CONFIGURATOR_DIR))

import camera_configurator as configurator
from camera_inventory_reader import probe_onvif_camera, read_camera_settings
from camera_store import CameraStore, camera_identity
from camera_table_tools import FIELDS, matches, clear_readings, export_rows
from camera_video import find_vlc, stream_uri, launch_video, camera_url
import camera_profiles as profiles_mod
from camera_vlc_widget import EmbeddedVlcPlayer

DEFAULT_CONFIG = CONFIGURATOR_DIR / "camera_config.yaml"
DATABASE_PATH = APP_DIR / "camera_tools.db"
DEFAULT_CAMERA_IP = "192.168.0.250"
DEFAULT_ASSIGNMENT_LOG = APP_DIR / "default_ip_assignment_results.csv"

TIMEZONE_VALUES = [f"(GMT{offset:+03d}:00)" for offset in range(-12, 15)]
TIMEZONE_VALUES[TIMEZONE_VALUES.index("(GMT+09:00)")] = "(GMT+09:00) Якутия"


class QueueWriter:
    def __init__(self, output_queue: queue.Queue):
        self.output_queue = output_queue

    def write(self, value: str) -> int:
        if value:
            self.output_queue.put(("log", value))
        return len(value)

    def flush(self) -> None:
        pass


class CameraGui(tk.Tk):
    """Modern Wireshark-inspired 3-pane interface for CameraIpTools."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Camera IP Tools — 2026.09.4")
        self.geometry("1420x880")
        self.minsize(1100, 720)

        self.config_data = {}
        self.inventory = []  # Fast clean startup: starts empty!
        self.events = queue.Queue()
        self.running = False
        self.cancel_requested = threading.Event()
        self.current_project_name = ""
        self.active_credential_rules = []
        self.sort_column = ""
        self.sort_reverse = False
        self.table_headings = {}
        self.column_filters = {}
        self.quick_filter = "all"
        self.scan_time = ""
        self.store = CameraStore(DATABASE_PATH)
        self.custom_profiles = {}
        self.selected_profile_key = tk.StringVar(value="medium")

        # Global parameters (configured via Settings dialog)
        self.scan_start = tk.StringVar(value="192.168.0.250")
        self.scan_end = tk.StringVar(value="192.168.0.250")
        self.pass_count = tk.StringVar(value="1")
        self.vendor_discovery = tk.BooleanVar(value=True)
        self.interface_ip = tk.StringVar(value="")
        self.network_mask = tk.StringVar(value="255.255.252.0")
        self.gateway = tk.StringVar(value="10.81.240.1")
        self.dns_main = tk.StringVar(value="8.8.8.8")
        self.default_username = tk.StringVar(value="Admin")
        self.default_password = tk.StringVar(value="1234")
        self.credential_exceptions = tk.StringVar()
        self.ntp_server = tk.StringVar(value="10.99.200.60")
        self.cross_timezone = tk.StringVar(value="(GMT+09:00) Якутия")
        self.target_start = tk.StringVar(value="10.81.241.150")
        self.target_end = tk.StringVar(value="10.81.241.200")
        self.apply_video_profile = tk.BooleanVar(value=True)

        # UI state
        self.bottom_collapsed = False
        self.autoscroll_log = tk.BooleanVar(value=True)
        self.filter_text = tk.StringVar()

        configurator.PROFILES_DIR = CONFIGURATOR_DIR / "profiles"
        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._drain_events)

        if DEFAULT_CONFIG.exists():
            self._load_config_file(DEFAULT_CONFIG)
        self._load_projects()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(font_name).configure(family="Segoe UI", size=9)
        tkfont.nametofont("TkHeadingFont").configure(family="Segoe UI", size=9, weight="bold")
        tkfont.nametofont("TkFixedFont").configure(family="Consolas", size=9)

        style.configure("Treeview", font=("Segoe UI", 9), rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=(7, 4), font=("Segoe UI", 9))
        style.configure("Primary.TButton", padding=(9, 4), font=("Segoe UI", 9, "bold"))
        style.configure("TEntry", padding=3)
        style.configure("TCombobox", padding=3)
        style.configure("FilterChip.TButton", padding=(6, 2), font=("Segoe UI", 8))
        style.configure("ActiveChip.TButton", padding=(6, 2), font=("Segoe UI", 8, "bold"))

    # =========================================================================
    # UI CONSTRUCTION (Wireshark 3-Pane Layout)
    # =========================================================================

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)  # PanedWindow expands

        # 1. Top Toolbar (Compact 1 Row)
        self._build_top_toolbar()

        # 2. Filter & Quick-filter Bar
        self._build_filter_bar()

        # 3. Main 3-Pane Layout (Vertical PanedWindow)
        self.v_paned = ttk.PanedWindow(self, orient="vertical")
        self.v_paned.grid(row=2, column=0, sticky="nsew", padx=6, pady=(2, 4))

        # 3a. Top Pane: Camera Table
        self._build_camera_table(self.v_paned)

        # 3b. Bottom Pane: Horizontal PanedWindow (Log + Video)
        self._build_bottom_panes(self.v_paned)

        # 4. Status Bar
        self._build_status_bar()

    def _build_top_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6, 8, 2))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(10, weight=1)

        # Main Action Buttons
        self.btn_scan = ttk.Button(bar, text="🔍 Поиск", style="Primary.TButton", command=self.start_scan)
        self.btn_scan.grid(row=0, column=0, padx=(0, 4))

        self.btn_stop = ttk.Button(bar, text="⏹ Остановить", command=self._request_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 8))

        # Dropdown: Действия
        self.actions_mb = ttk.Menubutton(bar, text="Действия ▾")
        self.actions_menu = tk.Menu(self.actions_mb, tearoff=0)
        self.actions_mb.configure(menu=self.actions_menu)
        self.actions_mb.grid(row=0, column=2, padx=(0, 4))

        self.actions_menu.add_command(label="Назначить 192.168.0.250...", command=self.apply_default_network_plan)
        self.actions_menu.add_command(label="Сформировать адресный план...", command=self.build_address_plan)
        self.actions_menu.add_command(label="Применить настройки к отмеченным...", command=self.apply_selected)
        self.actions_menu.add_command(label="Прочитать настройки отмеченных...", command=self.read_selected_details)
        self.actions_menu.add_command(label="Проверить доступность (Ping)...", command=self.check_selected_ping)
        self.actions_menu.add_separator()
        self.actions_menu.add_command(label="Отметить все", command=lambda: self._set_all_assign(True))
        self.actions_menu.add_command(label="Снять все отметки", command=lambda: self._set_all_assign(False))
        self.actions_menu.add_command(label="Очистить текущий список", command=self.clear_inventory)
        self.actions_menu.add_separator()
        self.actions_menu.add_command(label="Экспорт в Excel (.xlsx)...", command=lambda: self._export_dialog("xlsx"))
        self.actions_menu.add_command(label="Экспорт в CSV...", command=lambda: self._export_dialog("csv"))
        self.actions_menu.add_separator()
        self.actions_menu.add_command(label="История объекта и сохранённые камеры...", command=self._open_history_dialog)

        # Dropdown: Профили
        self.profiles_mb = ttk.Menubutton(bar, text="Профили ▾")
        self.profiles_menu = tk.Menu(self.profiles_mb, tearoff=0)
        self.profiles_mb.configure(menu=self.profiles_menu)
        self.profiles_mb.grid(row=0, column=3, padx=(0, 8))

        for k, name in profiles_mod.list_profile_items():
            self.profiles_menu.add_radiobutton(
                label=f"{name}",
                variable=self.selected_profile_key,
                value=k,
                command=self._on_profile_selected,
            )
        self.profiles_menu.add_separator()
        self.profiles_menu.add_command(label="Оценка суммарного трафика...", command=self._open_traffic_dialog)
        self.profiles_menu.add_command(label="Импорт профилей...", command=self._import_profiles_dialog)
        self.profiles_menu.add_command(label="Экспорт профилей...", command=self._export_profiles_dialog)

        # Settings Dialog Button
        ttk.Button(bar, text="⚙ Параметры...", command=self._open_settings_dialog).grid(row=0, column=4, padx=(0, 12))

        # Toggle Bottom Panes Button
        self.btn_toggle_bottom = ttk.Button(bar, text="▼ Скрыть панель", width=16, command=self._toggle_bottom_pane)
        self.btn_toggle_bottom.grid(row=0, column=5, padx=(0, 12))

        # Project Selection (Right Aligned)
        proj_frame = ttk.Frame(bar)
        proj_frame.grid(row=0, column=10, sticky="e")
        ttk.Label(proj_frame, text="Объект:").pack(side="left", padx=(0, 4))
        self.project_name = tk.StringVar()
        self.project_box = ttk.Combobox(proj_frame, textvariable=self.project_name, state="readonly", width=18)
        self.project_box.pack(side="left", padx=2)
        self.project_box.bind("<<ComboboxSelected>>", self._project_changed)
        ttk.Button(proj_frame, text="+", width=3, command=self._new_project).pack(side="left", padx=1)
        ttk.Button(proj_frame, text="-", width=3, command=self._delete_project).pack(side="left", padx=1)

    def _build_filter_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 2, 8, 4))
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Фильтр:").grid(row=0, column=0, padx=(0, 4))

        entry_frame = ttk.Frame(bar)
        entry_frame.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        entry_frame.columnconfigure(0, weight=1)

        self.filter_entry = ttk.Entry(entry_frame, textvariable=self.filter_text)
        self.filter_entry.grid(row=0, column=0, sticky="ew")
        self.filter_entry.bind("<KeyRelease>", lambda _e: self._on_filter_changed())

        self.btn_clear_filter = ttk.Button(entry_frame, text="✖", width=3, command=self._clear_filter)
        self.btn_clear_filter.grid(row=0, column=1, padx=(2, 0))

        # Quick Filter Chips
        chips_frame = ttk.Frame(bar)
        chips_frame.grid(row=0, column=2, sticky="e", padx=(0, 12))

        self.chip_buttons = {}
        chips = [
            ("all", "Все"),
            ("default_ip", "192.168.0.250"),
            ("errors", "Ошибки"),
            ("offline", "Недоступные"),
            ("h264", "H.264"),
            ("h265", "H.265"),
            ("unread", "Не прочитаны"),
        ]
        for key, label in chips:
            btn = ttk.Button(
                chips_frame,
                text=label,
                style="FilterChip.TButton",
                command=lambda k=key: self._set_quick_filter(k),
            )
            btn.pack(side="left", padx=1)
            self.chip_buttons[key] = btn
        self._update_chip_styles()

        # Counter Label
        self.counter_label = ttk.Label(bar, text="Найдено: 0 | Показано: 0 | Отмечено: 0", font=("Segoe UI", 9, "bold"))
        self.counter_label.grid(row=0, column=3, sticky="e")

    def _build_camera_table(self, parent) -> None:
        frame = ttk.Frame(parent)
        parent.add(frame, weight=3)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        columns = (
            "actions", "assign", "ip", "new_ip", "model", "mac", "serial", "mask", "gateway_read",
            "ntp_read", "timezone_read", "codec", "profile", "seen", "status",
        )
        self.table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")

        col_defs = [
            ("actions", "⋮", 36, "center"),
            ("assign", "Назначить", 80, "center"),
            ("ip", "Текущий IP", 120, "w"),
            ("new_ip", "Новый IP", 120, "w"),
            ("model", "Модель", 240, "w"),
            ("mac", "MAC-адрес", 140, "w"),
            ("serial", "S/N / DeviceID", 140, "w"),
            ("mask", "Маска", 115, "w"),
            ("gateway_read", "Шлюз", 115, "w"),
            ("ntp_read", "NTP камеры", 125, "w"),
            ("timezone_read", "Часовой пояс", 140, "w"),
            ("codec", "Кодек", 75, "w"),
            ("profile", "Профиль", 95, "w"),
            ("seen", "Ответы", 65, "center"),
            ("status", "Статус", 200, "w"),
        ]

        for key, label, width, anchor in col_defs:
            self.table_headings[key] = label
            self.table.heading(key, text=label, command=lambda c=key: self._sort_table(c))
            self.table.column(key, width=width, anchor=anchor)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.table.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Table Bindings
        self.table.bind("<Button-1>", self._on_table_click)
        self.table.bind("<Button-3>", self._on_table_right_click)
        self.table.bind("<<TreeviewSelect>>", self._on_table_select)
        self.table.bind("<space>", self._on_space_toggle)

        # Context Menu
        self._build_context_menu()

    def _build_bottom_panes(self, parent) -> None:
        self.bottom_frame = ttk.Frame(parent)
        parent.add(self.bottom_frame, weight=2)
        self.bottom_frame.rowconfigure(0, weight=1)
        self.bottom_frame.columnconfigure(0, weight=1)

        self.h_paned = ttk.PanedWindow(self.bottom_frame, orient="horizontal")
        self.h_paned.grid(row=0, column=0, sticky="nsew")

        # Bottom Left: Operation Log
        log_pane = ttk.Frame(self.h_paned)
        self.h_paned.add(log_pane, weight=3)
        log_pane.rowconfigure(1, weight=1)
        log_pane.columnconfigure(0, weight=1)

        log_toolbar = ttk.Frame(log_pane, padding=(4, 2))
        log_toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(log_toolbar, text="📋 Журнал операций", font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)
        ttk.Button(log_toolbar, text="🧹 Очистить", width=10, command=self._clear_log).pack(side="left", padx=6)
        ttk.Button(log_toolbar, text="💾 Сохранить...", width=12, command=self._save_log_dialog).pack(side="left", padx=2)
        ttk.Checkbutton(log_toolbar, text="Автопрокрутка", variable=self.autoscroll_log).pack(side="right", padx=4)

        self.log = ScrolledText(log_pane, wrap="word", height=8, font=("Consolas", 9), bg="#181a1f", fg="#dcdfe4", insertbackground="white")
        self.log.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        self.log.tag_configure("success", foreground="#98c379")
        self.log.tag_configure("error", foreground="#e06c75")
        self.log.tag_configure("warn", foreground="#e5c07b")
        self.log.tag_configure("info", foreground="#61afef")

        # Bottom Right: Embedded Video Player
        video_pane = ttk.Frame(self.h_paned)
        self.h_paned.add(video_pane, weight=2)
        video_pane.rowconfigure(0, weight=1)
        video_pane.columnconfigure(0, weight=1)

        self.vlc_player = EmbeddedVlcPlayer(video_pane, on_external_request=self._launch_external_vlc)
        self.vlc_player.grid(row=0, column=0, sticky="nsew")

    def _build_status_bar(self) -> None:
        status_frame = ttk.Frame(self, padding=(8, 4))
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)

        self.status = tk.StringVar(value="Готово к работе.")
        self.status_label = ttk.Label(status_frame, textvariable=self.status, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")

        self.progress = ttk.Progressbar(status_frame, mode="determinate", length=220)
        self.progress.grid(row=0, column=1, padx=(12, 12))

        admin_text = "🛡 Администратор" if self._is_admin() else "⚠ Пользователь"
        admin_fg = "green" if self._is_admin() else "orange"
        lbl = tk.Label(status_frame, text=admin_text, fg=admin_fg, font=("Segoe UI", 8, "bold"))
        lbl.grid(row=0, column=2, sticky="e")

    # =========================================================================
    # CONTEXT MENU & ROW ACTIONS
    # =========================================================================

    def _build_context_menu(self) -> None:
        self.row_menu = tk.Menu(self, tearoff=0)

        # Video section
        self.row_menu.add_command(label="📹 Открыть видео (доп. поток)", command=lambda: self._play_selected_video("sub"))
        self.row_menu.add_command(label="📹 Открыть видео (основной поток)", command=lambda: self._play_selected_video("main"))
        self.row_menu.add_command(label="🖥 Открыть во внешнем VLC", command=lambda: self._launch_external_vlc(self._get_active_row()))
        self.row_menu.add_command(label="🌐 Открыть в браузере", command=self._open_in_browser)
        self.row_menu.add_separator()

        # Configuration section
        self.row_menu.add_command(label="⚙ Сетевые настройки камеры...", command=self._open_camera_network_dialog)
        self.row_menu.add_command(label="🎬 Видеопотоки и кодек...", command=self._open_camera_video_dialog)

        # Submenu: Применить профиль
        self.profile_submenu = tk.Menu(self.row_menu, tearoff=0)
        for k, name in profiles_mod.list_profile_items():
            self.profile_submenu.add_command(
                label=name,
                command=lambda pk=k: self._apply_profile_to_camera(self._get_active_row(), pk),
            )
        self.row_menu.add_cascade(label="📋 Применить видеопрофиль к камере", menu=self.profile_submenu)

        self.row_menu.add_command(label="🔄 Прочитать / обновить настройки", command=self._read_single_camera_details)
        self.row_menu.add_command(label="📶 Проверить доступность (Ping)", command=self._ping_single_camera)
        self.row_menu.add_separator()

        # Clipboard & filter section
        copy_menu = tk.Menu(self.row_menu, tearoff=0)
        copy_menu.add_command(label="IP-адрес", command=lambda: self._copy_cell("ip"))
        copy_menu.add_command(label="MAC-адрес", command=lambda: self._copy_cell("mac"))
        copy_menu.add_command(label="Всю строку", command=self._copy_row_text)
        self.row_menu.add_cascade(label="📋 Копировать", menu=copy_menu)

        self.row_menu.add_command(label="🔍 Фильтровать по этому значению", command=self._filter_by_cell)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="❌ Убрать из текущего списка", command=self._remove_selected_from_table)

    def _get_active_row(self) -> dict:
        sel = self.table.selection()
        if sel:
            ip = self.table.item(sel[0], "values")[2]  # "ip" column index
            for r in self.inventory:
                if r.get("ip") == ip:
                    return r
        return self.vlc_player.current_camera

    def _on_table_select(self, _event=None) -> None:
        sel = self.table.selection()
        if not sel:
            return
        vals = self.table.item(sel[0], "values")
        if len(vals) > 2:
            ip = vals[2]
            for r in self.inventory:
                if r.get("ip") == ip:
                    # Update video widget label without auto-playing
                    self.vlc_player.set_camera(r)
                    break

    def _on_table_click(self, event) -> None:
        region = self.table.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.table.identify_column(event.x)
        item = self.table.identify_row(event.y)
        if not item:
            return

        # Column #1 is 'actions' (⋮)
        if col == "#1":
            self.table.selection_set(item)
            self._show_context_menu(event.x_root, event.y_root)
            return

        # Column #2 is 'assign' (checkbox)
        if col == "#2":
            self._toggle_assign(item)
            return

    def _on_table_right_click(self, event) -> None:
        item = self.table.identify_row(event.y)
        if item:
            if item not in self.table.selection():
                self.table.selection_set(item)
            self._show_context_menu(event.x_root, event.y_root)

    def _show_context_menu(self, x, y) -> None:
        row = self._get_active_row()
        if row:
            self.row_menu.tk_popup(x, y)

    def _on_space_toggle(self, _event=None) -> None:
        for item in self.table.selection():
            self._toggle_assign(item)

    def _toggle_assign(self, item) -> None:
        vals = list(self.table.item(item, "values"))
        ip = vals[2]
        for r in self.inventory:
            if r.get("ip") == ip:
                r["assign"] = not r.get("assign", False)
                vals[1] = "☑" if r["assign"] else "☐"
                self.table.item(item, values=vals)
                break
        self._update_counter_label()

    def _set_all_assign(self, state: bool) -> None:
        visible = self._visible_rows()
        for r in visible:
            r["assign"] = state
        self._render_table()

    # =========================================================================
    # FILTERING & SEARCH
    # =========================================================================

    def _on_filter_changed(self) -> None:
        self._render_table()

    def _clear_filter(self) -> None:
        self.filter_text.set("")
        self._render_table()

    def _set_quick_filter(self, key: str) -> None:
        self.quick_filter = key
        self._update_chip_styles()
        self._render_table()

    def _update_chip_styles(self) -> None:
        for k, btn in self.chip_buttons.items():
            btn.configure(style="ActiveChip.TButton" if k == self.quick_filter else "FilterChip.TButton")

    def _visible_rows(self) -> list[dict]:
        query = self.filter_text.get().strip().casefold()
        qf = self.quick_filter
        result = []
        for r in self.inventory:
            # Quick filter condition
            if qf == "default_ip" and r.get("ip") != DEFAULT_CAMERA_IP and r.get("current_ip") != DEFAULT_CAMERA_IP:
                continue
            if qf == "errors" and "ошибк" not in str(r.get("status", "")).lower() and "fail" not in str(r.get("status", "")).lower():
                continue
            if qf == "offline" and r.get("online"):
                continue
            if qf == "h264" and "264" not in str(r.get("codec", "")).lower():
                continue
            if qf == "h265" and "265" not in str(r.get("codec", "")).lower():
                continue
            if qf == "unread" and (r.get("mask") or r.get("gateway_read")):
                continue

            # Text search condition (matches IP, new_ip, model, mac, serial, status)
            if query:
                haystack = f"{r.get('ip','')} {r.get('new_ip','')} {r.get('model','')} {r.get('mac','')} {r.get('serial_number','')} {r.get('device_id','')} {r.get('status','')}".casefold()
                if query not in haystack:
                    continue

            result.append(r)
        return result

    def _render_table(self) -> None:
        selected_ips = {self.table.item(item, "values")[2] for item in self.table.selection() if len(self.table.item(item, "values")) > 2}
        self.table.delete(*self.table.get_children())
        visible = self._visible_rows()

        # Sort if requested
        if self.sort_column:
            def sort_key(row):
                val = row.get(self.sort_column, "")
                if self.sort_column in ("ip", "new_ip"):
                    try:
                        return ipaddress.IPv4Address(val)
                    except Exception:
                        return ipaddress.IPv4Address("0.0.0.0")
                return str(val).lower()
            visible.sort(key=sort_key, reverse=self.sort_reverse)

        for r in visible:
            assign_mark = "☑" if r.get("assign") else "☐"
            serial = r.get("serial_number") or r.get("device_id", "")
            seen = str(r.get("seen_passes", 0)) if r.get("online") else ""
            item_id = self.table.insert(
                "", "end",
                values=(
                    "⋮",
                    assign_mark,
                    r.get("ip", ""),
                    r.get("new_ip", ""),
                    r.get("model", ""),
                    r.get("mac", ""),
                    serial,
                    r.get("mask", ""),
                    r.get("gateway_read", ""),
                    r.get("ntp_read", ""),
                    r.get("timezone_read", ""),
                    r.get("codec", ""),
                    r.get("profile", ""),
                    seen,
                    r.get("status", ""),
                ),
            )
            if r.get("ip") in selected_ips:
                self.table.selection_add(item_id)

        self._update_counter_label()

    def _update_counter_label(self) -> None:
        total = len(self.inventory)
        shown = len(self._visible_rows())
        checked = sum(1 for r in self.inventory if r.get("assign"))
        self.counter_label.configure(text=f"Найдено: {total} | Показано: {shown} | Отмечено: {checked}")

    def _sort_table(self, col: str) -> None:
        if col in ("actions", "assign"):
            return
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False

        arrow = " ▼" if self.sort_reverse else " ▲"
        for key, text in self.table_headings.items():
            self.table.heading(key, text=text + (arrow if key == col else ""))
        self._render_table()

    def _filter_by_cell(self) -> None:
        row = self._get_active_row()
        if row and row.get("ip"):
            self.filter_text.set(row.get("ip"))
            self._render_table()

    # =========================================================================
    # BOTTOM PANE TOGGLE & LOG
    # =========================================================================

    def _toggle_bottom_pane(self) -> None:
        if self.bottom_collapsed:
            self.v_paned.add(self.bottom_frame, weight=2)
            self.btn_toggle_bottom.configure(text="▼ Скрыть панель")
            self.bottom_collapsed = False
        else:
            self.v_paned.forget(self.bottom_frame)
            self.btn_toggle_bottom.configure(text="▲ Показать панель")
            self.bottom_collapsed = True

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def _save_log_dialog(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Сохранить журнал операций",
            defaultextension=".txt",
            filetypes=[("Текстовый файл", "*.txt"), ("Все файлы", "*.*")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.get("1.0", "end"))
            self.status.set(f"Журнал сохранён: {path}")

    # =========================================================================
    # VIDEO ACTIONS
    # =========================================================================

    def _play_selected_video(self, stream_type: str = "sub") -> None:
        row = self._get_active_row()
        if not row:
            messagebox.showinfo("Видео", "Сначала выберите камеру в таблице.")
            return
        if self.bottom_collapsed:
            self._toggle_bottom_pane()
        self.vlc_player.play(row, stream_type=stream_type)

    def _launch_external_vlc(self, camera: dict) -> None:
        if not camera:
            return
        vlc_path = find_vlc()
        if not vlc_path:
            messagebox.showerror("VLC не найден", "Плеер VLC не найден в системе (C:\\Program Files\\VideoLAN\\VLC\\vlc.exe).")
            return
        ip = camera.get("ip")
        u = camera.get("username", self.default_username.get().strip())
        p = camera.get("password", self.default_password.get())
        try:
            uri = stream_uri(ip, u, p, stream_index=0)
            launch_video(vlc_path, uri, u, p)
            self.events.put(("log", f"Запущен внешний VLC: {ip}\n"))
        except Exception as exc:
            messagebox.showerror("Ошибка VLC", f"Не удалось получить поток RTSP для внешнего VLC: {exc}")

    def _open_in_browser(self) -> None:
        row = self._get_active_row()
        if row and row.get("ip"):
            webbrowser.open(f"http://{row['ip']}")

    # =========================================================================
    # CLIPBOARD & TABLE MANAGEMENT
    # =========================================================================

    def _copy_cell(self, field: str) -> None:
        row = self._get_active_row()
        if row:
            val = str(row.get(field, ""))
            self.clipboard_clear()
            self.clipboard_append(val)
            self.status.set(f"Скопировано: {val}")

    def _copy_row_text(self) -> None:
        row = self._get_active_row()
        if row:
            line = "\t".join(f"{k}: {v}" for k, v in row.items() if v and k != "camera")
            self.clipboard_clear()
            self.clipboard_append(line)
            self.status.set("Строка камеры скопирована в буфер.")

    def _remove_selected_from_table(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        ips_to_remove = {self.table.item(i, "values")[2] for i in sel}
        self.inventory = [r for r in self.inventory if r.get("ip") not in ips_to_remove]
        self._render_table()
        self.status.set(f"Удалено строк: {len(ips_to_remove)}.")

    def clear_inventory(self) -> None:
        if self.inventory and messagebox.askyesno("Очистить список", "Очистить текущий список найденных камер?"):
            self.vlc_player.stop()
            self.inventory = []
            self._render_table()
            self.status.set("Список камер очищен.")

    # =========================================================================
    # PROFILES & TRAFFIC ESTIMATION
    # =========================================================================

    def _on_profile_selected(self) -> None:
        k = self.selected_profile_key.get()
        p = profiles_mod.get_profile(k, self.custom_profiles)
        self.events.put(("log", f"Выбран видеопрофиль: {p['name']} ({p.get('description','')})\n"))
        self.status.set(f"Активный видеопрофиль: {p['name']}")

    def _open_traffic_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Оценка расчётного сетевого трафика")
        dlg.geometry("540x440")
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)

        checked = [r for r in self._visible_rows() if r.get("assign")]
        base_count = len(checked) if checked else len(self._visible_rows())
        count_var = tk.IntVar(value=max(1, base_count))
        profile_var = tk.StringVar(value=self.selected_profile_key.get())

        row_f = ttk.Frame(frame)
        row_f.pack(fill="x", pady=4)
        ttk.Label(row_f, text="Камер в расчёте:").pack(side="left")
        spin = ttk.Spinbox(row_f, from_=1, to=1000, textvariable=count_var, width=8)
        spin.pack(side="left", padx=8)
        if checked:
            ttk.Label(row_f, text=f"(отмечено: {len(checked)})", font=("Segoe UI", 8, "italic")).pack(side="left")
        else:
            ttk.Label(row_f, text=f"(все видимые: {len(self._visible_rows())})", font=("Segoe UI", 8, "italic")).pack(side="left")

        row_p = ttk.Frame(frame)
        row_p.pack(fill="x", pady=4)
        ttk.Label(row_p, text="Профиль качества:").pack(side="left")
        cb = ttk.Combobox(row_p, textvariable=profile_var, values=[k for k, _ in profiles_mod.list_profile_items(self.custom_profiles)], state="readonly", width=18)
        cb.pack(side="left", padx=8)

        res_frame = ttk.LabelFrame(frame, text="Расчётный битрейт и архив", padding=12)
        res_frame.pack(fill="both", expand=True, pady=12)

        lbl_main = ttk.Label(res_frame, font=("Segoe UI", 9))
        lbl_main.pack(anchor="w", pady=2)

        lbl_sub = ttk.Label(res_frame, font=("Segoe UI", 9))
        lbl_sub.pack(anchor="w", pady=2)

        lbl_total = ttk.Label(res_frame, font=("Segoe UI", 10, "bold"))
        lbl_total.pack(anchor="w", pady=4)

        lbl_storage = ttk.Label(res_frame, font=("Segoe UI", 9))
        lbl_storage.pack(anchor="w", pady=2)

        lbl_note = ttk.Label(
            res_frame,
            text="⚠️ Внимание: Это теоретический расчёт исходя из битрейта профилей,\nа не физический замер фактического трафика в коммутаторах сети.",
            font=("Segoe UI", 8, "italic"),
            foreground="#666666",
        )
        lbl_note.pack(anchor="w", pady=(8, 0))

        def update_calc(*_args):
            try:
                c = int(count_var.get())
            except Exception:
                c = 1
            p = profiles_mod.get_profile(profile_var.get(), self.custom_profiles)
            s = profiles_mod.estimate_traffic_summary(c, p)
            gb_day = round(s["total_main_mbps"] * 3600 * 24 / 8 / 1000, 1)
            lbl_main.config(text=f"• Основной поток ({s['main_kbps']} Кбит/с на камеру): ~{s['total_main_mbps']} Мбит/с")
            lbl_sub.config(text=f"• Дополнительный поток ({s['sub_kbps']} Кбит/с на камеру): ~{s['total_sub_mbps']} Мбит/с")
            lbl_total.config(text=f"• Суммарная нагрузка на сеть: ~{s['total_both_mbps']} Мбит/с")
            lbl_storage.config(text=f"• Расчётный архив (осн. поток 24ч): ~{gb_day} ГБ/сутки (~{round(gb_day * 30 / 1000, 2)} ТБ/мес)")

        count_var.trace_add("write", update_calc)
        profile_var.trace_add("write", update_calc)
        update_calc()

        def apply_to_selected():
            self.selected_profile_key.set(profile_var.get())
            self._on_profile_selected()
            p = profiles_mod.get_profile(profile_var.get(), self.custom_profiles)
            target_rows = checked if checked else self._visible_rows()
            for r in target_rows:
                r["profile"] = p["name"]
            self._render_table()
            dlg.destroy()

        btn_box = ttk.Frame(dlg, padding=8)
        btn_box.pack(fill="x")
        ttk.Button(btn_box, text="Применить профиль к камерам", command=apply_to_selected).pack(side="left", padx=4)
        ttk.Button(btn_box, text="Закрыть", command=dlg.destroy).pack(side="right", padx=4)

    def _import_profiles_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Импорт видеопрофилей",
            filetypes=[("Файлы профилей", "*.yaml *.yml *.json"), ("Все файлы", "*.*")],
        )
        if path:
            try:
                data = profiles_mod.load_profiles_file(pathlib.Path(path))
                self.custom_profiles.update(data)
                messagebox.showinfo("Импорт профилей", f"Успешно импортировано профилей: {len(data)}")
            except Exception as e:
                messagebox.showerror("Ошибка импорта", str(e))

    def _export_profiles_dialog(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Экспорт видеопрофилей",
            defaultextension=".yaml",
            filetypes=[("YAML файл", "*.yaml"), ("JSON файл", "*.json")],
        )
        if path:
            try:
                all_profiles = {**profiles_mod.DEFAULT_PROFILES, **self.custom_profiles}
                profiles_mod.save_profiles_file(pathlib.Path(path), all_profiles)
                messagebox.showinfo("Экспорт профилей", f"Профили сохранены в {path}")
            except Exception as e:
                messagebox.showerror("Ошибка экспорта", str(e))

    def _apply_profile_to_camera(self, camera: dict, profile_key: str) -> None:
        if not camera:
            return
        profile = profiles_mod.get_profile(profile_key, self.custom_profiles)
        streams = profiles_mod.profile_to_streams_config(profile)
        config_data = self._runtime_config()
        config_data["streams"] = streams

        def worker():
            writer = QueueWriter(self.events)
            with redirect_stdout(writer), redirect_stderr(writer):
                self.events.put(("log", f"\n[Видео] Применение профиля «{profile['name']}» к {camera.get('ip')}...\n"))
                cam_obj = copy.deepcopy(camera.get("camera", camera))
                cam_obj["ip"] = camera.get("ip")
                driver = self._make_driver(cam_obj, config_data)
                try:
                    if driver.connect():
                        ok = driver.apply_streams()
                        camera["codec"] = profile.get("codec", "H264")
                        self.events.put(("log", f"  ✓ Профиль применён: {'Успешно' if ok else 'Отказ'}\n"))
                        self.events.put(("refresh", None))
                    else:
                        self.events.put(("log", "  ✗ Не удалось подключиться к камере\n"))
                except Exception as exc:
                    self.events.put(("log", f"  ✗ Ошибка: {exc}\n"))
            self.events.put(("done", f"Настройка видео завершена для {camera.get('ip')}"))

        self._start_worker(f"Настройка видео {camera.get('ip')}", worker)

    # =========================================================================
    # MODAL DIALOGS (SETTINGS, ADDRESS PLAN, HISTORY)
    # =========================================================================

    def _open_settings_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Параметры работы и сети")
        dlg.geometry("560x520")
        dlg.transient(self)
        dlg.grab_set()

        notebook = ttk.Notebook(dlg, padding=8)
        notebook.pack(fill="both", expand=True)

        # Tab 1: Network & Scan
        t1 = ttk.Frame(notebook, padding=12)
        notebook.add(t1, text="Сеть и поиск")

        ttk.Label(t1, text="Маска подсети:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.network_mask, width=22).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(t1, text="Основной шлюз:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.gateway, width=22).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(t1, text="DNS сервер:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.dns_main, width=22).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Separator(t1, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

        ttk.Label(t1, text="Сканировать от IP:").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.scan_start, width=22).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(t1, text="Сканировать до IP:").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.scan_end, width=22).grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(t1, text="Сетевой интерфейс (IP ПК):").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(t1, textvariable=self.interface_ip, width=22).grid(row=6, column=1, sticky="w", pady=4)

        ttk.Checkbutton(t1, text="Поиск производителя (Sunell/Uniview мост)", variable=self.vendor_discovery).grid(row=7, column=0, columnspan=2, sticky="w", pady=8)

        # Tab 2: Credentials & Time
        t2 = ttk.Frame(notebook, padding=12)
        notebook.add(t2, text="Доступ и время")

        ttk.Label(t2, text="Логин по умолчанию:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(t2, textvariable=self.default_username, width=20).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(t2, text="Пароль по умолчанию:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(t2, textvariable=self.default_password, width=20, show="•").grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(t2, text="Исключения паролей:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(t2, textvariable=self.credential_exceptions, width=32).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Separator(t2, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

        ttk.Label(t2, text="NTP сервер:").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(t2, textvariable=self.ntp_server, width=22).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(t2, text="Часовой пояс:").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(t2, textvariable=self.cross_timezone, values=TIMEZONE_VALUES, state="readonly", width=24).grid(row=5, column=1, sticky="w", pady=4)

        btn_box = ttk.Frame(dlg, padding=(8, 8))
        btn_box.pack(fill="x")
        ttk.Button(btn_box, text="Готово", width=12, command=dlg.destroy).pack(side="right", padx=4)

    def _open_history_dialog(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title(f"История объекта «{self.current_project_name}»")
        dlg.geometry("900x550")
        dlg.transient(self)

        frame = ttk.Frame(dlg, padding=8)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        _settings, cameras = self.store.load_project(self.current_project_name)
        ttk.Label(header, text=f"Всего сохранено в базе данных: {len(cameras)} камер", font=("Segoe UI", 9, "bold")).pack(side="left")

        tree = ttk.Treeview(frame, columns=("ip", "model", "mac", "serial", "status", "updated"), show="headings")
        tree.heading("ip", text="IP адрес")
        tree.heading("model", text="Модель")
        tree.heading("mac", text="MAC")
        tree.heading("serial", text="Серийный номер")
        tree.heading("status", text="Статус")
        tree.heading("updated", text="Дата сохранения")

        tree.column("ip", width=120)
        tree.column("model", width=220)
        tree.column("mac", width=140)
        tree.column("serial", width=140)
        tree.column("status", width=160)
        tree.column("updated", width=140)

        tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=1, sticky="ns")

        for c in cameras:
            tree.insert("", "end", values=(
                c.get("ip",""), c.get("model",""), c.get("mac",""),
                c.get("serial_number") or c.get("device_id",""),
                c.get("status",""), c.get("updated_at", c.get("last_seen",""))
            ))

        btn_box = ttk.Frame(dlg, padding=8)
        btn_box.pack(fill="x")

        def load_selected_into_current():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Выбор", "Выберите камеры для загрузки в текущую таблицу.")
                return
            chosen_ips = {tree.item(i, "values")[0] for i in sel}
            added = 0
            for c in cameras:
                if c.get("ip") in chosen_ips:
                    if not any(r.get("ip") == c.get("ip") for r in self.inventory):
                        self.inventory.append(c)
                        added += 1
            self._render_table()
            dlg.destroy()
            messagebox.showinfo("Загрузка", f"Загружено камер в текущий список: {added}")

        ttk.Button(btn_box, text="Загрузить выбранные в текущий список", command=load_selected_into_current).pack(side="left", padx=4)
        ttk.Button(btn_box, text="Закрыть", command=dlg.destroy).pack(side="right", padx=4)

    def _open_camera_network_dialog(self) -> None:
        row = self._get_active_row()
        if not row:
            return
        dlg = tk.Toplevel(self)
        dlg.title(f"Сетевые настройки — {row.get('ip')}")
        dlg.geometry("380x280")
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)

        new_ip_var = tk.StringVar(value=row.get("new_ip") or row.get("ip"))
        mask_var = tk.StringVar(value=row.get("mask") or self.network_mask.get())
        gw_var = tk.StringVar(value=row.get("gateway_read") or self.gateway.get())
        dns_var = tk.StringVar(value=self.dns_main.get())

        ttk.Label(frame, text="Новый IP адрес:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=new_ip_var, width=20).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Маска подсети:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=mask_var, width=20).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Основной шлюз:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=gw_var, width=20).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="DNS сервер:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=dns_var, width=20).grid(row=3, column=1, sticky="w", pady=4)

        def apply_change():
            row["new_ip"] = new_ip_var.get().strip()
            row["assign"] = True
            self._render_table()
            dlg.destroy()

        ttk.Button(frame, text="Сохранить в план", command=apply_change).grid(row=5, column=0, columnspan=2, pady=16)

    def _open_camera_video_dialog(self) -> None:
        row = self._get_active_row()
        if not row:
            return
        dlg = tk.Toplevel(self)
        dlg.title(f"Видео и кодек — {row.get('ip')}")
        dlg.geometry("420x300")
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)

        codec_var = tk.StringVar(value=row.get("codec") or "H264")
        res_var = tk.StringVar(value="1920x1080")
        fps_var = tk.StringVar(value="15")
        bitrate_var = tk.StringVar(value="3000")

        ttk.Label(frame, text="Кодек:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=codec_var, values=["H264", "H265"], state="readonly", width=18).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Разрешение:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=res_var, values=["2560x1440", "1920x1080", "1280x720"], state="readonly", width=18).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Частота кадров (FPS):").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=fps_var, values=["10", "15", "20", "25"], state="readonly", width=18).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Битрейт (Кбит/с):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=bitrate_var, width=20).grid(row=3, column=1, sticky="w", pady=4)

        def apply_video():
            custom_p = {
                "id": "single_custom",
                "name": "Индивидуальный",
                "codec": codec_var.get(),
                "main": {
                    "id": 1, "name": "stream1", "enabled": True,
                    "resolution": res_var.get(), "fps": int(fps_var.get()),
                    "codec": codec_var.get(), "bitrate": int(bitrate_var.get()),
                    "bitrate_type": "CBR", "gop": int(fps_var.get()) * 2, "quality": 5,
                },
                "sub": {
                    "id": 2, "name": "stream2", "enabled": True,
                    "resolution": "720x576", "fps": 10,
                    "codec": codec_var.get(), "bitrate": 512,
                    "bitrate_type": "CBR", "gop": 20, "quality": 4,
                },
            }
            dlg.destroy()
            self._apply_profile_to_camera(row, "custom")

        ttk.Button(frame, text="Применить к камере", command=apply_video).grid(row=5, column=0, columnspan=2, pady=16)

    # =========================================================================
    # BACKEND WORKERS & PIPELINES
    # =========================================================================

    def start_scan(self) -> None:
        if self.running:
            return
        self.btn_scan.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.cancel_requested.clear()
        self.running = True
        self.status.set("Поиск камер в сети...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        # Fast scan: do not auto deep-read settings or open streams
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        start_time = time.monotonic()
        writer = QueueWriter(self.events)
        with redirect_stdout(writer), redirect_stderr(writer):
            self.events.put(("log", "\n=== Запуск быстрого поиска камер ===\n"))
            vendor_rows = []
            if self.vendor_discovery.get():
                try:
                    self.events.put(("status", "Фирменный опрос оборудования..."))
                    vendor_rows = self._run_vendor_discovery(
                        timeout=3.0,
                        sunell_only=False,
                        interface_ip=self.interface_ip.get().strip(),
                    )
                    self.events.put(("log", f"Фирменный протокол обнаружил: {len(vendor_rows)} камер\n"))
                except Exception as exc:
                    self.events.put(("log", f"Ошибка поиска производителя: {exc}\n"))

            # Build initial items
            new_items = []
            for vr in vendor_rows:
                ip = vr.get("current_ip")
                if not ip:
                    continue
                camera = self._camera_template_for_ip(ip)
                profile = self._profile_for_vendor_row(vr, camera.get("profile", "cross"))
                camera["profile"] = profile
                item = {
                    "ip": ip,
                    "assign": False,
                    "new_ip": vr.get("new_ip", ""),
                    "model": vr.get("model", ""),
                    "mac": vr.get("mac", ""),
                    "device_id": vr.get("device_id", ""),
                    "serial_number": vr.get("serial_number", ""),
                    "mask": vr.get("mask", ""),
                    "gateway_read": vr.get("gateway", ""),
                    "ntp_read": "",
                    "timezone_read": "",
                    "codec": "",
                    "profile": profile,
                    "status": vr.get("protocol", "производитель"),
                    "seen_passes": 1,
                    "online": True,
                    "camera": camera,
                    "vendor_row": vr,
                }
                new_items.append(item)

            # Fast probe range for ONVIF / LAPI if specified and different
            start_ip_text = self.scan_start.get().strip()
            end_ip_text = self.scan_end.get().strip()
            if start_ip_text and end_ip_text:
                try:
                    s_addr = int(ipaddress.IPv4Address(start_ip_text))
                    e_addr = int(ipaddress.IPv4Address(end_ip_text))
                    if e_addr >= s_addr:
                        target_ips = [str(ipaddress.IPv4Address(val)) for val in range(s_addr, min(e_addr + 1, s_addr + 255))]
                        # Exclude already found
                        found_ips = {it["ip"] for it in new_items}
                        probe_ips = [ip for ip in target_ips if ip not in found_ips]
                        if probe_ips:
                            self.events.put(("status", f"Быстрая проверка диапазона {len(probe_ips)} адресов..."))
                            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                                futures = {pool.submit(self._probe_fast_ip, ip): ip for ip in probe_ips}
                                for f in concurrent.futures.as_completed(futures):
                                    if self.cancel_requested.is_set():
                                        break
                                    res = f.result()
                                    if res:
                                        new_items.append(res)
                except Exception as exc:
                    self.events.put(("log", f"Диапазон IP пропущен: {exc}\n"))

        elapsed = round(time.monotonic() - start_time, 1)
        self.events.put(("merge_scan", new_items))
        self.events.put(("done", f"Поиск завершён за {elapsed}с. Найдено камер: {len(new_items)}."))

    def _probe_fast_ip(self, ip: str) -> dict:
        """Fast non-blocking probe for a single IP address."""
        if not self._host_replies(ip):
            return None
        camera = self._camera_template_for_ip(ip)
        # Fast port check
        return {
            "ip": ip,
            "assign": False,
            "new_ip": "",
            "model": "IP Camera",
            "mac": "",
            "device_id": "",
            "serial_number": "",
            "mask": "",
            "gateway_read": "",
            "ntp_read": "",
            "timezone_read": "",
            "codec": "",
            "profile": camera.get("profile", "cross"),
            "status": "онлайн (ping)",
            "seen_passes": 1,
            "online": True,
            "camera": camera,
        }

    def _probe_onvif(self, ip: str, username: str = None, password: str = None) -> tuple[bool, str, dict]:
        """Wrapper around camera_inventory_reader.probe_onvif_camera."""
        u = username or self.default_username.get().strip()
        p = password or self.default_password.get()
        return probe_onvif_camera(ip, u, p)

    # =========================================================================
    # SEQUENTIAL ASSIGNMENT FROM 192.168.0.250 (TESTED & STABLE)
    # =========================================================================

    def apply_default_network_plan(self) -> None:
        if self.running:
            return
        assigned_rows = [row for row in self._visible_rows() if row.get("assign") and row.get("new_ip")]
        if not assigned_rows:
            messagebox.showwarning("Нет плана", "Сначала выберите камеры и укажите новые адреса («Сформировать план»).")
            return

        # Confirmation with preview
        p = profiles_mod.get_profile(self.selected_profile_key.get(), self.custom_profiles)
        video_note = f"Включено («{p['name']}»)" if self.apply_video_profile.get() else "Отключено (без изменений)"
        preview = "\n".join(f"192.168.0.250  →  {r['new_ip']}" for r in assigned_rows[:6])
        if len(assigned_rows) > 6:
            preview += f"\n... ещё {len(assigned_rows) - 6}"

        if not messagebox.askyesno(
            "Назначить 192.168.0.250",
            f"Камер к обработке: {len(assigned_rows)}\n"
            f"Применение видеопрофиля: {video_note}\n\n"
            f"План перевода:\n{preview}\n\n"
            f"Программа будет последовательно настраивать каждую появляющуюся на 192.168.0.250 камеру, "
            f"устанавливать время/NTP и переводить на новый IP. Начать?",
        ):
            return

        self._start_worker("Назначение с 192.168.0.250", self._default_ip_worker, assigned_rows)

    def _wait_for_default_ip(self, max_wait: float = 120.0) -> bool:
        """Wait until DEFAULT_CAMERA_IP responds to ping, allowing cameras to boot or switch to update ARP."""
        start = time.monotonic()
        while not self.cancel_requested.is_set():
            self._flush_arp(DEFAULT_CAMERA_IP)
            if self._host_replies(DEFAULT_CAMERA_IP):
                return True
            elapsed = int(time.monotonic() - start)
            if max_wait > 0 and elapsed >= max_wait:
                return False
            self.events.put(("status", f"Ожидание камеры на {DEFAULT_CAMERA_IP}... ({elapsed}с, нажмите «Остановить» для выхода)"))
            self.cancel_requested.wait(2.0)
        return False

    def _default_ip_worker(self, rows: list[dict]) -> None:
        results = []
        targets = [row["new_ip"] for row in rows]
        occupied = self._check_target_pool(targets)
        if self.cancel_requested.is_set():
            self.events.put(("done", "Проверка плана остановлена."))
            return

        occupied_set = set(occupied)
        available = []
        for row in rows:
            if row["new_ip"] not in occupied_set:
                available.append(row)
            else:
                results.append({"timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                                "mac": "", "device_id": "", "source_ip": DEFAULT_CAMERA_IP,
                                "target_ip": row["new_ip"], "status": "skipped_occupied",
                                "message": "Адрес занят; команда не отправлялась"})
                row.update(status="целевой адрес занят — пропущен", assign=False, new_ip="")

        rows = available
        self.events.put(("refresh", None))
        if not rows:
            self._write_default_assignment_log(results)
            self.events.put(("done", f"Все адреса в пуле заняты."))
            return

        completed = 0
        self._last_default_profile = None
        writer = QueueWriter(self.events)
        self.events.put(("progress", (0, len(rows))))

        for index, row in enumerate(rows, start=1):
            if self.cancel_requested.is_set():
                break
            target_ip = row["new_ip"]

            # Ожидание ответа камеры на 192.168.0.250 (до 120 сек)
            self.events.put(("status", f"Камера {index}/{len(rows)}: ожидание появления {DEFAULT_CAMERA_IP}..."))
            if not self._wait_for_default_ip(max_wait=120.0):
                if self.cancel_requested.is_set():
                    break
                self.events.put(("log", f"  ⚠ 192.168.0.250 не отвечает (таймаут 120с). Ожидание завершено.\n"))
                row["status"] = "192.168.0.250 не отвечает"
                break

            result = {
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "mac": row.get("mac", ""),
                "device_id": row.get("device_id", ""),
                "source_ip": DEFAULT_CAMERA_IP,
                "target_ip": target_ip,
                "status": "failed",
                "message": "",
            }
            mac_label = row.get("mac") or "неизвестен"
            self.events.put(("status", f"Камера {index}/{len(rows)}: 192.168.0.250 → {target_ip}"))
            self.events.put(("log", f"[{index}/{len(rows)}] 192.168.0.250 → {target_ip} (MAC: {mac_label})\n"))
            self._flush_arp(DEFAULT_CAMERA_IP)
            self._flush_arp(target_ip)

            with redirect_stdout(writer), redirect_stderr(writer):
                sent = self._apply_default_network_row(row)

            if not sent:
                result["message"] = row.get("status", "команда отклонена")
                results.append(result)
                self.events.put(("log", f"  ✗ Не удалось отправить смену IP: {row.get('status')}\n"))
                self.events.put(("progress", (index, len(rows))))
                self.events.put(("refresh", None))
                continue

            # Команда успешно отправлена
            completed += 1
            row["ip"] = target_ip
            row["new_ip"] = ""
            row["online"] = False
            row["status"] = "команда отправлена"
            row["last_seen"] = dt.datetime.now().isoformat(timespec="seconds")
            if row.get("camera"):
                row["camera"]["ip"] = target_ip

            result["status"] = "sent"
            result["message"] = "команда смены IP успешно отправлена"
            results.append(result)

            self.events.put(("log", f"  ✓ Команда для {target_ip} отправлена. Сброс ARP и пауза 3с...\n"))
            self._flush_arp(DEFAULT_CAMERA_IP)
            self._flush_arp(target_ip)
            self.events.put(("progress", (index, len(rows))))
            self.events.put(("refresh", None))

            # Технологическая пауза для переключения камеры и сброса коммутатора
            self.cancel_requested.wait(3)

        self._write_default_assignment_log(results)
        msg = f"Назначение завершено: отправлено {completed}/{len(rows)}."
        self.events.put(("done", msg))

    def _apply_default_network_row(self, row):
        target = row["new_ip"]
        if self.cancel_requested.is_set():
            return False
        if self._host_replies(target):
            row["status"] = "целевой IP занят"
            return False

        config = self._runtime_config()
        config.setdefault("network", {})["ip_address"] = target

        # 1. Приоритетный способ: Sunell по MAC
        if (not self.cancel_requested.is_set() and row.get("protocol") == "sunell"
                and row.get("mac") and row.get("device_id")):
            tz_val = config.get("timezone", {}).get("timezone")
            ntp_val = config.get("ntp", {}).get("server")
            if tz_val or ntp_val:
                try:
                    sunell_cam = copy.deepcopy(row.get("camera", row))
                    sunell_cam.update(ip=DEFAULT_CAMERA_IP, profile="cross")
                    sunell_drv = self._make_driver(sunell_cam, config)
                    if sunell_drv.connect():
                        if tz_val: sunell_drv.apply_timezone()
                        if ntp_val: sunell_drv.apply_ntp()
                except Exception:
                    pass
            username, password = self._credential_for_ip(DEFAULT_CAMERA_IP, self.active_credential_rules,
                                                        (self.default_username.get().strip(), self.default_password.get()))
            network = config["network"]
            sent, message = self._send_targeted_sunell_ip(row, target, username, password,
                network["subnet_mask"], network["gateway"], network.get("dns_main", ""))
            if sent:
                row["status"] = "фирменная команда отправлена"
                return True

        # 2. Перебор HTTP драйверов
        model = str(row.get("model", "")).lower()
        last_success = getattr(self, "_last_default_profile", None)
        preferred = last_success or ("apix_s8" if "/s8" in model else "apix_e8" if "/e8" in model else row.get("profile", "cross"))
        profiles = list(dict.fromkeys([preferred, "apix_e8", "cross", "apix_s8"]))
        profiles = [p for p in profiles if p in {"cross", "apix_e8", "apix_s8"}]
        labels = {"cross": "CROSS / WEB", "apix_e8": "APIX E8 / LAPI", "apix_s8": "APIX S8 / CGI"}

        for index, profile in enumerate(profiles, 1):
            if self.cancel_requested.is_set():
                row["status"] = "остановлено до смены IP"
                return False
            label = labels[profile]
            camera = copy.deepcopy(row.get("camera", row))
            camera.update(ip=DEFAULT_CAMERA_IP, profile=profile, network_autodetect=True)
            driver = self._make_driver(camera, config)
            try:
                connected = driver.connect()
            except Exception as exc:
                continue
            if not connected or getattr(driver, "onvif_fallback", False):
                continue
            if self.cancel_requested.is_set():
                row["status"] = "остановлено до смены IP"
                return False

            # Настройка времени и NTP до смены IP
            tz_val = config.get("timezone", {}).get("timezone")
            if tz_val:
                try: driver.apply_timezone()
                except Exception: pass

            ntp_val = config.get("ntp", {}).get("server")
            if ntp_val:
                try: driver.apply_ntp()
                except Exception: pass

            # Опциональное применение видеопрофиля
            if self.apply_video_profile.get():
                try:
                    p = profiles_mod.get_profile(self.selected_profile_key.get(), self.custom_profiles)
                    config["streams"] = profiles_mod.profile_to_streams_config(p)
                    driver.apply_streams()
                except Exception:
                    pass

            try:
                sent = driver.apply_network()
            except configurator.requests.RequestException as exc:
                response = getattr(exc, "response", None)
                code = response.status_code if response is not None else None
                if code in {400, 401, 403, 404, 405, 501}:
                    continue
                row["status"] = f"{label}: команда отправлена"
                self._last_default_profile = profile
                return True
            if sent:
                self._last_default_profile = profile
                row["profile"] = profile
                row["status"] = f"команда отправлена: {label}"
                return True

        row["status"] = "ни один способ настройки сети не подошёл"
        return False

    # =========================================================================
    # PARALLEL BATCH OPERATIONS FOR INDEPENDENT CAMERAS
    # =========================================================================

    def apply_selected(self) -> None:
        if self.running:
            return
        selected = [r for r in self._visible_rows() if r.get("assign")]
        if not selected:
            messagebox.showwarning("Выбор", "Отметьте камеры для применения настроек (колонка «Назначить»).")
            return

        config_data = self._runtime_config()
        if self.apply_video_profile.get():
            p = profiles_mod.get_profile(self.selected_profile_key.get(), self.custom_profiles)
            config_data["streams"] = profiles_mod.profile_to_streams_config(p)

        self._start_worker("Применение настроек", self._batch_apply_worker, selected, config_data)

    def _batch_apply_worker(self, rows: list[dict], config_data: dict) -> None:
        total = len(rows)
        self.events.put(("progress", (0, total)))
        self.events.put(("log", f"\n=== Пакетное применение настроек ({total} камер) ===\n"))

        def process_camera(row):
            if self.cancel_requested.is_set():
                return
            ip = row.get("ip")
            cam_obj = copy.deepcopy(row.get("camera", row))
            cam_obj["ip"] = ip
            driver = self._make_driver(cam_obj, config_data)

            self.events.put(("status", f"Подключение к {ip}..."))
            try:
                if not driver.connect():
                    row["status"] = "ошибка подключения"
                    self.events.put(("log", f"[{ip}] ✗ Не удалось подключиться\n"))
                    return
                # Time & NTP
                if config_data.get("timezone"): driver.apply_timezone()
                if config_data.get("ntp", {}).get("server"): driver.apply_ntp()
                if self.apply_video_profile.get(): driver.apply_streams()
                row["status"] = "настройки применены"
                self.events.put(("log", f"[{ip}] ✓ Настройки успешно обновлены\n"))
            except Exception as exc:
                row["status"] = f"ошибка: {exc}"
                self.events.put(("log", f"[{ip}] ✗ Ошибка: {exc}\n"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(process_camera, r) for r in rows]
            for i, f in enumerate(concurrent.futures.as_completed(futures), 1):
                self.events.put(("progress", (i, total)))
                self.events.put(("refresh", None))

        self.events.put(("done", f"Обработка завершена для {total} камер."))

    def read_selected_details(self) -> None:
        selected = [r for r in self._visible_rows() if r.get("assign")]
        if not selected:
            messagebox.showwarning("Выбор", "Отметьте камеры для чтения настроек.")
            return

        def worker():
            total = len(selected)
            self.events.put(("progress", (0, total)))
            self.events.put(("log", f"\n=== Чтение подробных настроек ({total} камер) ===\n"))
            for i, r in enumerate(selected, 1):
                if self.cancel_requested.is_set(): break
                ip = r.get("ip")
                u, p = self._credential_for_ip(ip, self.active_credential_rules, (self.default_username.get().strip(), self.default_password.get()))
                self.events.put(("status", f"Чтение {i}/{total}: {ip}..."))
                try:
                    res = read_camera_settings(ip, u, p)
                    if res:
                        r.update({k: v for k, v in res.items() if v})
                        r["status"] = "настройки прочитаны"
                        self.events.put(("log", f"[{ip}] ✓ Настройки прочитаны (кодек: {r.get('codec','?')})\n"))
                except Exception as exc:
                    self.events.put(("log", f"[{ip}] ✗ Ошибка чтения: {exc}\n"))
                self.events.put(("progress", (i, total)))
                self.events.put(("refresh", None))
            self.events.put(("done", f"Чтение настроек завершено ({total} камер)."))

        self._start_worker("Чтение настроек", worker)

    def check_selected_ping(self) -> None:
        selected = [r for r in self._visible_rows() if r.get("assign")] or self._visible_rows()
        if not selected:
            return

        def worker():
            total = len(selected)
            self.events.put(("progress", (0, total)))
            for i, r in enumerate(selected, 1):
                if self.cancel_requested.is_set(): break
                ip = r.get("ip")
                online = self._host_replies(ip)
                r["online"] = online
                r["status"] = "онлайн" if online else "не отвечает"
                self.events.put(("progress", (i, total)))
                self.events.put(("refresh", None))
            self.events.put(("done", "Проверка доступности завершена."))

        self._start_worker("Проверка доступности", worker)

    def _read_single_camera_details(self) -> None:
        row = self._get_active_row()
        if not row: return
        ip = row.get("ip")
        u, p = self._credential_for_ip(ip, self.active_credential_rules, (self.default_username.get().strip(), self.default_password.get()))
        def worker():
            self.events.put(("log", f"\n[Чтение] Запрос настроек с {ip}...\n"))
            try:
                res = read_camera_settings(ip, u, p)
                if res:
                    row.update({k: v for k, v in res.items() if v})
                    row["status"] = "настройки прочитаны"
                    self.events.put(("log", f"[{ip}] ✓ Настройки успешно получены\n"))
                    self.events.put(("refresh", None))
            except Exception as e:
                self.events.put(("log", f"[{ip}] ✗ Ошибка: {e}\n"))
            self.events.put(("done", f"Чтение {ip} завершено"))
        self._start_worker(f"Чтение {ip}", worker)

    def _ping_single_camera(self) -> None:
        row = self._get_active_row()
        if not row: return
        ip = row.get("ip")
        online = self._host_replies(ip)
        row["online"] = online
        row["status"] = "онлайн" if online else "не отвечает"
        self._render_table()
        self.status.set(f"{ip}: {'Онлайн (отвечает)' if online else 'Недоступен (таймаут)'}")

    # =========================================================================
    # HELPERS & SYSTEM CALLS
    # =========================================================================

    def _runtime_config(self) -> dict:
        data = copy.deepcopy(self.config_data)
        ntp_srv = self.ntp_server.get().strip()
        data.setdefault("ntp", {})["server"] = ntp_srv
        data.setdefault("ntp", {})["enabled"] = bool(ntp_srv)
        timezone_text = self.cross_timezone.get().strip()
        timezone = data.setdefault("timezone", {})
        timezone["timezone"] = timezone_text
        match = re.search(r"GMT([+-])(\d{2}):(\d{2})", timezone_text)
        if match:
            sign, hours, minutes = match.groups()
            offset = f"{sign}{hours}:{minutes}"
            timezone["timezone_offset"] = offset
            timezone["timezone_lapi"] = f"GMT{offset}"
            # POSIX TZ (S8): GMT+9 -> UTC-9, GMT+3 -> UTC-3
            posix_sign = "-" if sign == "+" else "+"
            tz_min = f":{minutes}" if minutes != "00" else ""
            timezone["timezone_utc"] = f"UTC{posix_sign}{int(hours)}{tz_min}"
            timezone["timezone_name"] = "RUS"
        network = data.setdefault("network", {})
        network["subnet_mask"] = self.network_mask.get().strip()
        network["gateway"] = self.gateway.get().strip()
        network["dns_main"] = self.dns_main.get().strip()
        return data

    def _make_driver(self, camera: dict, config_data: dict):
        camera = copy.deepcopy(camera)
        username, password = self._credential_for_ip(
            camera["ip"],
            self.active_credential_rules,
            (self.default_username.get().strip(), self.default_password.get()),
        )
        camera["username"] = username
        camera["password"] = password
        profile_name = camera.get("profile", "cross")
        profile = configurator.load_profile(profile_name)
        driver_class = configurator.DRIVERS.get(profile.get("driver"))
        if driver_class is None:
            raise ValueError(f"Неизвестный драйвер: {profile.get('driver')}")
        return driver_class(profile, camera, config_data)

    def _start_worker(self, title: str, func, *args):
        if self.running:
            return
        self.btn_scan.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.cancel_requested.clear()
        self.running = True
        self.status.set(f"{title}...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        threading.Thread(target=func, args=args, daemon=True).start()

    def _request_stop(self):
        self.cancel_requested.set()
        self.btn_stop.configure(state="disabled")
        self.status.set("Остановка: завершаю текущие операции...")

    def _drain_events(self):
        while not self.events.empty():
            kind, payload = self.events.get_nowait()
            if kind == "status":
                self.status.set(payload)
            elif kind == "log":
                self.log.insert("end", payload)
                if self.autoscroll_log.get():
                    self.log.see("end")
            elif kind == "progress":
                cur, tot = payload
                self.progress.configure(mode="determinate", maximum=tot, value=cur)
            elif kind == "refresh":
                self._render_table()
            elif kind == "merge_scan":
                self.inventory = payload
                self._render_table()
            elif kind == "done":
                self.running = False
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
                self.btn_scan.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.status.set(payload)
                self._render_table()
        self.after(50, self._drain_events)

    def _on_close(self):
        self.vlc_player.stop()
        self._save_current_project()
        self.destroy()

    @staticmethod
    def _is_admin() -> bool:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    @classmethod
    def _flush_arp(cls, ip: str) -> bool:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            subprocess.run(["arp", "-d", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup, check=False)
            subprocess.run(["netsh", "interface", "ip", "delete", "arpcache"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup, check=False)
            return True
        except Exception:
            return False

    @staticmethod
    def _host_replies(ip: str, timeout: float = 1.0) -> bool:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            res = subprocess.run(["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, startupinfo=startup, check=False)
            return b"TTL=" in res.stdout
        except Exception:
            return False

    def _check_target_pool(self, targets):
        occupied = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            pending = {pool.submit(self._host_replies, ip): ip for ip in targets}
            for future in concurrent.futures.as_completed(pending):
                if future.result():
                    occupied.append(pending[future])
        return occupied

    def _write_default_assignment_log(self, results: list[dict]) -> None:
        fields = ["timestamp", "mac", "device_id", "source_ip", "target_ip", "status", "message"]
        write_header = not DEFAULT_ASSIGNMENT_LOG.exists() or DEFAULT_ASSIGNMENT_LOG.stat().st_size == 0
        with DEFAULT_ASSIGNMENT_LOG.open("a", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerows(results)

    def _send_targeted_sunell_ip(self, row, target_ip, username, password, mask, gateway, dns):
        bridge = APP_DIR / "vendor_bridge" / "VendorBridge.exe"
        vendor_dir = APP_DIR / "vendor"
        if not bridge.exists() or not vendor_dir.is_dir():
            return False, "VendorBridge не найден"
        command = [str(bridge), "set-sunell", DEFAULT_CAMERA_IP, target_ip, mask, gateway, dns, row.get("model",""), row.get("mac",""), row.get("device_id",""), username]
        env = dict(os.environ, ESC_VENDOR_DIR=str(vendor_dir))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            p = subprocess.run(command, input=password + "\n", capture_output=True, text=True, timeout=30, cwd=vendor_dir, env=env, startupinfo=startup)
            return p.returncode == 0, (p.stderr or p.stdout).strip()
        except Exception as e:
            return False, str(e)

    def _run_vendor_discovery(self, timeout=3.0, sunell_only=False, interface_ip=""):
        output = APP_DIR / "vendor_camera_inventory_gui.csv"
        cmd = [sys.executable, str(APP_DIR / "esc_vendor_bulk.py"), "--discover-only", "--timeout", str(timeout), "--output", str(output)]
        if sunell_only: cmd.extend(["--skip-onvif", "--skip-dynacolor", "--repeats", "2"])
        if interface_ip: cmd.extend(["--interface-ip", interface_ip])
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if output.is_file():
            with output.open(newline="", encoding="utf-8-sig") as s:
                return list(csv.DictReader(s))
        return []

    def _credential_for_ip(self, ip, rules, default):
        for pattern, u, p in rules:
            if re.search(pattern, ip):
                return u, p
        return default

    def _profile_for_vendor_row(self, vr, fallback="cross"):
        proto = vr.get("protocol","").lower()
        model = vr.get("model","").lower()
        if "sunell" in proto: return "cross"
        if "/s8" in model or model.endswith("s8"): return "apix_s8"
        if "onvif" in proto: return "apix_e8"
        return fallback

    def _camera_template_for_ip(self, ip):
        return {"ip": ip, "username": self.default_username.get().strip(), "password": self.default_password.get(), "profile": "cross"}

    # =========================================================================
    # CONFIG & PROJECT STORE
    # =========================================================================

    def _load_config_file(self, path: pathlib.Path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.config_data = yaml.safe_load(f) or {}
            net = self.config_data.get("network", {})
            if net.get("subnet_mask"): self.network_mask.set(net["subnet_mask"])
            if net.get("gateway"): self.gateway.set(net["gateway"])
            if net.get("dns_main"): self.dns_main.set(net["dns_main"])
            ntp = self.config_data.get("ntp", {})
            if ntp.get("server"): self.ntp_server.set(ntp["server"])
            tz = self.config_data.get("timezone", {})
            if tz.get("timezone"): self.cross_timezone.set(tz["timezone"])
        except Exception:
            pass

    def _load_projects(self):
        names = self.store.list_projects()
        if not names:
            self.store.ensure_project("По умолчанию")
            names = ["По умолчанию"]
        self.project_box.configure(values=names)
        self.project_name.set(names[0])
        self._load_project(names[0])

    def _load_project(self, name: str):
        self.current_project_name = name
        settings, _cameras = self.store.load_project(name)
        if settings:
            if "network_mask" in settings: self.network_mask.set(settings["network_mask"])
            if "gateway" in settings: self.gateway.set(settings["gateway"])
            if "dns_main" in settings: self.dns_main.set(settings["dns_main"])
            if "ntp_server" in settings: self.ntp_server.set(settings["ntp_server"])
            if "cross_timezone" in settings: self.cross_timezone.set(settings["cross_timezone"])
            if "target_start" in settings: self.target_start.set(settings["target_start"])
            if "target_end" in settings: self.target_end.set(settings["target_end"])
        # Fast clean startup: table starts empty
        self.inventory = []
        self._render_table()
        self.status.set(f"Объект «{name}» загружен. Нажмите «🔍 Поиск» для сканирования.")

    def _save_current_project(self):
        if self.current_project_name:
            settings = {
                "network_mask": self.network_mask.get(),
                "gateway": self.gateway.get(),
                "dns_main": self.dns_main.get(),
                "ntp_server": self.ntp_server.get(),
                "cross_timezone": self.cross_timezone.get(),
                "target_start": self.target_start.get(),
                "target_end": self.target_end.get(),
                "default_username": self.default_username.get(),
            }
            self.store.save_project(self.current_project_name, settings, self.inventory)

    def _project_changed(self, _event=None):
        name = self.project_name.get().strip()
        if name and name != self.current_project_name:
            self._save_current_project()
            self._load_project(name)

    def _new_project(self):
        name = simpledialog.askstring("Новый объект", "Введите название объекта:", parent=self)
        if name and name.strip():
            name = name.strip()
            self._save_current_project()
            self.store.ensure_project(name)
            names = self.store.list_projects()
            self.project_box.configure(values=names)
            self.project_name.set(name)
            self._load_project(name)

    def _delete_project(self):
        name = self.project_name.get().strip()
        if name and messagebox.askyesno("Удалить", f"Удалить объект «{name}»?"):
            self.store.delete_project(name)
            self._load_projects()

    def build_address_plan(self):
        dlg = tk.Toplevel(self)
        dlg.title("Формирование адресного плана")
        dlg.geometry("450x360")
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Начальный целевой IP:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.target_start, width=20).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Конечный целевой IP:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.target_end, width=20).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Separator(frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=10)

        ttk.Checkbutton(frame, text="Применить видеопрофиль к камерам", variable=self.apply_video_profile).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(frame, text="Профиль видео:").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.selected_profile_key, values=[k for k, _ in profiles_mod.list_profile_items()], state="readonly", width=18).grid(row=4, column=1, sticky="w", pady=4)

        def generate_plan():
            try:
                s = int(ipaddress.IPv4Address(self.target_start.get().strip()))
                e = int(ipaddress.IPv4Address(self.target_end.get().strip()))
                if e < s: raise ValueError("Конечный адрес меньше начального.")
                targets = [str(ipaddress.IPv4Address(val)) for val in range(s, e + 1)]
            except Exception as ex:
                messagebox.showerror("Ошибка пула", str(ex))
                return

            assigned_rows = [r for r in self._visible_rows() if r.get("assign")]
            if not assigned_rows:
                assigned_rows = self._visible_rows()

            for i, r in enumerate(assigned_rows):
                r["assign"] = True
                r["new_ip"] = targets[i] if i < len(targets) else ""
            self._render_table()
            dlg.destroy()
            messagebox.showinfo("Адресный план", f"Сформирован план для {min(len(assigned_rows), len(targets))} камер.")

        ttk.Button(frame, text="Применить план к таблице", command=generate_plan).grid(row=6, column=0, columnspan=2, pady=16)

    def _export_dialog(self, fmt: str):
        path = filedialog.asksaveasfilename(
            title=f"Экспорт таблицы в {fmt.upper()}",
            defaultextension=f".{fmt}",
            filetypes=[(f"Файл {fmt.upper()}", f"*.{fmt}")],
        )
        if path:
            rows = [r for r in self._visible_rows() if r.get("assign")] or self._visible_rows()
            try:
                export_rows(path, rows, self.current_project_name, "таблица")
                messagebox.showinfo("Экспорт", f"Экспортировано {len(rows)} строк в {path}")
            except Exception as e:
                messagebox.showerror("Ошибка экспорта", str(e))


if __name__ == "__main__":
    app = CameraGui()
    app.mainloop()
