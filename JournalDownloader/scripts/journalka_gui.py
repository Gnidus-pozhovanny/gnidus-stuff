import asyncio
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from download_top_homework import STATUSES, TASK_LIST_NAME, download_tasks, error_description, summary
from journalka_usage import report_config, send_login


SERVICE = "Journalka Downloader"
ACCOUNT = "saved-account"
FILTERS = ((3, "Текущие"), (0, "Просроченные"), (5, "Удалённые"), (1, "Выполненные"), (2, "На проверке"))


def settings_path():
    if sys.platform == "win32":
        directory = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return directory / "JournalkaDownloader" / "settings.json"


def read_settings():
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(folder, selected):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"folder": str(folder), "statuses": selected}, ensure_ascii=False), encoding="utf-8")


def vault():
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    from keyring.backends.SecretService import Keyring

    return Keyring()


def read_account():
    saved = vault().get_password(SERVICE, ACCOUNT)
    if saved:
        username, password = json.loads(saved)
        if isinstance(username, str) and isinstance(password, str):
            return username, password
    return None


def apply_linux_theme(root):
    if not sys.platform.startswith("linux"):
        return
    background, text = "#f5f7fb", "#172235"
    field, muted, border = "#ffffff", "#4b586b", "#8795a8"
    disabled, accent = "#e4eaf2", "#1856a5"
    for option, color in {
        "*background": background, "*foreground": text, "*disabledForeground": muted,
        "*selectBackground": accent, "*selectForeground": field, "*insertBackground": text,
        "*activeBackground": disabled, "*activeForeground": text,
    }.items():
        root.option_add(option, color, "interactive")
    root.configure(background=background)
    families = set(font.families(root))
    family = next((name for name in ("DejaVu Sans", "Noto Sans", "Liberation Sans") if name in families), None)
    if family:
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            font.nametofont(name, root).configure(family=family, size=10)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", foreground=text, background=background, bordercolor=border,
                    focuscolor=accent, lightcolor=background, darkcolor=background,
                    selectbackground=accent, selectforeground=field)
    for name in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label"):
        style.configure(name, foreground=text, background=background)
        style.map(name, foreground=[("disabled", muted), ("!disabled", text)])
    style.configure("TEntry", foreground=text, fieldbackground=field, insertcolor=text,
                    selectbackground=accent, selectforeground=field)
    style.map("TEntry", foreground=[("disabled", muted), ("!disabled", text)],
              fieldbackground=[("disabled", disabled), ("readonly", disabled), ("!disabled", field)],
              bordercolor=[("focus", accent), ("!focus", border)])
    style.configure("TButton", foreground=text, background=disabled, bordercolor=border)
    style.map("TButton", foreground=[("disabled", muted), ("!disabled", text)],
              background=[("disabled", disabled), ("pressed", "#cbdcf4"), ("active", "#d4e4f9"), ("!disabled", disabled)])
    style.configure("TCheckbutton", foreground=text, background=background,
                    indicatorbackground=field, indicatorforeground=text)
    style.map("TCheckbutton", foreground=[("disabled", muted), ("!disabled", text)],
              background=[("active", "#e6eef9"), ("!active", background)],
              indicatorbackground=[("disabled", disabled), ("selected", accent), ("!selected", field)],
              indicatorforeground=[("disabled", muted), ("selected", field), ("!selected", text)])
    style.configure("Horizontal.TProgressbar", background=accent, troughcolor=disabled, bordercolor=border)


class App:
    def __init__(self, root, *, load_account=True):
        self.root = root
        self.events = Queue()
        self.stop = Event()
        self.busy = False
        self.account_saved = False
        self.closing = False
        settings = read_settings() if load_account else {}
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar()
        self.share_login = tk.BooleanVar(value=False)
        self.reporting = report_config() if load_account else None
        self.folder = tk.StringVar(value=settings.get("folder") or str(Path.home() / "Учёба"))
        self.status = tk.StringVar()
        self.filters = {}
        self.fields = []

        apply_linux_theme(root)
        root.title("Journal Downloader")
        root.minsize(570, 575 if self.reporting else 540)
        root.protocol("WM_DELETE_WINDOW", self.close)
        box = ttk.Frame(root, padding=16)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((("Логин", self.username), ("Пароль", self.password), ("Папка выгрузки", self.folder))):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(box, textvariable=variable, show="●" if row == 1 else "")
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            self.fields.append(entry)
        browse = ttk.Button(box, text="Обзор…", command=self.choose_folder)
        browse.grid(row=2, column=2, padx=(8, 0))
        folder_help = ttk.LabelFrame(box, text="Как сохраняются файлы", padding=(8, 5))
        folder_help.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        ttk.Label(
            folder_help,
            text="Через «Обзор…» выберите одну общую папку. Внутри неё материалы раскладываются по предметам, статусам и типам работ.",
            wraplength=505,
            justify="left",
        ).pack(fill="x")
        remember = ttk.Checkbutton(box, text="Запомнить меня", variable=self.remember, command=self.change_remember)
        remember.grid(row=4, column=1, sticky="w", pady=(2, 10))
        self.fields.extend((browse, remember))

        if self.reporting:
            consent = ttk.Checkbutton(box, text="Отправить статистику", variable=self.share_login)
            consent.grid(row=5, column=0, columnspan=3, sticky="w")
            self.fields.append(consent)

        options = ttk.Frame(box)
        options.grid(row=7, column=0, columnspan=3, sticky="ew")
        saved_filters = settings.get("statuses")
        for kind, label in enumerate(("Домашние задания", "Лабораторные работы")):
            group = ttk.LabelFrame(options, text=label, padding=(12, 5))
            group.pack(side="left", fill="both", expand=True, padx=(0, 8) if kind == 0 else 0)
            self.filters[kind] = {}
            values = saved_filters.get(str(kind)) if isinstance(saved_filters, dict) else None
            for status, name in FILTERS:
                enabled = tk.BooleanVar(value=status in (values if isinstance(values, list) else STATUSES))
                self.filters[kind][status] = enabled
                button = ttk.Checkbutton(group, text=name, variable=enabled)
                button.pack(anchor="w", pady=2)
                self.fields.append(button)

        actions = ttk.Frame(box)
        actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(14, 8))
        self.start_button = ttk.Button(actions, text="Скачать", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Отмена", command=self.cancel, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="determinate", length=160)
        self.progress.pack(side="right")
        self.stage = tk.StringVar()
        ttk.Label(box, textvariable=self.stage).grid(row=10, column=0, columnspan=3, sticky="w")
        ttk.Label(box, textvariable=self.status, wraplength=520).grid(row=11, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(box, text="Для скачивания рекомендуется выключить VPN.", wraplength=520).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        self.fields[0].focus_set()
        root.after(100, self.poll)
        if load_account:
            Thread(target=self.load_account, daemon=True).start()

    def load_account(self):
        try:
            saved = read_account()
        except Exception:
            return
        if saved:
            self.events.put(("account", saved))

    def change_remember(self):
        if not self.remember.get() and self.account_saved:
            try:
                vault().delete_password(SERVICE, ACCOUNT)
            except Exception:
                self.remember.set(True)
                messagebox.showwarning(SERVICE, "Не удалось удалить данные из системного хранилища.")
            else:
                self.account_saved = False

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get(), mustexist=True, parent=self.root)
        if folder:
            self.folder.set(folder)

    def selection(self):
        return {kind: [status for status, enabled in options.items() if enabled.get()] for kind, options in self.filters.items()}

    def start(self):
        username, password = self.username.get().strip(), self.password.get()
        path = self.folder.get().strip()
        selected = self.selection()
        if not username or not password or not path:
            messagebox.showwarning(SERVICE, "Укажите логин, пароль и папку.")
            return
        if not any(selected.values()):
            messagebox.showwarning(SERVICE, "Выберите хотя бы один статус заданий.")
            return
        try:
            folder = Path(path).expanduser().resolve()
        except (ValueError, OSError):
            messagebox.showwarning(SERVICE, "Не удалось открыть выбранную папку.")
            return
        try:
            write_settings(folder, selected)
        except OSError:
            pass
        self.busy = True
        self.stop.clear()
        for field in self.fields:
            field.state(["disabled"])
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.progress.configure(value=0, maximum=1)
        self.stage.set("Подготовка")
        self.status.set("Подключение…")
        consent = self.reporting if self.share_login.get() else None
        Thread(target=self.download, args=(folder, username, password, selected, self.remember.get(), consent), daemon=True).start()

    def download(self, folder, username, password, selected, remember, consent=None):
        def report(text):
            self.events.put(("status", text))

        def on_progress(stage, completed, total):
            self.events.put(("progress", (stage, completed, total)))

        reporting_result = None

        def before_login():
            nonlocal reporting_result
            if consent:
                report("Отправляю статистику в панель по вашему согласию…")
                reporting_result = send_login(consent, username)

        def on_login():
            if remember:
                try:
                    vault().set_password(SERVICE, ACCOUNT, json.dumps([username, password]))
                    self.events.put(("saved", True))
                except Exception:
                    self.events.put(("vault-error", None))

        async def run():
            task = asyncio.create_task(
                download_tasks(
                    folder,
                    username,
                    password,
                    selected,
                    report,
                    on_login,
                    before_login=before_login,
                    on_progress=on_progress,
                    export_list=True,
                )
            )
            while not task.done():
                if self.stop.is_set():
                    task.cancel()
                await asyncio.wait({task}, timeout=0.15)
            return await task

        try:
            result = asyncio.run(run())
        except asyncio.CancelledError:
            self.events.put(("cancelled", "Остановлено. Сохранённые файлы остались в папке."))
        except Exception as error:
            self.events.put(("failed", f"Ошибка: {error_description(error)}"))
        else:
            status = f"{summary(*result)} Список сохранён: {TASK_LIST_NAME}"
            if consent and reporting_result is not None:
                status += " Вход записан в панель." if reporting_result else " Панель недоступна, вход не записан."
            self.events.put(("done", status))

    def cancel(self):
        self.stop.set()
        self.stop_button.state(["disabled"])
        self.status.set("Останавливаю скачивание…")

    def close(self):
        if self.busy:
            self.closing = True
            self.cancel()
        else:
            self.root.destroy()

    def poll(self):
        while True:
            try:
                event, data = self.events.get_nowait()
            except Empty:
                break
            if event == "account":
                self.account_saved = True
                if not self.busy and not self.username.get() and not self.password.get():
                    self.username.set(data[0])
                    self.password.set(data[1])
                    self.remember.set(True)
            elif event == "saved":
                self.account_saved = True
            elif event == "vault-error":
                self.remember.set(False)
                if not self.closing:
                    messagebox.showwarning(SERVICE, "Системное хранилище паролей недоступно. Новые данные не сохранены.")
            elif event == "status" and not self.stop.is_set():
                self.status.set(data)
            elif event == "progress" and not self.stop.is_set():
                stage, completed, total = data
                self.stage.set(f"{stage} · {completed} из {total}" if total > 1 else stage)
                self.progress.configure(value=completed, maximum=total)
            elif event in ("done", "cancelled", "failed"):
                self.busy = False
                if self.closing:
                    self.root.destroy()
                    return
                self.progress.configure(value=int(event == "done"), maximum=1)
                self.stage.set({"done": "Завершено", "cancelled": "Отменено", "failed": "Ошибка"}[event])
                for field in self.fields:
                    field.state(["!disabled"])
                self.start_button.state(["!disabled"])
                self.stop_button.state(["disabled"])
                self.status.set(data)
        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        app = App(root, load_account=False)
        root.update()
        assert root.winfo_viewable()
        assert font.nametofont("TkDefaultFont", root).measure("Логин") > 0
        assert all(set(statuses) == set(STATUSES) for statuses in app.selection().values())
        Path(sys.argv[2]).write_text("GUI OK\n", encoding="utf-8")
        root.destroy()
    else:
        App(root)
        root.mainloop()
