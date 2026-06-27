# -*- coding: utf-8 -*-
import glob
import json
import os
import re
import socket
import sys
import zipfile
from dataclasses import dataclass, field

PRINTER_IP = "192.168.1.100"
PRINTER_PORT = 9100
APP_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
APP_ICON_PATH = os.path.join(RESOURCE_DIR, "app.ico")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".zpl_printer_settings.json")
VARIABLE_PATTERN = re.compile(r"@([^@\r\n]+?)@")
FIELD_DATA_PATTERN = re.compile(r"(\^FD)(.*?)(\^FS)", re.IGNORECASE | re.DOTALL)
CODE128_FIELD_PATTERN = re.compile(r"\^BC", re.IGNORECASE)
CODE128_ESCAPE_PATTERN = re.compile(r">[0-9:;<=>?]")


def ensure_qt_plugin_path():
    try:
        import PyQt5
        root = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
        platforms = os.path.join(root, "platforms")
        if os.path.exists(platforms):
            os.environ.setdefault("QT_PLUGIN_PATH", root)
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", platforms)
    except Exception:
        pass


def check_dependencies():
    missing = []
    required = [("PyQt5", "PyQt5")]
    if sys.platform.startswith("win"):
        required.append(("win32print", "pywin32"))
    for module, package in required:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


missing = check_dependencies()
if missing:
    print("缺少依赖库：" + ", ".join(missing))
    print("请安装：python -m pip install " + " ".join(missing))
    sys.exit(1)

ensure_qt_plugin_path()

from PyQt5.QtCore import QEvent, QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QIntValidator
from PyQt5.QtWidgets import (
    QAction, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTextEdit, QVBoxLayout, QWidget,
)

THEME_PRESETS = {
    "蓝色": "#2B5C8A",
    "绿色": "#2E7D32",
    "橙色": "#EF6C00",
    "紫色": "#6A4C93",
    "红色": "#C62828",
    "深灰": "#374151",
}
FONT_SIZES = {"small": 12, "medium": 13, "large": 15}
LEGACY_FONT_SIZE_ALIASES = {
    "small": {"small", "\u704f?"},
    "medium": {"medium", "\u6d93?"},
    "large": {"large", "\u6fa6?"},
}


def normalize_font_size(value):
    for normalized, aliases in LEGACY_FONT_SIZE_ALIASES.items():
        if value in aliases:
            return normalized
    return value


@dataclass
class TemplateInfo:
    path: str
    name: str
    content: str = ""
    variables: list = field(default_factory=list)
    exists: bool = True


def default_settings():
    return {
        "connection_type": "tcp",
        "tcp_ip": PRINTER_IP,
        "tcp_port": PRINTER_PORT,
        "usb_device": "",
        "template_list": [],
        "template_configs": {},
        "appearance": {"theme": "light", "accent": THEME_PRESETS["蓝色"], "font_size": "medium"},
    }


def normalize_settings(data):
    settings = default_settings()
    if isinstance(data, dict):
        settings.update(data)
    settings.pop("default_check_all_variables", None)
    if not isinstance(settings.get("template_list"), list):
        settings["template_list"] = []
    if not isinstance(settings.get("template_configs"), dict):
        settings["template_configs"] = {}
    appearance = default_settings()["appearance"]
    if isinstance(settings.get("appearance"), dict):
        appearance.update(settings["appearance"])
    appearance["font_size"] = normalize_font_size(appearance.get("font_size"))
    if appearance.get("font_size") not in FONT_SIZES:
        appearance["font_size"] = "medium"
    if appearance.get("accent") not in THEME_PRESETS.values():
        appearance["accent"] = THEME_PRESETS["蓝色"]
    settings["appearance"] = appearance
    return settings


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        return default_settings()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return normalize_settings(json.load(f))
    except Exception:
        return default_settings()


def save_settings(settings):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(normalize_settings(settings), f, ensure_ascii=False, indent=2)


def read_text_file(path):
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="ignore")


def extract_variables(text):
    result = []
    seen = set()
    for match in FIELD_DATA_PATTERN.finditer(text):
        field_data = match.group(2)
        for name in VARIABLE_PATTERN.findall(field_data):
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def code128_run_uses_subset_c(run_length, at_end):
    return run_length >= 4 if at_end else run_length >= 6


def code128_consume_subset_c_digits(digits):
    even_length = len(digits) - (len(digits) % 2)
    return digits[:even_length], digits[even_length:]


def code128_scan_subset(text, initial_subset="B"):
    subset = initial_subset
    index = 0
    while index < len(text):
        if text[index] == ">" and index + 1 < len(text):
            command = text[index + 1]
            if command in {":", "6"}:
                subset = "B"
                index += 2
                continue
            if command in {";", "5"}:
                subset = "C"
                index += 2
                continue
        index += 1
    return subset


def code128_scan_state(text, initial_subset="B", last_char="", last_char_subset="B"):
    subset = initial_subset
    index = 0
    current_last_char = last_char
    current_last_char_subset = last_char_subset
    while index < len(text):
        if text[index] == ">" and index + 1 < len(text):
            command = text[index + 1]
            if command in {":", "6"}:
                subset = "B"
                index += 2
                continue
            if command in {";", "5"}:
                subset = "C"
                index += 2
                continue
        current_last_char = text[index]
        current_last_char_subset = subset
        index += 1
    return subset, current_last_char, current_last_char_subset


def code128_literal_requires_subset_b(text):
    index = 0
    subset = "B"
    while index < len(text):
        if text[index] == ">" and index + 1 < len(text):
            command = text[index + 1]
            if command in {":", "6"}:
                subset = "B"
                index += 2
                continue
            if command in {";", "5"}:
                subset = "C"
                index += 2
                continue
        if subset == "C" and not text[index].isdigit():
            return True
        index += 1
    return False


def encode_code128_value(value, initial_subset=None, include_start=True, prior_b_digit=False):
    value = str(value or "")
    if not value:
        return ""

    index = 0
    parts = []

    if include_start:
        leading_digits = 0
        while leading_digits < len(value) and value[leading_digits].isdigit():
            leading_digits += 1
        start_with_c = False
        if leading_digits:
            at_end = leading_digits == len(value)
            if leading_digits % 2 == 0 and code128_run_uses_subset_c(leading_digits, at_end):
                start_with_c = True
        parts.append(">;" if start_with_c else ">:")
        subset = "C" if start_with_c else "B"
    else:
        subset = initial_subset or "B"

    while index < len(value):
        if subset == "B":
            digit_run = 0
            while index + digit_run < len(value) and value[index + digit_run].isdigit():
                digit_run += 1
            if digit_run:
                at_end = index + digit_run == len(value)
                if (
                    prior_b_digit
                    and digit_run >= 4
                    and code128_run_uses_subset_c(digit_run + 1, at_end)
                ):
                    parts.append(">5")
                    subset = "C"
                    prior_b_digit = False
                    continue
                if digit_run % 2 == 1:
                    usable_length = digit_run - 1
                    if usable_length and code128_run_uses_subset_c(usable_length, at_end):
                        parts.append(value[index])
                        prior_b_digit = value[index].isdigit()
                        index += 1
                        digit_run -= 1
                        at_end = index + digit_run == len(value)
                if digit_run and digit_run % 2 == 0 and code128_run_uses_subset_c(digit_run, at_end):
                    parts.append(">5")
                    subset = "C"
                    prior_b_digit = False
                    continue
            parts.append(value[index])
            prior_b_digit = value[index].isdigit()
            index += 1
            continue

        digit_run = 0
        while index + digit_run < len(value) and value[index + digit_run].isdigit():
            digit_run += 1
        if digit_run >= 2:
            consumed, remainder = code128_consume_subset_c_digits(value[index:index + digit_run])
            parts.append(consumed)
            index += len(consumed)
            if remainder:
                parts.append(">6")
                subset = "B"
                prior_b_digit = False
            continue
        parts.append(">6")
        subset = "B"
        prior_b_digit = False

    return "".join(parts)


def render_field_data(field_data, replacements):
    def replace_match(match):
        name = match.group(1).strip()
        return replacements.get(name, match.group(0))

    return VARIABLE_PATTERN.sub(replace_match, field_data)


def render_code128_field(field_data, replacements):
    if not VARIABLE_PATTERN.search(field_data):
        return field_data

    if not CODE128_ESCAPE_PATTERN.search(field_data):
        return encode_code128_value(render_field_data(field_data, replacements))

    rendered_parts = []
    cursor = 0
    current_subset = "B"
    last_char = ""
    last_char_subset = "B"

    for match in VARIABLE_PATTERN.finditer(field_data):
        literal = field_data[cursor:match.start()]
        if literal:
            if current_subset == "C" and code128_literal_requires_subset_b(literal):
                rendered_parts.append(">6")
                current_subset = "B"
                last_char = ""
                last_char_subset = "B"
            rendered_parts.append(literal)
            current_subset, last_char, last_char_subset = code128_scan_state(
                literal, current_subset, last_char, last_char_subset
            )

        name = match.group(1).strip()
        replacement = replacements.get(name, match.group(0))
        if (
            current_subset == "B"
            and last_char_subset == "B"
            and last_char.isdigit()
            and replacement.isdigit()
            and len(replacement) % 2 == 1
            and code128_run_uses_subset_c(len(replacement) + 1, True)
        ):
            encoded_value = ">5" + replacement
        else:
            encoded_value = encode_code128_value(
                replacement,
                initial_subset=current_subset,
                include_start=False,
            )
        rendered_parts.append(encoded_value)
        current_subset, last_char, last_char_subset = code128_scan_state(
            encoded_value, current_subset, last_char, last_char_subset
        )
        cursor = match.end()

    tail = field_data[cursor:]
    if tail:
        if current_subset == "C" and code128_literal_requires_subset_b(tail):
            rendered_parts.append(">6")
            current_subset = "B"
        rendered_parts.append(tail)
    return "".join(rendered_parts)


def render_template_zpl(text, replacements):
    rendered_parts = []
    cursor = 0
    for match in FIELD_DATA_PATTERN.finditer(text):
        field_start, field_end = match.span()
        rendered_parts.append(text[cursor:match.start(2)])
        field_data = match.group(2)
        rendered_field = render_field_data(field_data, replacements)
        field_context = text[cursor:match.start(1)]
        if VARIABLE_PATTERN.search(field_data) and CODE128_FIELD_PATTERN.search(field_context):
            rendered_field = render_code128_field(field_data, replacements)
        rendered_parts.append(rendered_field)
        cursor = match.end(2)
    rendered_parts.append(text[cursor:])
    return "".join(rendered_parts)


def replace_variable(text, name, value):
    placeholder = f"@{name}@"

    def replace_in_field(match):
        return f"{match.group(1)}{match.group(2).replace(placeholder, value)}{match.group(3)}"

    return FIELD_DATA_PATTERN.sub(replace_in_field, text)


def increment_numeric_text(value):
    width = len(value)
    return str(int(value) + 1).zfill(width)


def split_suffix_digits(value):
    text = str(value or "")
    match = re.match(r"^(.*?)(\d+)$", text)
    if not match:
        return text, ""
    return match.group(1), match.group(2)


def can_auto_increment_value(value):
    _, digits = split_suffix_digits(value)
    return bool(digits)


def increment_auto_value(value):
    prefix, digits = split_suffix_digits(value)
    if not digits:
        raise ValueError("值末尾不包含可递增的数字")
    return prefix + increment_numeric_text(digits)


def detect_usb_devices():
    if sys.platform.startswith("win"):
        return detect_windows_usb_printers()
    elif sys.platform == "darwin":
        ports = glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbserial*")
    else:
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/usb/lp*")
    return sorted(set(ports))


def detect_windows_usb_printers():
    try:
        import win32print
    except ImportError:
        print("[USB] win32print unavailable, cannot enumerate USB printer ports.")
        return []

    ports = {}

    def register_port(port_name, printer_name=""):
        port_name = str(port_name or "").strip()
        if not port_name or not port_name.upper().startswith("USB"):
            return
        entry = ports.setdefault(port_name, {"port": port_name, "label": port_name, "printers": []})
        printer_name = str(printer_name or "").strip()
        if printer_name and printer_name not in entry["printers"]:
            entry["printers"].append(printer_name)
            entry["label"] = f"{port_name} - {printer_name}"

    try:
        for port in win32print.EnumPorts(None, 2):
            register_port(port.get("pPortName", ""))
    except Exception as error:
        print(f"[USB] EnumPorts failed: {error}")

    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for printer in win32print.EnumPrinters(flags, None, 2):
            printer_name = printer.get("pPrinterName", "")
            for port_name in str(printer.get("pPortName", "")).split(","):
                register_port(port_name, printer_name)
    except Exception as error:
        print(f"[USB] EnumPrinters failed: {error}")

    return [ports[key] for key in sorted(ports)]


def get_selected_usb_port(combo_box):
    data = combo_box.currentData()
    if isinstance(data, dict):
        return str(data.get("port", "")).strip()
    return combo_box.currentText().split(" - ", 1)[0].strip()


def resolve_usb_printer_name(port_name):
    if not sys.platform.startswith("win"):
        return ""
    for entry in detect_windows_usb_printers():
        if entry.get("port") == port_name and entry.get("printers"):
            return entry["printers"][0]
    return ""


def prepare_zpl_payload(zpl_data):
    zpl_text = (zpl_data or "").replace("\ufeff", "")
    zpl_text = zpl_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not zpl_text:
        raise ValueError("ZPL 数据为空")
    upper_text = zpl_text.upper()
    if "^XA" not in upper_text or "^XZ" not in upper_text:
        raise ValueError("ZPL 数据缺少 ^XA/^XZ 起止指令")
    payload = zpl_text.encode("utf-8")
    return zpl_text, payload


def send_zpl_over_tcp(ip, port, payload):
    sock = None
    try:
        print(f"[TCP] Connecting to {ip}:{port}")
        sock = socket.create_connection((ip, int(port)), timeout=5)
        print(f"[TCP] Connected, sending {len(payload)} bytes")
        sock.sendall(payload)
        print("[TCP] sendall completed")
        return True, "OK"
    except Exception as error:
        print(f"[TCP] Send failed: {error}")
        return False, f"TCP 发送失败：{error}"
    finally:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
                print("[TCP] Socket closed")
            except Exception as error:
                print(f"[TCP] Socket close failed: {error}")


def send_zpl_to_windows_usb_printer(port_name, payload):
    try:
        import win32print
    except ImportError:
        return False, "缺少 pywin32，无法进行 USB 打印"

    printer_name = resolve_usb_printer_name(port_name)
    if not printer_name:
        return False, f"未找到绑定到端口 {port_name} 的 USB 打印机"

    printer_handle = None
    doc_started = False
    page_started = False
    try:
        print(f"[USB] Opening printer '{printer_name}' on port {port_name}")
        printer_handle = win32print.OpenPrinter(printer_name)
        win32print.StartDocPrinter(printer_handle, 1, ("ZPL Label", None, "RAW"))
        doc_started = True
        win32print.StartPagePrinter(printer_handle)
        page_started = True
        written = win32print.WritePrinter(printer_handle, payload)
        print(f"[USB] WritePrinter sent {written} bytes")
        return True, "OK"
    except Exception as error:
        print(f"[USB] Send failed: {error}")
        return False, f"USB 发送失败：{error}"
    finally:
        if printer_handle is not None:
            if page_started:
                try:
                    win32print.EndPagePrinter(printer_handle)
                except Exception as error:
                    print(f"[USB] EndPagePrinter failed: {error}")
            if doc_started:
                try:
                    win32print.EndDocPrinter(printer_handle)
                except Exception as error:
                    print(f"[USB] EndDocPrinter failed: {error}")
            try:
                win32print.ClosePrinter(printer_handle)
                print("[USB] Printer handle closed")
            except Exception as error:
                print(f"[USB] ClosePrinter failed: {error}")


def icon_path():
    return APP_ICON_PATH if os.path.exists(APP_ICON_PATH) else ""


class ConfigNameDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建配置方案")
        self.setMinimumWidth(420)
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入配置名称")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        prompt = QLabel("请输入新配置方案的名称：")
        layout.addWidget(prompt)
        layout.addWidget(self.input)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return self.input.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("打印机设置")
        self.setMinimumWidth(560)
        self.settings = settings.copy()
        self.connection = QComboBox()
        self.connection.addItem("TCP/IP 网络打印", "tcp")
        self.connection.addItem("USB 直接连接（Type-B）", "usb")
        index = self.connection.findData(settings.get("connection_type", "tcp"))
        self.connection.setCurrentIndex(index if index >= 0 else 0)
        self.ip = QLineEdit(str(settings.get("tcp_ip", PRINTER_IP)))
        self.port = QLineEdit(str(settings.get("tcp_port", PRINTER_PORT)))
        self.port.setValidator(QIntValidator(1, 65535, self))
        self.usb = QComboBox()
        self.test_tcp = QPushButton("测试连接")
        self.test_usb = QPushButton("测试连接")
        self.build()
        self.refresh_usb()
        self.connection.currentIndexChanged.connect(self.update_mode)
        self.test_tcp.clicked.connect(self.test_tcp_connection)
        self.test_usb.clicked.connect(self.test_usb_connection)
        self.update_mode()

    def build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        form = QFormLayout()
        form.addRow("连接方式", self.connection)
        self.ip_label = QLabel("TCP/IP 参数")
        self.ip_field_label = QLabel("IP")
        self.port_field_label = QLabel("端口")
        tcp_row = QHBoxLayout()
        tcp_row.addWidget(self.ip_field_label)
        tcp_row.addWidget(self.ip, 1)
        tcp_row.addWidget(self.port_field_label)
        tcp_row.addWidget(self.port)
        tcp_row.addWidget(self.test_tcp)
        form.addRow(self.ip_label, tcp_row)
        self.usb_label = QLabel("USB 设备")
        usb_row = QHBoxLayout()
        usb_row.addWidget(self.usb, 1)
        usb_row.addWidget(self.test_usb)
        form.addRow(self.usb_label, usb_row)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def refresh_usb(self):
        current = self.settings.get("usb_device", "") or (get_selected_usb_port(self.usb) if self.usb.count() else "")
        self.usb.clear()
        devices = detect_usb_devices()
        if sys.platform.startswith("win"):
            if devices:
                for device in devices:
                    self.usb.addItem(device.get("label", device.get("port", "")), device)
            else:
                self.usb.addItem("未检测到 USB 打印机端口")
            if current:
                for index in range(self.usb.count()):
                    data = self.usb.itemData(index)
                    if isinstance(data, dict) and data.get("port") == current:
                        self.usb.setCurrentIndex(index)
                        break
        else:
            self.usb.addItems(devices or ["未检测到 USB 设备"])
            if current:
                self.usb.setCurrentText(current)

    def update_mode(self):
        is_tcp = self.connection.currentData() == "tcp"
        for widget in (self.ip_label, self.ip_field_label, self.ip, self.port_field_label, self.port, self.test_tcp):
            widget.setVisible(is_tcp)
        for widget in (self.usb_label, self.usb, self.test_usb):
            widget.setVisible(not is_tcp)

    def test_tcp_connection(self):
        ip = self.ip.text().strip()
        port = self.port.text().strip()
        if not ip or not port.isdigit():
            QMessageBox.warning(self, "测试连接", "请输入有效的 IP 地址和端口")
            return
        try:
            with socket.create_connection((ip, int(port)), timeout=3):
                pass
            QMessageBox.information(self, "测试连接", "TCP/IP 打印机连接成功")
        except Exception as error:
            QMessageBox.critical(self, "测试连接", f"TCP/IP 打印机连接失败：{error}")

    def test_usb_connection(self):
        device = get_selected_usb_port(self.usb)
        if not device or device in {"未检测到 USB 打印机端口", "未检测到 USB 设备"}:
            QMessageBox.warning(self, "测试连接", "USB打印机端口未连接或无法访问")
            return
        try:
            if sys.platform.startswith("win"):
                printer_name = resolve_usb_printer_name(device)
                if not printer_name:
                    raise RuntimeError(f"未找到绑定到端口 {device} 的打印机")
            QMessageBox.information(self, "测试连接", "USB 打印机连接成功")
        except Exception as error:
            QMessageBox.critical(self, "测试连接", f"USB 打印机连接失败：{error}")

    def accept(self):
        if self.connection.currentData() == "tcp" and not self.port.text().strip().isdigit():
            QMessageBox.warning(self, "设置错误", "端口必须是数字")
            return
        self.settings.update({
            "connection_type": self.connection.currentData(),
            "tcp_ip": self.ip.text().strip() or PRINTER_IP,
            "tcp_port": int(self.port.text().strip() or PRINTER_PORT),
            "usb_device": get_selected_usb_port(self.usb),
        })
        super().accept()
class AppearanceDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("外观主题")
        self.setMinimumWidth(420)
        self.settings = normalize_settings(settings.copy())
        appearance = self.settings.get("appearance", default_settings()["appearance"])
        self.theme = QComboBox()
        self.theme.addItem("浅色模式", "light")
        self.theme.addItem("深色模式", "dark")
        index = self.theme.findData(appearance.get("theme", "light"))
        self.theme.setCurrentIndex(index if index >= 0 else 0)
        self.accent = QComboBox()
        for name, color in THEME_PRESETS.items():
            self.accent.addItem(name, color)
        index = self.accent.findData(appearance.get("accent", THEME_PRESETS["蓝色"]))
        self.accent.setCurrentIndex(index if index >= 0 else 0)
        self.font_size = QComboBox()
        self.font_size.addItems(FONT_SIZES.keys())
        self.font_size.setCurrentText(appearance.get("font_size", "medium"))
        self.build()
        self.theme.currentIndexChanged.connect(self.preview)
        self.accent.currentIndexChanged.connect(self.preview)
        self.font_size.currentIndexChanged.connect(self.preview)

    def build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        form = QFormLayout()
        form.addRow("主题", self.theme)
        form.addRow("主色调", self.accent)
        form.addRow("字体大小", self.font_size)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return {"theme": self.theme.currentData(), "accent": self.accent.currentData(), "font_size": self.font_size.currentText()}

    def preview(self):
        if self.parent():
            self.parent().settings["appearance"] = self.value()
            self.parent().apply_qss()

    def accept(self):
        self.settings["appearance"] = self.value()
        super().accept()


class TemplateItemWidget(QWidget):
    deleteRequested = pyqtSignal(str)
    selectedRequested = pyqtSignal(str)

    def __init__(self, path, title, missing):
        super().__init__()
        self.path = path
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.label.installEventFilter(self)
        self.delete = QPushButton("×")
        self.delete.setObjectName("TinyDangerButton")
        self.delete.setFixedSize(22, 22)
        self.delete.setCursor(Qt.PointingHandCursor)
        self.delete.setFocusPolicy(Qt.NoFocus)
        self.delete.setToolTip("删除模板")
        self.delete.clicked.connect(lambda: self.deleteRequested.emit(self.path))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.delete)
        self.set_title(title, missing)
        self.set_selected(False)

    def eventFilter(self, obj, event):
        if obj is self.label and event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.selectedRequested.emit(self.path)
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.selectedRequested.emit(self.path)
        super().mousePressEvent(event)

    def set_title(self, title, missing=False):
        text = f"{title}  [文件缺失]" if missing else title
        self.label.setText(text)
        self.label.setToolTip(text)
        self.setToolTip(text)

    def set_selected(self, selected):
        if selected:
            self.setStyleSheet("background:#2B5C8A; border-radius:9px;")
            self.label.setStyleSheet("background:transparent; color:white;")
        else:
            self.setStyleSheet("background:transparent;")
            self.label.setStyleSheet("background:transparent;")


class VariableRow(QWidget):
    def __init__(self, name, checked, on_change):
        super().__init__()
        self.name = name
        self.on_change = on_change
        self.label = QLabel(f"@{name}@")
        self.label.setObjectName("VariableName")
        self.input = QLineEdit()
        self.need = QCheckBox("需要替换")
        self.need.setChecked(checked)
        self.auto = QCheckBox("自动递增")
        self.next_label = QLabel("下次打印值: -")
        self.next_label.setObjectName("NextValue")
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setColumnStretch(1, 1)
        layout.addWidget(self.label, 0, 0, 2, 1)
        layout.addWidget(self.input, 0, 1, 2, 1)
        layout.addWidget(self.need, 0, 2)
        layout.addWidget(self.auto, 0, 3)
        layout.addWidget(self.next_label, 1, 2, 1, 2)
        self.input.textChanged.connect(self.changed)
        self.need.toggled.connect(self.need_changed)
        self.auto.toggled.connect(self.auto_changed)
        self.need_changed(checked)
        self.changed()

    def value(self):
        return self.input.text()

    def set_value(self, value):
        self.input.setText(str(value))

    def should_replace(self):
        return self.need.isChecked()

    def is_auto_increment(self):
        return self.auto.isChecked()

    def snapshot(self):
        return {"value": self.value(), "replace": self.should_replace(), "auto_increment": self.is_auto_increment()}

    def apply_snapshot(self, data):
        self.input.setText(str(data.get("value", "")))
        self.need.setChecked(bool(data.get("replace", True)))
        self.auto.setChecked(bool(data.get("auto_increment", False)))
        self.changed()

    def need_changed(self, checked):
        self.input.setEnabled(checked)
        self.auto.setEnabled(checked and can_auto_increment_value(self.value()))
        self.changed()

    def changed(self):
        ok = self.should_replace() and can_auto_increment_value(self.value())
        if not ok and self.auto.isChecked():
            self.auto.blockSignals(True)
            self.auto.setChecked(False)
            self.auto.blockSignals(False)
        self.auto.setEnabled(ok)
        if self.should_replace() and self.auto.isChecked() and ok:
            self.next_label.setText(f"下次打印值: {increment_auto_value(self.value())}")
        else:
            self.next_label.setText("下次打印值: -")
        self.on_change()

    def auto_changed(self, checked):
        if checked and not can_auto_increment_value(self.value()):
            self.auto.setChecked(False)
            QMessageBox.warning(self, "自动递增", f"变量 @{self.name}@ 末尾必须带数字，才能启用自动递增")
        self.changed()


class PrintCountStepper(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 1
        self.minus = QPushButton("-")
        self.plus = QPushButton("+")
        self.value_input = QLineEdit("1")
        self.value_input.setAlignment(Qt.AlignCenter)
        self.value_input.setObjectName("StepperValue")
        self.value_input.setValidator(QIntValidator(1, 999, self))
        self.value_input.setToolTip("可直接输入 1-999 的打印份数")
        self.minus.setObjectName("StepperButton")
        self.plus.setObjectName("StepperButton")
        self.minus.setFixedSize(34, 34)
        self.plus.setFixedSize(34, 34)
        self.value_input.setFixedWidth(62)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        layout.addWidget(self.minus)
        layout.addWidget(self.value_input)
        layout.addWidget(self.plus)
        self.setObjectName("PrintCountStepper")
        self.minus.clicked.connect(lambda: self.setValue(self._value - 1))
        self.plus.clicked.connect(lambda: self.setValue(self._value + 1))
        self.value_input.editingFinished.connect(self.commit_input)
        self.value_input.returnPressed.connect(self.commit_input)

    def value(self):
        self.commit_input()
        return self._value

    def setValue(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = self._value
        value = max(1, min(999, value))
        if value == self._value and self.value_input.text() == str(value):
            return
        self._value = value
        self.value_input.setText(str(value))
        self.valueChanged.emit(value)
        self.animate_value()

    def commit_input(self):
        text = self.value_input.text().strip()
        if not text:
            self.setValue(self._value)
            return
        self.setValue(text)

    def animate_value(self):
        animation = QPropertyAnimation(self.value_input, b"maximumWidth", self)
        animation.setDuration(120)
        animation.setStartValue(68)
        animation.setEndValue(62)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.start(QPropertyAnimation.DeleteWhenStopped)


class ZPLLabelPrinterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ZPL 标签打印工具")
        if icon_path():
            self.setWindowIcon(QIcon(icon_path()))
        self.resize(1240, 760)
        self.setMinimumSize(980, 620)
        self.settings = load_settings()
        self.templates = {}
        self.current_template_path = None
        self.variable_rows = {}
        self.connection_ok = False
        self.build_ui()
        self.apply_qss()
        self.load_templates()
        self.update_connection_status()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_connection_status)
        self.status_timer.start(12000)

    def build_ui(self):
        self.toolbox = self.menuBar().addMenu("工具箱")
        action_settings = QAction("打印机设置", self)
        action_settings.triggered.connect(self.open_settings)
        action_appearance = QAction("外观主题", self)
        action_appearance.triggered.connect(self.open_appearance)
        action_import = QAction("导入模板", self)
        action_import.triggered.connect(self.open_templates)
        action_export = QAction("导出配置", self)
        action_export.triggered.connect(self.export_settings)
        action_import_config = QAction("导入配置", self)
        action_import_config.triggered.connect(self.import_settings)
        for action in (action_settings, action_appearance, action_import, action_export, action_import_config):
            self.toolbox.addAction(action)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        self.splitter = QSplitter(Qt.Horizontal)
        root.addWidget(self.splitter)
        self.sidebar = self.panel()
        self.editor_panel = self.panel()
        self.preview_panel = self.panel()
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.editor_panel)
        self.splitter.addWidget(self.preview_panel)
        self.splitter.setSizes([260, 470, 420])
        self.build_sidebar()
        self.build_editor()
        self.build_preview()
        self.status = self.statusBar()
        self.print_progress = QProgressBar()
        self.print_progress.setMaximumWidth(260)
        self.print_progress.hide()
        self.status.addPermanentWidget(self.print_progress)

    def panel(self):
        frame = QFrame()
        frame.setObjectName("Panel")
        return frame

    def build_sidebar(self):
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(18, 18, 18, 18)
        title = QLabel("模板管理")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        self.open_button = QPushButton("打开模板")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.clicked.connect(self.open_templates)
        layout.addWidget(self.open_button)
        self.template_list = QListWidget()
        self.template_list.setObjectName("TemplateList")
        self.template_list.setTextElideMode(Qt.ElideNone)
        self.template_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.template_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.template_list.itemClicked.connect(lambda item: self.select_template(item.data(Qt.UserRole)))
        self.template_list.itemDoubleClicked.connect(self.rename_template)
        layout.addWidget(self.template_list, 1)

    def build_editor(self):
        layout = QVBoxLayout(self.editor_panel)
        layout.setContentsMargins(22, 20, 22, 20)
        self.current_label = QLabel("暂无模板，请先导入。")
        self.current_label.setObjectName("PanelTitle")
        layout.addWidget(self.current_label)
        config_bar = QHBoxLayout()
        config_bar.addWidget(QLabel("配置方案："))
        self.config_combo = QComboBox()
        self.config_combo.currentTextChanged.connect(self.load_selected_config)
        self.save_config_button = QPushButton("保存配置")
        self.save_config_button.clicked.connect(self.save_current_config)
        self.new_config_button = QPushButton("新建配置")
        self.new_config_button.clicked.connect(self.new_config)
        self.rename_config_button = QPushButton("重命名")
        self.rename_config_button.clicked.connect(self.rename_config)
        self.delete_config_button = QPushButton("删除")
        self.delete_config_button.clicked.connect(self.delete_config)
        config_bar.addWidget(self.config_combo, 1)
        for button in (self.save_config_button, self.new_config_button, self.rename_config_button, self.delete_config_button):
            config_bar.addWidget(button)
        layout.addLayout(config_bar)
        section = QLabel("可替换变量列表")
        section.setObjectName("SectionTitle")
        layout.addWidget(section)
        self.variable_scroll = QScrollArea()
        self.variable_scroll.setWidgetResizable(True)
        self.variable_box = QWidget()
        self.variable_layout = QVBoxLayout(self.variable_box)
        self.variable_layout.setContentsMargins(0, 0, 0, 0)
        self.variable_layout.addStretch(1)
        self.variable_scroll.setWidget(self.variable_box)
        layout.addWidget(self.variable_scroll, 1)
        buttons = QHBoxLayout()
        self.apply_all_button = QPushButton("应用到预览")
        self.reset_button = QPushButton("重置默认值")
        self.apply_all_button.clicked.connect(self.update_preview)
        self.reset_button.clicked.connect(self.reset_values)
        buttons.addWidget(self.apply_all_button)
        buttons.addWidget(self.reset_button)
        layout.addLayout(buttons)

    def build_preview(self):
        layout = QVBoxLayout(self.preview_panel)
        layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel("ZPL 指令预览")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setLineWrapMode(QTextEdit.NoWrap)
        self.preview_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.preview_text, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("打印份数："))
        self.print_count_stepper = PrintCountStepper()
        bottom.addWidget(self.print_count_stepper)
        self.print_button = QPushButton("发送打印")
        self.print_button.setObjectName("PrintButton")
        self.print_button.clicked.connect(self.print_current_label)
        self.connection_status = QLabel()
        self.connection_status.setObjectName("ConnectionStatus")
        bottom.addWidget(self.print_button, 1)
        bottom.addWidget(self.connection_status)
        layout.addLayout(bottom)

    def apply_qss(self):
        app = self.settings.get("appearance", default_settings()["appearance"])
        dark = app.get("theme") == "dark"
        accent = app.get("accent", THEME_PRESETS["蓝色"])
        font_size = FONT_SIZES.get(app.get("font_size", "medium"), 13)
        bg, panel, text = ("#111827", "#1F2937", "#F9FAFB") if dark else ("#F5F7FA", "#FFFFFF", "#1D1D1F")
        muted, input_bg, border, hover = ("#9CA3AF", "#111827", "#374151", "#3D3D3D") if dark else ("#8A94A6", "#FFFFFF", "#E2E7EF", "#E5E7EB")
        list_bg = "#2D2D2D" if dark else "#F5F5F5"
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {bg}; color: {text}; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: {font_size}px; }}
            QMenuBar, QMenu {{ background: {panel}; color: {text}; border: 1px solid {border}; }}
            QMenuBar::item:selected, QMenu::item:selected {{ background: {hover}; }}
            QFrame#Panel {{ background: {panel}; border: 1px solid {border}; border-radius: 14px; }}
            QLabel#PanelTitle {{ font-size: {font_size + 5}px; font-weight: 700; background: transparent; }}
            QLabel#SectionTitle {{ color: {accent}; font-weight: 700; background: transparent; }}
            QLabel#VariableName, QLabel#NextValue {{ color: {muted}; background: transparent; }}
            QLabel#ConnectionStatus {{ font-weight: 700; background: transparent; }}
            QPushButton {{ min-height: 34px; border: none; border-radius: 9px; padding: 7px 13px; background: {hover}; color: {text}; }}
            QPushButton:hover {{ background: {border}; }}
            QPushButton#PrimaryButton, QPushButton#PrintButton {{ background: {accent}; color: white; font-weight: 700; }}
            QPushButton#TinyDangerButton {{ background: transparent; color: #9CA3AF; font-weight: 700; padding: 0; min-height: 20px; border-radius: 11px; }}
            QPushButton#TinyDangerButton:hover {{ background: #DC2626; color: #FFFFFF; }}
            QListWidget#TemplateList {{ background: {list_bg}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 6px; outline: none; }}
            QListWidget#TemplateList::item {{ min-height: 42px; padding: 4px; border-radius: 9px; background: {list_bg}; color: {text}; }}
            QListWidget#TemplateList::item:hover {{ background: {hover}; color: {text}; }}
            QListWidget#TemplateList::item:selected {{ background: #2B5C8A; color: #FFFFFF; }}
            QLineEdit, QTextEdit, QComboBox {{ background: {input_bg}; color: {text}; border: 1px solid {border}; border-radius: 9px; padding: 7px 10px; }}
            QLineEdit:disabled {{ background: {hover}; color: {muted}; }}
            QScrollArea {{ border: none; background: transparent; }}
            QWidget#PrintCountStepper {{ background: {hover}; border: 1px solid {border}; border-radius: 18px; }}
            QPushButton#StepperButton {{ background: transparent; color: {text}; min-height: 30px; min-width: 30px; border-radius: 15px; padding: 0; font-size: {font_size + 4}px; font-weight: 700; }}
            QPushButton#StepperButton:hover {{ background: {border}; }}
            QLineEdit#StepperValue {{ background: transparent; color: {text}; font-weight: 700; font-size: {font_size + 1}px; border: none; padding: 0; }}
            QProgressBar {{ border: 1px solid {border}; border-radius: 8px; text-align: center; }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 8px; }}
        """)
        if hasattr(self, "template_list"):
            self.refresh_template_item_styles()

    def save_all(self):
        self.settings["template_list"] = [
            {"path": self.template_list.item(i).data(Qt.UserRole), "name": self.templates[self.template_list.item(i).data(Qt.UserRole)].name}
            for i in range(self.template_list.count())
        ]
        save_settings(self.settings)

    def load_templates(self):
        for item in self.settings.get("template_list", []):
            if isinstance(item, dict):
                self.add_template(item.get("path", ""), item.get("name"), persist=False)
            elif isinstance(item, str):
                self.add_template(item, None, persist=False)
        if self.template_list.count():
            self.select_template(self.template_list.item(0).data(Qt.UserRole))
        else:
            self.clear_workspace()

    def open_templates(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "导入模板", os.path.expanduser("~"), "ZPL 模板 (*.zpl *.prn);;所有文件 (*.*)")
        first_new = None
        for path in paths:
            added = self.add_template(path)
            if added and first_new is None:
                first_new = path
        if first_new:
            self.select_template(first_new)
            self.save_all()

    def add_template(self, path, display_name=None, persist=True):
        if not path:
            return False
        path = os.path.abspath(path)
        if path in self.templates:
            return False
        exists = os.path.exists(path)
        name = display_name or os.path.basename(path)
        content = read_text_file(path) if exists else ""
        info = TemplateInfo(path=path, name=name, content=content, variables=extract_variables(content), exists=exists)
        self.templates[path] = info
        item = QListWidgetItem()
        item.setData(Qt.UserRole, path)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setSizeHint(QSize(220, 56))
        item.setToolTip(name)
        self.template_list.addItem(item)
        widget = TemplateItemWidget(path, name, not exists)
        widget.deleteRequested.connect(self.delete_template)
        widget.selectedRequested.connect(self.select_template)
        self.template_list.setItemWidget(item, widget)
        if not exists:
            item.setForeground(Qt.gray)
        if persist:
            self.save_all()
        return True

    def refresh_template_item_styles(self):
        for i in range(self.template_list.count()):
            item = self.template_list.item(i)
            path = item.data(Qt.UserRole)
            widget = self.template_list.itemWidget(item)
            if widget and path in self.templates:
                widget.set_title(self.templates[path].name, not self.templates[path].exists)
                widget.set_selected(path == self.current_template_path)

    def rename_template(self, item):
        path = item.data(Qt.UserRole)
        if path not in self.templates:
            return
        name, ok = QInputDialog.getText(self, "重命名模板", "请输入模板显示名称：", text=self.templates[path].name)
        if ok and name.strip():
            self.templates[path].name = name.strip()
            widget = self.template_list.itemWidget(item)
            if widget:
                widget.set_title(name.strip(), not self.templates[path].exists)
            item.setToolTip(name.strip())
            if path == self.current_template_path:
                self.current_label.setText(f"当前模板：{name.strip()}")
            self.save_all()

    def delete_template(self, path):
        if path not in self.templates:
            return
        title = self.templates[path].name
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除模板“{title}”吗？仅从列表中移除，不会删除源文件。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return
        row_to_remove = -1
        for i in range(self.template_list.count()):
            if self.template_list.item(i).data(Qt.UserRole) == path:
                row_to_remove = i
                break
        if row_to_remove >= 0:
            self.template_list.takeItem(row_to_remove)
        self.templates.pop(path, None)
        self.settings.get("template_configs", {}).pop(path, None)
        if self.current_template_path == path:
            self.current_template_path = None
            if self.template_list.count():
                self.select_template(self.template_list.item(0).data(Qt.UserRole))
            else:
                self.clear_workspace()
        self.save_all()

    def clear_workspace(self):
        self.current_label.setText("暂无模板，请先导入。")
        self.preview_text.clear()
        self.clear_variable_rows()
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        self.config_combo.blockSignals(False)

    def clear_variable_rows(self):
        while self.variable_layout.count() > 1:
            item = self.variable_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.variable_rows.clear()

    def select_template(self, path):
        info = self.templates.get(path)
        if not info:
            return
        if not os.path.exists(path):
            info.exists = False
            QMessageBox.warning(self, "模板文件不存在", "模板文件不存在，请重新导入。")
            self.refresh_template_item_styles()
            return
        info.exists = True
        info.content = read_text_file(path)
        info.variables = extract_variables(info.content)
        self.current_template_path = path
        for i in range(self.template_list.count()):
            item = self.template_list.item(i)
            if item.data(Qt.UserRole) == path:
                self.template_list.setCurrentItem(item)
                break
        self.refresh_template_item_styles()
        self.current_label.setText(f"当前模板：{info.name}")
        self.ensure_template_configs(path)
        self.rebuild_variables()
        if not info.variables:
            QMessageBox.information(self, "未找到变量", "该模板不包含可替换变量，请检查模板格式。")
        self.update_preview()

    def ensure_template_configs(self, path):
        configs = self.settings.setdefault("template_configs", {}).setdefault(path, {})
        configs.setdefault("默认配置", {})
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        self.config_combo.addItems(configs.keys())
        self.config_combo.setCurrentText("默认配置")
        self.config_combo.blockSignals(False)

    def rebuild_variables(self):
        self.clear_variable_rows()
        if not self.current_template_path:
            return
        info = self.templates[self.current_template_path]
        config = self.current_config_data()
        for name in info.variables:
            data = config.get(name, {})
            row = VariableRow(name, bool(data.get("replace", True)), self.update_preview)
            row.apply_snapshot(data)
            self.variable_rows[name] = row
            self.variable_layout.insertWidget(self.variable_layout.count() - 1, row)

    def current_config_name(self):
        return self.config_combo.currentText() or "默认配置"

    def current_config_data(self):
        if not self.current_template_path:
            return {}
        return self.settings.setdefault("template_configs", {}).setdefault(self.current_template_path, {}).setdefault(self.current_config_name(), {})

    def save_current_config(self):
        if not self.current_template_path:
            return
        configs = self.settings.setdefault("template_configs", {}).setdefault(self.current_template_path, {})
        configs[self.current_config_name()] = {name: row.snapshot() for name, row in self.variable_rows.items()}
        self.save_all()
        QMessageBox.information(self, "保存配置", "配置方案已保存")

    def load_selected_config(self):
        if not self.current_template_path:
            return
        config = self.current_config_data()
        for name, row in self.variable_rows.items():
            row.apply_snapshot(config.get(name, {}))
        self.update_preview()

    def new_config(self):
        if not self.current_template_path:
            return
        dialog = ConfigNameDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        name = dialog.value()
        if not name:
            return
        configs = self.settings.setdefault("template_configs", {}).setdefault(self.current_template_path, {})
        if name in configs:
            QMessageBox.warning(self, "新建配置方案", "该配置方案已存在")
            return
        configs[name] = {var: row.snapshot() for var, row in self.variable_rows.items()}
        self.config_combo.addItem(name)
        self.config_combo.setCurrentText(name)
        self.save_all()

    def rename_config(self):
        if not self.current_template_path:
            return
        old = self.current_config_name()
        if old == "默认配置":
            QMessageBox.warning(self, "重命名配置", "默认配置不可重命名")
            return
        name, ok = QInputDialog.getText(self, "重命名配置", "请输入新的配置名称：", text=old)
        if not ok or not name.strip():
            return
        name = name.strip()
        configs = self.settings.setdefault("template_configs", {}).setdefault(self.current_template_path, {})
        if name in configs:
            QMessageBox.warning(self, "重命名配置", "该配置方案已存在")
            return
        configs[name] = configs.pop(old)
        self.ensure_template_configs(self.current_template_path)
        self.config_combo.setCurrentText(name)
        self.save_all()

    def delete_config(self):
        if not self.current_template_path:
            return
        name = self.current_config_name()
        if name == "默认配置":
            QMessageBox.warning(self, "删除配置", "默认配置不可删除")
            return
        if QMessageBox.question(self, "删除配置", f"确定要删除配置“{name}”吗？") != QMessageBox.Yes:
            return
        configs = self.settings.setdefault("template_configs", {}).setdefault(self.current_template_path, {})
        configs.pop(name, None)
        self.ensure_template_configs(self.current_template_path)
        self.rebuild_variables()
        self.save_all()

    def reset_values(self):
        for row in self.variable_rows.values():
            row.set_value("")
        self.update_preview()

    def build_zpl_from_rows(self):
        if not self.current_template_path:
            return ""
        info = self.templates[self.current_template_path]
        replacements = {
            name: row.value()
            for name, row in self.variable_rows.items()
            if row.should_replace()
        }
        return render_template_zpl(info.content, replacements)

    def update_preview(self):
        if hasattr(self, "preview_text"):
            self.preview_text.setPlainText(self.build_zpl_from_rows())

    def validate_variables(self):
        for row in self.variable_rows.values():
            if row.should_replace() and not row.value().strip():
                return False
        return True

    def set_printing_enabled(self, enabled):
        for widget in (self.sidebar, self.editor_panel, self.print_count_stepper, self.print_button):
            widget.setEnabled(enabled)

    def increment_auto_rows(self):
        for row in self.variable_rows.values():
            if row.should_replace() and row.is_auto_increment() and can_auto_increment_value(row.value()):
                row.set_value(increment_auto_value(row.value()))

    def print_current_label(self):
        if not self.current_template_path:
            QMessageBox.warning(self, "发送打印", "暂无模板，请先导入。")
            return
        if not self.validate_variables():
            QMessageBox.warning(self, "发送打印", "请填写所有变量")
            return
        total = self.print_count_stepper.value()
        original_values = {name: row.value() for name, row in self.variable_rows.items()}
        self.print_progress.setMaximum(total)
        self.print_progress.setValue(0)
        self.print_progress.show()
        self.set_printing_enabled(False)
        sent = 0
        try:
            for index in range(1, total + 1):
                self.print_button.setText(f"正在打印... 第 {index}/{total} 张")
                QApplication.processEvents()
                zpl = self.build_zpl_from_rows()
                ok, message = self.send_to_printer(zpl)
                if not ok:
                    for name, value in original_values.items():
                        self.variable_rows[name].set_value(value)
                    self.update_preview()
                    QMessageBox.critical(
                        self, "打印失败",
                        f"第 {index} 张打印失败：{message}。已停止打印。已成功打印 {sent} 张，递增变量已回滚到打印前状态。"
                    )
                    return
                sent += 1
                self.print_progress.setValue(sent)
                if index < total:
                    self.increment_auto_rows()
            QMessageBox.information(self, "打印完成", f"全部 {total} 张标签已发送完成")
        finally:
            self.print_button.setText("发送打印")
            self.set_printing_enabled(True)
            self.print_progress.hide()
            self.update_preview()
            self.update_connection_status()
    def send_to_printer(self, zpl_data):
        try:
            zpl_text, payload = prepare_zpl_payload(zpl_data)
        except Exception as error:
            print(f"[PRINT] Invalid ZPL payload: {error}")
            return False, f"ZPL 数据格式错误：{error}"

        mode = self.settings.get("connection_type", "tcp")
        print(f"[PRINT] mode={mode}")
        print(f"[PRINT] settings tcp_ip={self.settings.get('tcp_ip')} tcp_port={self.settings.get('tcp_port')} usb_device={self.settings.get('usb_device')}")
        print(f"[PRINT] zpl_length={len(zpl_text)} payload_bytes={len(payload)}")
        print("[PRINT] ZPL preview begin")
        print(zpl_text)
        print("[PRINT] ZPL preview end")

        if mode == "usb":
            device = (self.settings.get("usb_device") or "").strip()
            if not device:
                return False, "未配置 USB 打印机端口"
            if sys.platform.startswith("win"):
                return send_zpl_to_windows_usb_printer(device, payload)
            return False, "当前 USB 直接打印仅支持 Windows"

        ip = str(self.settings.get("tcp_ip", PRINTER_IP) or "").strip()
        port_value = self.settings.get("tcp_port", PRINTER_PORT)
        try:
            port = int(port_value)
        except Exception:
            return False, f"TCP 端口无效：{port_value}"
        if not ip:
            return False, "TCP/IP 地址为空"
        return send_zpl_over_tcp(ip, port, payload)

    def update_connection_status(self):
        mode = self.settings.get("connection_type", "tcp")
        ok = self.test_current_connection_quietly()
        self.connection_ok = ok
        if mode == "usb":
            text = "● USB已连接" if ok else "● USB未连接"
        else:
            text = "● TCP已连接" if ok else "● TCP未连接"
        color = "#16A34A" if ok else "#DC2626"
        if hasattr(self, "connection_status"):
            self.connection_status.setText(text)
            self.connection_status.setStyleSheet(f"color: {color}; font-weight: 700; background: transparent;")

    def test_current_connection_quietly(self):
        mode = self.settings.get("connection_type", "tcp")
        if mode == "usb":
            device = (self.settings.get("usb_device", "") or "").strip()
            if not device or device in {"未检测到 USB 打印机端口", "未检测到 USB 设备"}:
                return False
            if sys.platform.startswith("win"):
                return bool(resolve_usb_printer_name(device))
            return bool(device)
        try:
            ip = str(self.settings.get("tcp_ip", PRINTER_IP) or "").strip()
            port = int(self.settings.get("tcp_port", PRINTER_PORT))
            with socket.create_connection((ip, port), timeout=0.6):
                return True
        except Exception:
            return False

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_() == QDialog.Accepted:
            self.settings.update(dialog.settings)
            self.save_all()
            self.update_connection_status()

    def open_appearance(self):
        old = self.settings.get("appearance", {}).copy()
        dialog = AppearanceDialog(self.settings, self)
        if dialog.exec_() == QDialog.Accepted:
            self.settings["appearance"] = dialog.settings["appearance"]
            self.save_all()
        else:
            self.settings["appearance"] = old
            self.apply_qss()

    def export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", os.path.expanduser("~/zpl_printer_settings.zip"), "Zip 文件 (*.zip)")
        if not path:
            return
        self.save_all()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(SETTINGS_PATH, "settings.json")
        QMessageBox.information(self, "导出配置", "配置已导出")

    def import_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", os.path.expanduser("~"), "Zip 文件 (*.zip)")
        if not path:
            return
        try:
            with zipfile.ZipFile(path, "r") as zf:
                data = json.loads(zf.read("settings.json").decode("utf-8"))
            self.settings = normalize_settings(data)
            save_settings(self.settings)
            self.template_list.clear()
            self.templates.clear()
            self.load_templates()
            self.apply_qss()
            QMessageBox.information(self, "导入配置", "配置已导入")
        except Exception as error:
            QMessageBox.critical(self, "导入配置", f"导入失败：{error}")



def main():
    app = QApplication(sys.argv)
    if icon_path():
        app.setWindowIcon(QIcon(icon_path()))
    window = ZPLLabelPrinterWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
