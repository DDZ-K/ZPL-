from __future__ import annotations

import socket
import time
from datetime import datetime
from pathlib import Path


WATCH_DIR = r"E:\Download\AI\Test"
LOG_DIR = r"E:\Download\AI\Test\logs"
TARGET_TEXT = "Result=OK"
SEND_TEXT = "PING"
TCP_IP = "127.0.0.1"
TCP_PORT = 9000
SCAN_INTERVAL_SECONDS = 2
FILE_ENCODING = "utf-8"
LOG_FILE_NAME = "tcp_txt_watcher.log"
SOCKET_TIMEOUT_SECONDS = 3.0


def read_text_file(file_path: Path, encoding: str) -> str:
    return file_path.read_text(encoding=encoding)


def should_send(content: str, target_text: str) -> bool:
    return target_text in content


def send_tcp_message(ip: str, port: int, payload: str, timeout_seconds: float = SOCKET_TIMEOUT_SECONDS) -> None:
    with socket.create_connection((ip, port), timeout=timeout_seconds) as sock:
        sock.sendall(payload.encode("utf-8"))


def append_log(
    log_path: Path,
    timestamp_text: str,
    file_path: Path,
    match_result: str,
    action_result: str,
    detail: str,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{timestamp_text} | {file_path.as_posix()} | {match_result} | {action_result} | {detail}\n"
    with log_path.open("a", encoding="utf-8", newline="") as log_file:
        log_file.write(line)


def get_file_state(file_path: Path) -> tuple[int, int]:
    stat_result = file_path.stat()
    return (stat_result.st_mtime_ns, stat_result.st_size)


def collect_changed_txt_files(watch_dir: Path, known_mtimes: dict[str, tuple[int, int]]) -> list[Path]:
    changed_files: list[Path] = []
    for file_path in sorted(watch_dir.glob("*.txt")):
        current_state = get_file_state(file_path)
        old_state = known_mtimes.get(str(file_path))
        if old_state is None or current_state != old_state:
            changed_files.append(file_path)
    return changed_files


def update_known_mtime(file_path: Path, known_mtimes: dict[str, tuple[int, int]]) -> None:
    known_mtimes[str(file_path)] = get_file_state(file_path)


def build_runtime_config(
    watch_dir: Path = Path(WATCH_DIR),
    log_dir: Path = Path(LOG_DIR),
    log_file_name: str = LOG_FILE_NAME,
    target_text: str = TARGET_TEXT,
    send_text: str = SEND_TEXT,
    tcp_ip: str = TCP_IP,
    tcp_port: int = TCP_PORT,
    scan_interval_seconds: int = SCAN_INTERVAL_SECONDS,
    file_encoding: str = FILE_ENCODING,
) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "WATCH_DIR": watch_dir,
        "LOG_PATH": log_dir / log_file_name,
        "TARGET_TEXT": target_text,
        "SEND_TEXT": send_text,
        "TCP_IP": tcp_ip,
        "TCP_PORT": tcp_port,
        "SCAN_INTERVAL_SECONDS": scan_interval_seconds,
        "FILE_ENCODING": file_encoding,
    }


def process_one_file(file_path: Path, config: dict, send_func=None, log_func=None) -> str:
    send_func = send_func or send_tcp_message
    log_func = log_func or append_log
    timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        content = read_text_file(file_path, config["FILE_ENCODING"])
    except Exception as exc:
        log_func(Path(config["LOG_PATH"]), timestamp_text, file_path, "READ_ERROR", "SKIPPED", str(exc))
        return "READ_ERROR"

    if not should_send(content, config["TARGET_TEXT"]):
        log_func(Path(config["LOG_PATH"]), timestamp_text, file_path, "NO_MATCH", "SKIPPED", "target text not found")
        return "NO_MATCH"

    if config["SEND_TEXT"] == "":
        log_func(Path(config["LOG_PATH"]), timestamp_text, file_path, "MATCH", "NO_ACTION", "send text is empty")
        return "NO_ACTION"

    try:
        send_func(config["TCP_IP"], config["TCP_PORT"], config["SEND_TEXT"])
        log_func(Path(config["LOG_PATH"]), timestamp_text, file_path, "MATCH", "SENT", "send ok")
        return "SENT"
    except Exception as exc:
        log_func(Path(config["LOG_PATH"]), timestamp_text, file_path, "MATCH", "SEND_ERROR", str(exc))
        return "SEND_ERROR"


def run_once(config: dict, known_mtimes: dict[str, tuple[int, int]]) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    changed_files = collect_changed_txt_files(config["WATCH_DIR"], known_mtimes)
    for file_path in changed_files:
        result = process_one_file(file_path, config)
        update_known_mtime(file_path, known_mtimes)
        results.append((str(file_path), result))
    return results


def main() -> None:
    config = build_runtime_config()
    if not config["WATCH_DIR"].exists():
        raise FileNotFoundError(f"watch directory not found: {config['WATCH_DIR']}")

    known_mtimes: dict[str, tuple[int, int]] = {}
    while True:
        run_once(config, known_mtimes)
        time.sleep(config["SCAN_INTERVAL_SECONDS"])


if __name__ == "__main__":
    main()
