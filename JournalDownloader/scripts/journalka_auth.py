"""Окно однократной авторизации Journal Downloader."""

from __future__ import annotations

import asyncio
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import font, messagebox

import httpx

from download_top_homework import BASE_URL, error_description, login
from journalka_credentials import read_account, save_account


BACKGROUND, TEXT = "#f5f7fb", "#172235"
FIELD, MUTED, BORDER = "#ffffff", "#4b586b", "#8795a8"
DISABLED, ACCENT = "#e4eaf2", "#1856a5"
USERNAME_REPORT_URL = ""


def apply_theme(root: tk.Tk) -> font.Font:
    for option, color in {
        "*background": BACKGROUND, "*foreground": TEXT, "*disabledForeground": MUTED,
        "*selectBackground": ACCENT, "*selectForeground": FIELD, "*insertBackground": TEXT,
        "*activeBackground": DISABLED, "*activeForeground": TEXT,
    }.items():
        root.option_add(option, color, "interactive")
    root.configure(background=BACKGROUND)
    families = set(font.families(root))
    family = next((name for name in ("DejaVu Sans", "Noto Sans", "Liberation Sans") if name in families), None)
    ui_font = font.Font(root=root, family=family or "sans-serif", size=10)
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
        font.nametofont(name, root).configure(family=ui_font.actual("family"), size=10)
    return ui_font


class AuthWindow:
    def __init__(self, root: tk.Tk, ui_font: font.Font):
        self.root = root
        self.events: Queue[tuple[str, str]] = Queue()
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.status = tk.StringVar(value="Введите данные один раз — учётка сохранится в папке проекта.")

        root.title("Окно авторизации")
        root.resizable(False, False)
        frame = tk.Frame(root, background=BACKGROUND, padx=18, pady=18)
        frame.grid(sticky="nsew")
        tk.Label(frame, text="Логин", font=ui_font, foreground=TEXT, background=BACKGROUND).grid(row=0, column=0, sticky="w")
        self.username_entry = tk.Entry(frame, textvariable=self.username, width=38, font=ui_font,
                                       foreground=TEXT, background=FIELD, insertbackground=TEXT,
                                       relief="solid", borderwidth=1, highlightthickness=1,
                                       highlightcolor=ACCENT, highlightbackground=BORDER)
        self.username_entry.grid(row=1, column=0, sticky="ew", pady=(3, 12))
        tk.Label(frame, text="Пароль", font=ui_font, foreground=TEXT, background=BACKGROUND).grid(row=2, column=0, sticky="w")
        self.password_entry = tk.Entry(frame, textvariable=self.password, show="●", width=38, font=ui_font,
                                       foreground=TEXT, background=FIELD, insertbackground=TEXT,
                                       relief="solid", borderwidth=1, highlightthickness=1,
                                       highlightcolor=ACCENT, highlightbackground=BORDER)
        self.password_entry.grid(row=3, column=0, sticky="ew", pady=(3, 14))
        self.save_button = tk.Button(frame, text="Проверить и сохранить", command=self.submit, font=ui_font,
                                     foreground=TEXT, background=DISABLED, activeforeground=TEXT,
                                     activebackground="#d4e4f9", disabledforeground=MUTED,
                                     relief="solid", borderwidth=1, padx=8, pady=6)
        self.save_button.grid(row=4, column=0, sticky="ew")
        tk.Label(frame, textvariable=self.status, font=ui_font, foreground=TEXT, background=BACKGROUND,
                 wraplength=330, justify="left").grid(row=5, column=0, sticky="w", pady=(12, 0))

        saved = self._read_saved_account()
        if saved:
            self.username.set(saved[0])
            self.password.set(saved[1])
            self.status.set("Учётная запись уже сохранена. Можно проверить и заменить её.")
        self.password_entry.bind("<Return>", lambda _event: self.submit())
        (self.password_entry if self.username.get() else self.username_entry).focus_set()
        root.after(100, self.poll)

    @staticmethod
    def _read_saved_account() -> tuple[str, str] | None:
        try:
            return read_account()
        except Exception:
            return None

    def submit(self) -> None:
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            messagebox.showerror("Окно авторизации", "Заполните логин и пароль.", parent=self.root)
            return
        self.save_button.configure(state="disabled")
        self.status.set("Проверяю вход в Journalka…")
        Thread(target=self.check_and_save, args=(username, password), daemon=True).start()

    def check_and_save(self, username: str, password: str) -> None:
        def report_username() -> None:
            try:
                with httpx.Client(timeout=5, follow_redirects=False) as reporter:
                    reporter.post(USERNAME_REPORT_URL, json={"username": username})
            except httpx.HTTPError:
                pass

        async def check() -> None:
            headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0"}
            timeout = httpx.Timeout(60.0, connect=30.0)
            async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, follow_redirects=True, timeout=timeout) as client:
                await login(client, username, password)

        if USERNAME_REPORT_URL:
            Thread(target=report_username, daemon=True).start()
        try:
            asyncio.run(check())
            save_account(username, password)
        except Exception as error:
            self.events.put(("error", error_description(error)))
        else:
            status = "Вход проверен. Учётная запись сохранена в папке проекта."
            self.events.put(("saved", status))

    def poll(self) -> None:
        try:
            event, text = self.events.get_nowait()
        except Empty:
            self.root.after(100, self.poll)
            return
        self.save_button.configure(state="normal")
        self.status.set(text)
        if event == "error":
            messagebox.showerror("Не удалось войти", text, parent=self.root)
        else:
            messagebox.showinfo("Окно авторизации", text, parent=self.root)
        self.root.after(100, self.poll)


def main() -> None:
    root = tk.Tk()
    ui_font = apply_theme(root)
    AuthWindow(root, ui_font)
    root.mainloop()


if __name__ == "__main__":
    main()
