"""Иконка в системном трее, которая держит бота живым.

Сам бот запускается отдельным процессом через venv-питон, а не внутри этого
приложения: у него тяжёлые зависимости (faster-whisper, ctranslate2), и упаковывать
их в exe незачем — достаточно маленького лончера.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# Путь к проекту зашит по умолчанию, потому что exe лежит вне репозитория.
# Переопределяется переменной окружения, если проект переедет.
PROJECT_ROOT = Path(
    os.environ.get(
        "TELEGRAMHELPER_ROOT",
        r"C:\Users\Данило Кучерук\source\repos\TelegramHelper",
    )
)
PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
ENTRY = PROJECT_ROOT / "main.py"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG = LOG_DIR / "bot.log"
CRASH_LOG = LOG_DIR / "tray_crash.log"

CREATE_NO_WINDOW = 0x08000000
MAX_LOG_BYTES = 5 * 1024 * 1024
MUTEX_NAME = "TelegramHelperTray_single_instance"
ERROR_ALREADY_EXISTS = 183

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_draw import make_image  # noqa: E402


# use_last_error=True обязателен: ctypes не сохраняет GetLastError между вызовами,
# и через windll.kernel32.GetLastError() проверка «уже запущено» молча не работала.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

_mutex_handle = None


def single_instance() -> bool:
    """False — экземпляр уже запущен (иначе второй бот упрётся в лок Qdrant)."""
    global _mutex_handle
    _mutex_handle = _k32.CreateMutexW(None, 0, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def notify(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


def _make_kill_on_close_job():
    """Job Object: бот умирает вместе с треем даже при kill -Force.

    Без этого принудительно закрытый трей оставляет бота сиротой, и тот держит
    лок Qdrant — следующий запуск падает с 'already accessed by another instance'.
    """
    k32 = ctypes.windll.kernel32
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JobExtendedLimits()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        job, JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    return job if ok else None


class BotRunner:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._lock = threading.Lock()
        self._job = _make_kill_on_close_job()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            # лог не крутим по-настоящему, просто не даём разрастись
            if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
                LOG.unlink()
            self._log = open(LOG, "a", encoding="utf-8", errors="replace")
            self._log.write(f"\n=== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            self._log.flush()
            self._proc = subprocess.Popen(
                [str(PY), str(ENTRY)],
                cwd=str(PROJECT_ROOT),
                stdout=self._log,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
            if self._job:
                ctypes.windll.kernel32.AssignProcessToJobObject(
                    self._job, int(self._proc._handle)
                )

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if self._log is not None:
                try:
                    self._log.write(f"=== стоп {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    self._log.close()
                except Exception:
                    pass
                self._log = None

    def restart(self) -> None:
        self.stop()
        time.sleep(1.5)  # даём отпустить лок Qdrant
        self.start()


def main() -> int:
    if not PY.exists() or not ENTRY.exists():
        notify("TelegramHelper", f"Не знайшов проєкт тут:\n{PROJECT_ROOT}")
        return 1

    if not single_instance():
        # Тихо: иначе при входе в Windows можно поймать модальное окно,
        # которое будет ждать клика. Иконка в трее и так уже висит.
        return 0

    import pystray
    from pystray import Menu, MenuItem

    runner = BotRunner()
    runner.start()

    icon = pystray.Icon(
        "telegramhelper",
        make_image(64, running=True),
        "TelegramHelper",
    )

    def status_text(_item=None) -> str:
        return "● Працює" if runner.is_running() else "○ Зупинено"

    def toggle_text(_item=None) -> str:
        return "Зупинити" if runner.is_running() else "Запустити"

    def on_toggle(_icon=None, _item=None) -> None:
        if runner.is_running():
            runner.stop()
        else:
            runner.start()

    def on_restart(_icon=None, _item=None) -> None:
        threading.Thread(target=runner.restart, daemon=True).start()

    def on_logs(_icon=None, _item=None) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LOG.touch(exist_ok=True)
        os.startfile(str(LOG))  # noqa: S606

    def on_folder(_icon=None, _item=None) -> None:
        os.startfile(str(PROJECT_ROOT))  # noqa: S606

    def on_quit(icon_obj=None, _item=None) -> None:
        runner.stop()
        (icon_obj or icon).stop()

    icon.menu = Menu(
        MenuItem(status_text, on_restart, default=True),
        Menu.SEPARATOR,
        MenuItem(toggle_text, on_toggle),
        MenuItem("Перезапустити", on_restart),
        Menu.SEPARATOR,
        MenuItem("Відкрити логи", on_logs),
        MenuItem("Папка проєкту", on_folder),
        Menu.SEPARATOR,
        MenuItem("Вихід", on_quit),
    )

    def watcher() -> None:
        """Держит иконку и подсказку в согласии с реальным состоянием процесса."""
        was = None
        while True:
            now = runner.is_running()
            if now != was:
                icon.icon = make_image(64, running=now)
                icon.title = "TelegramHelper — працює" if now else "TelegramHelper — зупинено"
                try:
                    icon.update_menu()
                except Exception:
                    pass
                was = now
            time.sleep(2)

    threading.Thread(target=watcher, daemon=True).start()
    icon.run()
    runner.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            CRASH_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        notify("TelegramHelper — помилка", traceback.format_exc()[-800:])
        sys.exit(1)
