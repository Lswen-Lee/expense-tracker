"""記帳系統主程式。資料存在同資料夾的「記帳資料.db」，每次變更都會自動保存。"""
import sys

sys.dont_write_bytecode = True  # 不在資料夾裡留下 __pycache__

import calendar
import ctypes
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from storage import EXPENSE, INCOME, KIND_LABEL, Ledger, following_month, month_key

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "記帳資料.db"
ICON_PATH = APP_DIR / "logo.ico"

FONT = "Microsoft JhengHei UI"
SURFACE = "#ffffff"
INK, INK_2, MUTED, LINE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
ACCENT, SELECTED_BG, BLANK_BG = "#2a78d6", "#e3eefb", "#f7f7f5"
EXPENSE_COLOR, INCOME_COLOR = "#d03b3b", "#006300"
GOOD, WARNING, CRITICAL, WARNING_TEXT = "#0ca30c", "#fab219", "#d03b3b", "#8a5a00"
# 圓餅圖的顏色跟著分類走（依分類清單的順序），同一個分類每個月都是同一個顏色
PIE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
REST_COLOR = "#b5b3ab"
MAX_SLICES = 6
WEEKDAYS = "一二三四五六日"  # date.weekday()：0 是週一


def money(amount):
    return f"{amount:,}"


def share_text(amount, total):
    share = amount / total
    return "<1%" if share < 0.005 else f"{share:.0%}"


def parse_amount(text, allow_zero=False):
    text = text.replace(",", "").replace("，", "").strip()
    if not text.isdecimal() or (int(text) == 0 and not allow_zero):
        raise ValueError("金額請輸入整數" if allow_zero else "金額請輸入大於 0 的整數")
    if int(text) > 999_999_999:
        raise ValueError("金額太大了")
    return int(text)


def budget_state(spent, budget):
    """回傳 (進度條顏色, 文字顏色, 說明)：用到 80% 提醒，超過預算警告。"""
    if spent > budget:
        return CRITICAL, CRITICAL, f"⚠ 超支 {money(spent - budget)} 元"
    if spent >= budget * 0.8:
        return WARNING, WARNING_TEXT, f"⚠ 已用 {spent / budget:.0%}"
    return GOOD, INK_2, f"已用 {spent / budget:.0%}"


def draw_bar(canvas, ratio, color):
    width, height = int(canvas["width"]), int(canvas["height"])
    canvas.delete("all")
    canvas.create_rectangle(0, 0, width, height, fill=LINE, width=0)
    if ratio > 0:
        canvas.create_rectangle(0, 0, max(2, width * min(ratio, 1)), height, fill=color, width=0)


class Dialog(tk.Toplevel):
    """有「確定／取消」的對話框。子類別實作 collect()，輸入有問題時丟出 ValueError。"""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=SURFACE, padx=18, pady=14)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.first = None
        self.body = ttk.Frame(self)
        self.body.pack(anchor="w")
        self.error = ttk.Label(self, style="Error.TLabel")
        self.error.pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(self)
        buttons.pack(anchor="e", pady=(8, 0))
        ttk.Button(buttons, text="確定", command=self.confirm).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda event: self.confirm())
        self.bind("<Escape>", lambda event: self.destroy())

    def show(self):
        parent = self.master
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        self.grab_set()
        if self.first:
            self.first.focus_set()
            self.first.selection_range(0, "end")
        self.wait_window()
        return self.result

    def confirm(self):
        try:
            self.result = self.collect()
        except ValueError as error:
            self.error.config(text=str(error))
            return
        self.destroy()


class InputDialog(Dialog):
    """只問一個值的對話框；parse 負責檢查並轉換輸入。"""

    def __init__(self, parent, title, prompt, parse, initial=""):
        super().__init__(parent, title)
        self.parse = parse
        self.value = tk.StringVar(value=initial)
        ttk.Label(self.body, text=prompt).pack(anchor="w")
        self.first = ttk.Entry(self.body, textvariable=self.value, width=28)
        self.first.pack(anchor="w", pady=(8, 0))

    def collect(self):
        return self.parse(self.value.get())


class EntryDialog(Dialog):
    """編輯記錄、新增或編輯固定收支共用的表單。"""

    def __init__(self, parent, ledger, title, values, recurring=False, ask_first_month=False):
        super().__init__(parent, title)
        self.ledger = ledger
        self.recurring = recurring
        self.original = (values["kind"], values["category"])
        self.kind = tk.StringVar(value=values["kind"])
        self.amount = tk.StringVar(value=str(values["amount"]))
        self.category = tk.StringVar(value=values["category"])
        self.note = tk.StringVar(value=values["note"])
        self.first_month = tk.StringVar(value="this") if ask_first_month else None

        kinds = ttk.Frame(self.body)
        for kind in (EXPENSE, INCOME):
            ttk.Radiobutton(kinds, text=KIND_LABEL[kind], value=kind, variable=self.kind,
                            command=self.load_categories).pack(side="left", padx=(0, 12))
        self.add_row("類型", kinds)
        if not recurring:
            self.date = tk.StringVar(value=values["date"].isoformat())
            self.add_row("日期", ttk.Entry(self.body, textvariable=self.date, width=14))
        self.first = ttk.Entry(self.body, textvariable=self.amount, width=14)
        self.add_row("金額", self.first)
        self.category_box = ttk.Combobox(self.body, textvariable=self.category, state="readonly", width=12)
        self.add_row("分類", self.category_box)
        self.add_row("備註", ttk.Entry(self.body, textvariable=self.note, width=28))
        if recurring:
            self.day = tk.StringVar(value=str(values["day"]))
            day = ttk.Frame(self.body)
            ttk.Spinbox(day, from_=1, to=31, textvariable=self.day, width=5).pack(side="left")
            ttk.Label(day, text="號（沒有這一天的月份記在月底）", style="Muted.TLabel").pack(side="left", padx=6)
            self.add_row("每月", day)
        if ask_first_month:
            start = ttk.Frame(self.body)
            for value, text in (("this", "本月（日期已過會立刻補記）"), ("next", "下個月")):
                ttk.Radiobutton(start, text=text, value=value, variable=self.first_month).pack(
                    side="left", padx=(0, 12))
            self.add_row("開始", start)
        self.load_categories()

    def add_row(self, label, widget):
        row = self.body.grid_size()[1]
        ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
        widget.grid(row=row, column=1, sticky="w", pady=5)

    def load_categories(self):
        names = self.ledger.categories(self.kind.get())
        # 編輯舊記錄時，即使它的分類後來被刪除了也要能保留
        if self.kind.get() == self.original[0] and self.original[1] and self.original[1] not in names:
            names.append(self.original[1])
        self.category_box["values"] = names
        if self.category.get() not in names:
            self.category.set(names[0] if names else "")

    def collect(self):
        result = {"kind": self.kind.get(), "amount": parse_amount(self.amount.get())}
        if not self.category.get():
            raise ValueError("請選擇分類")
        result["category"] = self.category.get()
        result["note"] = self.note.get().strip()
        if self.recurring:
            day = self.day.get().strip()
            if not day.isdecimal() or not 1 <= int(day) <= 31:
                raise ValueError("每月日期請輸入 1 到 31")
            result["day"] = int(day)
            if self.first_month:
                today = date.today()
                month = month_key(today.year, today.month)
                result["first_month"] = month if self.first_month.get() == "this" else following_month(month)
        else:
            try:
                result["day"] = date.fromisoformat(self.date.get().strip())
            except ValueError:
                raise ValueError("日期格式請用 年-月-日，例如 2026-10-13") from None
        return result


class NoteSuggest:
    """備註輸入框的提示清單：列出以前打過的備註，選了會帶入當時的分類與金額。"""

    def __init__(self, app, entry, variable, fetch, on_pick):
        self.app, self.entry, self.variable = app, entry, variable
        self.fetch, self.on_pick = fetch, on_pick
        self.rows = []
        self.quiet = False
        self.box = tk.Listbox(app, activestyle="none", exportselection=False, takefocus=0,
                              relief="solid", borderwidth=1, highlightthickness=0,
                              selectbackground=ACCENT, selectforeground=SURFACE)
        # 用變數追蹤而不是鍵盤事件，中文輸入法選字完成後才會觸發
        variable.trace_add("write", lambda *_: self.update())
        entry.bind("<Down>", lambda event: self.move(1))
        entry.bind("<Up>", lambda event: self.move(-1))
        entry.bind("<Escape>", lambda event: self.hide())
        entry.bind("<FocusOut>", lambda event: app.after(120, self.hide))
        self.box.bind("<Button-1>", lambda event: self.choose(self.box.nearest(event.y)))

    def set_quietly(self, text):
        """填入文字但不跳出提示清單。"""
        self.quiet = True
        self.variable.set(text)
        self.quiet = False
        self.hide()

    def update(self):
        text = self.variable.get().strip()
        self.rows = [] if self.quiet or not text else self.fetch(text)
        if not self.rows:
            self.hide()
            return
        self.box.delete(0, "end")
        for row in self.rows:
            self.box.insert("end", f" {row['note']}　·　{row['category']}　{money(row['amount'])}")
        self.box.config(height=len(self.rows))
        app, entry = self.app, self.entry
        width = max(entry.winfo_width(), app.px(250))
        height = self.box.winfo_reqheight()
        x = min(entry.winfo_rootx() - app.winfo_rootx(), app.winfo_width() - width - 4)
        y = entry.winfo_rooty() - app.winfo_rooty()
        below = y + entry.winfo_height()
        # 下方放不下就改顯示在輸入框上方
        self.box.place(x=x, y=below if below + height <= app.winfo_height() else y - height, width=width)
        self.box.lift()

    def hide(self):
        self.box.place_forget()

    def move(self, step):
        if not self.box.winfo_ismapped():
            return None
        current = self.box.curselection()
        index = current[0] + step if current else (0 if step > 0 else len(self.rows) - 1)
        self.box.selection_clear(0, "end")
        self.box.selection_set(max(0, min(index, len(self.rows) - 1)))
        return "break"

    def choose_selected(self):
        """清單上有選取項目時帶入它並回傳 True。"""
        if self.box.winfo_ismapped() and self.box.curselection():
            self.choose(self.box.curselection()[0])
            return True
        return False

    def choose(self, index):
        row = self.rows[index]
        self.hide()
        self.on_pick(row)


class App(tk.Tk):
    def __init__(self, db_path=DB_PATH):
        super().__init__()
        self.withdraw()
        self.title("記帳系統")
        if ICON_PATH.exists():
            self.iconbitmap(default=str(ICON_PATH))  # default：對話框也用同一個圖示
        self.ledger = Ledger(db_path)
        self.scale = self.winfo_fpixels("1i") / 96
        self.selected = date.today()
        self.year, self.month = self.selected.year, self.selected.month
        self.status_job = None
        self._setup_style()
        self._build_header()
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12)
        for title, build in (("記帳", self._build_ledger_tab), ("統計圖表", self._build_stats_tab),
                             ("固定收支", self._build_recurring_tab), ("設定與備份", self._build_settings_tab)):
            frame = ttk.Frame(self.tabs, padding=14)
            self.tabs.add(frame, text=f"  {title}  ")
            build(frame)
        self.status = ttk.Label(self, style="Muted.TLabel", padding=(16, 6))
        self.status.pack(fill="x")

        created = self.ledger.apply_recurring()
        self.refresh_all()
        if created:
            self.set_status(f"已自動補上 {created} 筆固定收支記錄")
        self.resizable(False, False)
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
        y = max((self.winfo_screenheight() - self.winfo_reqheight()) // 2 - 30, 0)
        self.geometry(f"+{max(x, 0)}+{y}")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.deiconify()
        self.amount_entry.focus_set()

    def px(self, pixels):
        """依螢幕縮放比例換算像素，高解析度螢幕上才不會變得太小。"""
        return round(pixels * self.scale)

    def close(self):
        self.ledger.close()
        self.destroy()

    def report_callback_exception(self, exc_type, exc, traceback):
        # 用 pythonw 執行時沒有主控台，錯誤要顯示出來才不會無聲無息
        messagebox.showerror("發生錯誤", f"{exc_type.__name__}: {exc}", parent=self)

    def set_status(self, text):
        if self.status_job:
            self.after_cancel(self.status_job)
        self.status.config(text=text)
        self.status_job = self.after(8000, lambda: self.status.config(text=""))

    # ---- 畫面建立 ----

    def _setup_style(self):
        self.configure(bg=SURFACE)
        self.option_add("*Font", (FONT, 10))
        style = ttk.Style(self)
        style.configure(".", font=(FONT, 10), background=SURFACE)
        style.configure("Treeview", rowheight=self.px(28))
        style.configure("Title.TLabel", font=(FONT, 13, "bold"))
        style.configure("Muted.TLabel", foreground=INK_2)
        style.configure("Error.TLabel", foreground=CRITICAL)
        style.configure("Chip.TButton", padding=(4, 0))

    def _build_header(self):
        header = ttk.Frame(self, padding=(16, 12, 16, 4))
        header.pack(fill="x")
        ttk.Button(header, text="◀", width=3, command=lambda: self.shift_month(-1)).pack(side="left")
        self.month_label = ttk.Label(header, style="Title.TLabel", width=13, anchor="center")
        self.month_label.pack(side="left")
        ttk.Button(header, text="▶", width=3, command=lambda: self.shift_month(1)).pack(side="left")
        ttk.Button(header, text="今天", width=5,
                   command=lambda: self.select_day(date.today())).pack(side="left", padx=(10, 0))
        self.total_labels = {}
        for key, text, color in (("balance", "結餘", INK), (EXPENSE, "支出", EXPENSE_COLOR),
                                 (INCOME, "收入", INCOME_COLOR)):
            box = ttk.Frame(header)
            box.pack(side="right", padx=(28, 0))
            ttk.Label(box, text=text, style="Muted.TLabel").pack(anchor="e")
            self.total_labels[key] = ttk.Label(box, font=(FONT, 15, "bold"), foreground=color)
            self.total_labels[key].pack(anchor="e")

        budget = ttk.Frame(self, padding=(16, 0, 16, 10))
        budget.pack(fill="x")
        ttk.Label(budget, text="分類預算合計", style="Muted.TLabel").pack(side="left")
        self.budget_bar = tk.Canvas(budget, width=self.px(200), height=self.px(10), bg=SURFACE,
                                    highlightthickness=0)
        self.budget_bar.pack(side="left", padx=10)
        self.budget_text = ttk.Label(budget)
        self.budget_text.pack(side="left")
        self.budget_alert = ttk.Label(budget)
        self.budget_alert.pack(side="left", padx=(16, 0))

    def _build_ledger_tab(self, parent):
        grid = ttk.Frame(parent)
        grid.pack(side="left", anchor="n")
        for column, name in enumerate("日一二三四五六"):
            ttk.Label(grid, text=name, style="Muted.TLabel").grid(row=0, column=column, pady=(0, 4))
        self.cells = []
        self.cell_days = [0] * 42
        for index in range(42):
            cell = tk.Frame(grid, width=self.px(84), height=self.px(64), highlightthickness=1)
            cell.grid(row=1 + index // 7, column=index % 7, padx=1, pady=1)
            cell.pack_propagate(False)
            day = tk.Label(cell, anchor="w")
            day.pack(fill="x", padx=5, pady=(2, 0))
            expense = tk.Label(cell, anchor="e", fg=EXPENSE_COLOR, font=(FONT, 9), pady=0)
            expense.pack(fill="x", padx=4)
            income = tk.Label(cell, anchor="e", fg=INCOME_COLOR, font=(FONT, 9), pady=0)
            income.pack(fill="x", padx=4)
            for widget in (cell, day, expense, income):
                widget.bind("<Button-1>", lambda event, index=index: self.click_cell(index))
            self.cells.append((cell, day, expense, income))

        right = ttk.Frame(parent)
        right.pack(side="left", anchor="n", padx=(18, 0))
        self.day_title = ttk.Label(right, style="Title.TLabel")
        self.day_title.pack(anchor="w")
        table = ttk.Frame(right)
        table.pack(fill="x", pady=(8, 0))
        self.day_tree = ttk.Treeview(table, columns=("category", "note", "amount"), show="headings",
                                     height=6, selectmode="browse")
        for column, text, width, anchor in (("category", "分類", 90, "w"), ("note", "備註", 190, "w"),
                                            ("amount", "金額", 90, "e")):
            self.day_tree.heading(column, text=text, anchor=anchor)
            self.day_tree.column(column, width=self.px(width), anchor=anchor)
        self.day_tree.tag_configure(EXPENSE, foreground=EXPENSE_COLOR)
        self.day_tree.tag_configure(INCOME, foreground=INCOME_COLOR)
        scrollbar = ttk.Scrollbar(table, command=self.day_tree.yview)
        self.day_tree.config(yscrollcommand=scrollbar.set)
        self.day_tree.pack(side="left")
        scrollbar.pack(side="left", fill="y")
        self.day_tree.bind("<<TreeviewSelect>>", lambda event: self.update_day_buttons())
        self.day_tree.bind("<Double-1>", lambda event: self.edit_selected())
        self.day_tree.bind("<Delete>", lambda event: self.delete_selected())

        footer = ttk.Frame(right)
        footer.pack(fill="x", pady=(6, 0))
        self.day_summary = ttk.Label(footer, style="Muted.TLabel")
        self.day_summary.pack(side="left")
        self.delete_button = ttk.Button(footer, text="刪除", width=6, command=self.delete_selected)
        self.delete_button.pack(side="right")
        self.edit_button = ttk.Button(footer, text="編輯", width=6, command=self.edit_selected)
        self.edit_button.pack(side="right", padx=(0, 6))

        self.kind_var = tk.StringVar(value=EXPENSE)
        self.amount_var, self.category_var, self.note_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.form = ttk.LabelFrame(right, padding=(12, 6, 12, 8))
        self.form.pack(fill="x", pady=(12, 0))
        kinds = ttk.Frame(self.form)
        for kind in (EXPENSE, INCOME):
            ttk.Radiobutton(kinds, text=KIND_LABEL[kind], value=kind, variable=self.kind_var,
                            command=self.on_kind_change).pack(side="left", padx=(0, 12))
        self.amount_entry = ttk.Entry(self.form, textvariable=self.amount_var, width=12)
        self.category_box = ttk.Combobox(self.form, textvariable=self.category_var, state="readonly", width=10)
        note_entry = ttk.Entry(self.form, textvariable=self.note_var)
        self.chips = ttk.Frame(self.form)
        self.form_error = ttk.Label(self.form, style="Error.TLabel")
        ttk.Label(self.form, text="類型").grid(row=0, column=0, sticky="w", pady=4)
        kinds.grid(row=0, column=1, columnspan=3, sticky="w", padx=(10, 0))
        ttk.Label(self.form, text="金額").grid(row=1, column=0, sticky="w", pady=4)
        self.amount_entry.grid(row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Label(self.form, text="分類").grid(row=1, column=2, sticky="e", padx=(14, 0))
        self.category_box.grid(row=1, column=3, sticky="e", padx=(10, 0))
        ttk.Label(self.form, text="備註").grid(row=2, column=0, sticky="w", pady=4)
        note_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0))
        ttk.Button(self.form, text="新增", command=self.add_from_form).grid(row=2, column=3, sticky="e")
        ttk.Label(self.form, text="常用").grid(row=3, column=0, sticky="w", pady=4)
        self.chips.grid(row=3, column=1, columnspan=3, sticky="w", padx=(10, 0))
        self.form_error.grid(row=4, column=0, columnspan=4, sticky="w")
        self.form.columnconfigure(2, weight=1)

        self.suggest = NoteSuggest(
            self, note_entry, self.note_var,
            fetch=lambda text: self.ledger.note_suggestions(self.kind_var.get(), text),
            on_pick=lambda row: self.fill_from_memory(row, keep_amount=True))
        self.amount_entry.bind("<Return>", lambda event: self.add_from_form())
        note_entry.bind("<Return>", lambda event: self.suggest.choose_selected() or self.add_from_form())

    def _build_stats_tab(self, parent):
        left = ttk.Frame(parent)
        left.pack(side="left", anchor="n")
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.stats_title = ttk.Label(top, style="Title.TLabel")
        self.stats_title.pack(side="left")
        self.stats_kind = tk.StringVar(value=EXPENSE)
        for kind in (INCOME, EXPENSE):
            ttk.Radiobutton(top, text=KIND_LABEL[kind], value=kind, variable=self.stats_kind,
                            command=self.refresh_stats).pack(side="right", padx=(12, 0))
        chart = ttk.Frame(left)
        chart.pack(pady=(14, 0))
        self.donut = tk.Canvas(chart, width=self.px(250), height=self.px(250), bg=SURFACE,
                               highlightthickness=0)
        self.donut.pack(side="left")
        self.legend = ttk.Frame(chart, width=self.px(230))
        self.legend.pack(side="left", anchor="n", padx=(20, 0), pady=(10, 0))

        ttk.Separator(parent, orient="vertical").pack(side="left", fill="y", padx=22)
        right = ttk.Frame(parent)
        right.pack(side="left", anchor="n", fill="x", expand=True)
        ttk.Label(right, text="各分類預算", style="Title.TLabel").pack(anchor="w")
        self.budget_rows = ttk.Frame(right)
        self.budget_rows.pack(anchor="w", pady=(14, 0))

    def _build_recurring_tab(self, parent):
        ttk.Label(parent, style="Muted.TLabel",
                  text="每個月固定的收入或支出，例如房租、薪水、訂閱費。到了指定日期，開啟程式時會自動補上記錄。"
                  ).pack(anchor="w")
        self.rec_tree = ttk.Treeview(parent, show="headings", height=10, selectmode="browse",
                                     columns=("day", "kind", "category", "note", "amount", "state"))
        for column, text, width, anchor in (
                ("day", "日期", 110, "w"), ("kind", "類型", 80, "w"), ("category", "分類", 120, "w"),
                ("note", "備註", 300, "w"), ("amount", "金額", 120, "e"), ("state", "狀態", 90, "center")):
            self.rec_tree.heading(column, text=text, anchor=anchor)
            self.rec_tree.column(column, width=self.px(width), anchor=anchor)
        self.rec_tree.tag_configure("off", foreground=MUTED)
        self.rec_tree.pack(anchor="w", pady=(10, 0))
        self.rec_tree.bind("<<TreeviewSelect>>", lambda event: self.update_recurring_buttons())
        self.rec_tree.bind("<Double-1>", lambda event: self.edit_recurring())
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=(10, 0))
        ttk.Button(buttons, text="新增", command=self.add_recurring).pack(side="left", padx=(0, 6))
        self.rec_edit = ttk.Button(buttons, text="編輯", command=self.edit_recurring)
        self.rec_toggle = ttk.Button(buttons, text="停用", command=self.toggle_recurring)
        self.rec_delete = ttk.Button(buttons, text="刪除", command=self.delete_recurring)
        for button in (self.rec_edit, self.rec_toggle, self.rec_delete):
            button.pack(side="left", padx=(0, 6))

    def _build_settings_tab(self, parent):
        self.category_trees = {}
        for kind, title, columns in ((EXPENSE, "支出分類與每月預算", ("name", "budget")),
                                     (INCOME, "收入分類", ("name",))):
            frame = ttk.LabelFrame(parent, text=title, padding=12)
            frame.pack(side="left", anchor="n", padx=(0, 16))
            tree = ttk.Treeview(frame, columns=columns, show="headings", height=9, selectmode="browse")
            tree.heading("name", text="分類", anchor="w")
            tree.column("name", width=self.px(130))
            if kind == EXPENSE:
                tree.heading("budget", text="每月預算", anchor="e")
                tree.column("budget", width=self.px(110), anchor="e")
                tree.bind("<Double-1>", lambda event: self.edit_budget())
            tree.pack(fill="x")
            buttons = ttk.Frame(frame)
            buttons.pack(anchor="w", pady=(10, 0))
            ttk.Button(buttons, text="新增分類", command=lambda kind=kind: self.add_category(kind)).pack(
                side="left", padx=(0, 6))
            if kind == EXPENSE:
                ttk.Button(buttons, text="設定預算", command=self.edit_budget).pack(side="left", padx=(0, 6))
            ttk.Button(buttons, text="刪除分類", command=lambda kind=kind: self.delete_category(kind)).pack(
                side="left")
            self.category_trees[kind] = tree

        frame = ttk.LabelFrame(parent, text="匯出與備份", padding=12)
        frame.pack(side="left", anchor="n", fill="x", expand=True)
        for text, command, hint in (
                ("匯出 CSV…", self.export_csv, "把所有記錄存成可用 Excel 開啟的檔案。"),
                ("備份資料…", self.backup, "把記錄、分類、預算與固定收支整份存成備份檔。"),
                ("從備份還原…", self.restore, "用備份檔的內容取代目前的所有資料。")):
            ttk.Button(frame, text=text, width=14, command=command).pack(anchor="w")
            ttk.Label(frame, text=hint, style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        ttk.Label(frame, text=f"資料檔：{self.ledger.path.name}（和程式放在同一個資料夾）",
                  style="Muted.TLabel").pack(anchor="w")

    # ---- 月份與日期 ----

    def select_day(self, day):
        self.selected = day
        self.year, self.month = day.year, day.month
        self.refresh_month()

    def shift_month(self, step):
        year, month = divmod(self.year * 12 + self.month - 1 + step, 12)
        month += 1
        day = min(self.selected.day, calendar.monthrange(year, month)[1])
        self.select_day(date(year, month, day))

    def click_cell(self, index):
        if self.cell_days[index]:
            self.select_day(date(self.year, self.month, self.cell_days[index]))

    # ---- 重新整理畫面 ----

    def refresh_all(self):
        self.refresh_form_categories()
        self.refresh_month()
        self.refresh_recurring()
        self.refresh_settings()

    def refresh_month(self):
        """記錄有變動，或切換了月份、日期之後，重畫所有跟月份有關的畫面。"""
        self.refresh_header()
        self.refresh_calendar()
        self.refresh_day()
        self.refresh_stats()
        self.refresh_chips()

    def refresh_header(self):
        self.month_label.config(text=f"{self.year} 年 {self.month} 月")
        totals = self.ledger.month_totals(self.year, self.month)
        balance = totals[INCOME] - totals[EXPENSE]
        self.total_labels[INCOME].config(text=money(totals[INCOME]))
        self.total_labels[EXPENSE].config(text=money(totals[EXPENSE]))
        self.total_labels["balance"].config(text=money(balance), foreground=INK if balance >= 0 else CRITICAL)

        usage = self.ledger.budget_usage(self.year, self.month)
        if not usage:
            draw_bar(self.budget_bar, 0, GOOD)
            self.budget_text.config(text="尚未設定，可到「設定與備份」為各分類設定每月預算", foreground=MUTED)
            self.budget_alert.config(text="")
            return
        spent = sum(row[1] for row in usage)
        budget = sum(row[2] for row in usage)
        draw_bar(self.budget_bar, spent / budget, budget_state(spent, budget)[0])
        self.budget_text.config(text=f"{money(spent)} / {money(budget)}（{spent / budget:.0%}）", foreground=INK)
        over = [f"{name}超支 {money(used - limit)} 元" for name, used, limit in usage if used > limit]
        near = [f"{name}已用 {used / limit:.0%}" for name, used, limit in usage
                if limit * 0.8 <= used <= limit]
        alerts = over + near
        text = "⚠ " + "、".join(alerts[:3]) + (f" 等 {len(alerts)} 項" if len(alerts) > 3 else "")
        self.budget_alert.config(text=text if alerts else "", foreground=CRITICAL if over else WARNING_TEXT)

    def refresh_calendar(self):
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(self.year, self.month)
        days = [day for week in weeks for day in week]
        self.cell_days = days + [0] * (42 - len(days))
        totals = self.ledger.daily_totals(self.year, self.month)
        today = date.today()
        for (cell, day_label, expense, income), day in zip(self.cells, self.cell_days):
            is_today = bool(day) and date(self.year, self.month, day) == today
            background = BLANK_BG if not day else SELECTED_BG if day == self.selected.day else SURFACE
            amounts = totals.get(day, {})
            cell.config(bg=background, highlightbackground=ACCENT if is_today else LINE,
                        highlightthickness=2 if is_today else 1, cursor="hand2" if day else "")
            day_label.config(text=day or "", bg=background, fg=ACCENT if is_today else INK,
                             font=(FONT, 10, "bold" if is_today else "normal"))
            expense.config(text=f"-{money(amounts[EXPENSE])}" if EXPENSE in amounts else "", bg=background)
            income.config(text=f"+{money(amounts[INCOME])}" if INCOME in amounts else "", bg=background)

    def refresh_day(self):
        day = self.selected
        title = f"{day.month} 月 {day.day} 日（{WEEKDAYS[day.weekday()]}）"
        self.day_title.config(text=title + ("　今天" if day == date.today() else ""))
        self.form.config(text=f"新增記錄到 {day.month} 月 {day.day} 日")
        self.day_tree.delete(*self.day_tree.get_children())
        totals = {EXPENSE: 0, INCOME: 0}
        rows = self.ledger.transactions_on(day)
        for row in rows:
            sign = "-" if row["kind"] == EXPENSE else "+"
            note = ("〔固定〕" if row["recurring_id"] else "") + row["note"]
            self.day_tree.insert("", "end", iid=str(row["id"]), tags=(row["kind"],),
                                 values=(row["category"], note, sign + money(row["amount"])))
            totals[row["kind"]] += row["amount"]
        self.day_summary.config(
            text=f"當日支出 {money(totals[EXPENSE])}　收入 {money(totals[INCOME])}" if rows else "這一天還沒有記錄")
        self.update_day_buttons()

    def update_day_buttons(self):
        state = "normal" if self.day_tree.selection() else "disabled"
        self.edit_button.config(state=state)
        self.delete_button.config(state=state)

    def refresh_form_categories(self, reset=False):
        """更新分類選單；切換類型時預設帶入上次用過的分類。"""
        kind = self.kind_var.get()
        names = self.ledger.categories(kind)
        self.category_box["values"] = names
        if reset or self.category_var.get() not in names:
            last = self.ledger.get_setting(f"last_category_{kind}")
            self.category_var.set(last if last in names else (names[0] if names else ""))

    def refresh_chips(self):
        for child in self.chips.winfo_children():
            child.destroy()
        rows = self.ledger.note_suggestions(self.kind_var.get(), limit=4)
        if not rows:
            ttk.Label(self.chips, text="記過的項目會出現在這裡，點一下就能帶入",
                      style="Muted.TLabel").pack(side="left")
        for row in rows:
            note = row["note"]
            ttk.Button(self.chips, text=note if len(note) <= 5 else note[:4] + "…", style="Chip.TButton",
                       width=0, command=lambda row=row: self.fill_from_memory(row, keep_amount=False)
                       ).pack(side="left", padx=(0, 6))

    # ---- 記帳 ----

    def on_kind_change(self):
        self.refresh_form_categories(reset=True)
        self.refresh_chips()
        self.suggest.hide()

    def fill_from_memory(self, row, keep_amount):
        """帶入以前記過的項目；keep_amount 表示已經打好的金額不要蓋掉。"""
        self.suggest.set_quietly(row["note"])
        if row["category"] in self.category_box["values"]:
            self.category_var.set(row["category"])
        if not (keep_amount and self.amount_var.get().strip()):
            self.amount_var.set(str(row["amount"]))
        self.amount_entry.focus_set()
        self.amount_entry.selection_range(0, "end")
        self.amount_entry.icursor("end")

    def add_from_form(self):
        self.suggest.hide()
        kind, category, day = self.kind_var.get(), self.category_var.get(), self.selected
        try:
            amount = parse_amount(self.amount_var.get())
            if not category:
                raise ValueError("請先到「設定與備份」新增分類")
        except ValueError as error:
            self.form_error.config(text=str(error))
            self.amount_entry.focus_set()
            return
        self.form_error.config(text="")
        new_id = self.ledger.add_transaction(day, kind, amount, category, self.note_var.get().strip())
        self.ledger.set_setting(f"last_category_{kind}", category)
        self.amount_var.set("")
        self.suggest.set_quietly("")
        self.refresh_month()
        self.day_tree.see(str(new_id))
        self.set_status(f"已記下：{day.month}/{day.day} {KIND_LABEL[kind]}　{category}　{money(amount)} 元")
        self.amount_entry.focus_set()
        if kind == EXPENSE:
            self.budget_notice(category, day, amount)

    def budget_notice(self, category, day, amount):
        """新增支出後，如果該分類剛好跨過預算的 80% 或 100% 就跳出提醒。"""
        budget = self.ledger.budgets().get(category)
        if not budget:
            return
        spent = dict(self.ledger.category_totals(day.year, day.month, EXPENSE)).get(category, 0)
        before = spent - amount
        detail = f"「{category}」{day.month} 月已用 {money(spent)} / {money(budget)} 元"
        if before <= budget < spent:
            messagebox.showwarning("預算提醒", f"{detail}，超出預算 {money(spent - budget)} 元。", parent=self)
        elif before < budget * 0.8 <= spent:
            messagebox.showinfo("預算提醒", f"{detail}，已達預算的 {spent / budget:.0%}。", parent=self)

    def selected_transaction(self):
        selection = self.day_tree.selection()
        return self.ledger.get_transaction(int(selection[0])) if selection else None

    def edit_selected(self):
        row = self.selected_transaction()
        if not row:
            return
        values = {"kind": row["kind"], "amount": row["amount"], "category": row["category"],
                  "note": row["note"], "date": date.fromisoformat(row["date"])}
        result = EntryDialog(self, self.ledger, "編輯記錄", values).show()
        if result:
            self.ledger.update_transaction(row["id"], **result)
            self.refresh_month()
            self.set_status("已更新記錄")

    def delete_selected(self):
        row = self.selected_transaction()
        if not row:
            return
        summary = f"{row['category']}　{row['note']}　{money(row['amount'])} 元"
        if messagebox.askyesno("刪除記錄", f"確定要刪除這筆記錄嗎？\n\n{summary}", parent=self):
            self.ledger.delete_transaction(row["id"])
            self.refresh_month()
            self.set_status("已刪除記錄")

    # ---- 統計圖表 ----

    def refresh_stats(self):
        kind = self.stats_kind.get()
        self.stats_title.config(text=f"{self.month} 月{KIND_LABEL[kind]}分類佔比")
        totals = self.ledger.category_totals(self.year, self.month, kind)
        total = sum(amount for _, amount in totals)
        order = self.ledger.categories(kind)
        slices = [(name, amount, PIE_COLORS[order.index(name) % len(PIE_COLORS)] if name in order else REST_COLOR)
                  for name, amount in totals]
        if len(slices) > MAX_SLICES:
            rest = sum(amount for _, amount, _ in slices[MAX_SLICES - 1:])
            slices = slices[:MAX_SLICES - 1] + [("其餘分類", rest, REST_COLOR)]
        self.draw_donut(slices, total, kind)

        for child in self.legend.winfo_children():
            child.destroy()
        for row, (name, amount, color) in enumerate(slices):
            tk.Frame(self.legend, bg=color, width=self.px(12), height=self.px(12)).grid(
                row=row, column=0, pady=5)
            ttk.Label(self.legend, text=name).grid(row=row, column=1, sticky="w", padx=(8, 16))
            ttk.Label(self.legend, text=money(amount)).grid(row=row, column=2, sticky="e")
            ttk.Label(self.legend, text=share_text(amount, total), style="Muted.TLabel", width=5,
                      anchor="e").grid(row=row, column=3, sticky="e")

        for child in self.budget_rows.winfo_children():
            child.destroy()
        usage = self.ledger.budget_usage(self.year, self.month)
        if not usage:
            ttk.Label(self.budget_rows, style="Muted.TLabel",
                      text="尚未設定預算。\n到「設定與備份」分頁，雙擊支出分類就能設定每月預算。").grid()
        for row, (name, spent, budget) in enumerate(usage):
            bar_color, text_color, state = budget_state(spent, budget)
            ttk.Label(self.budget_rows, text=name).grid(row=row, column=0, sticky="w", pady=7)
            bar = tk.Canvas(self.budget_rows, width=self.px(140), height=self.px(10), bg=SURFACE,
                            highlightthickness=0)
            bar.grid(row=row, column=1, padx=12)
            draw_bar(bar, spent / budget, bar_color)
            ttk.Label(self.budget_rows, text=f"{money(spent)} / {money(budget)}").grid(
                row=row, column=2, sticky="e")
            ttk.Label(self.budget_rows, text=state, foreground=text_color).grid(
                row=row, column=3, sticky="w", padx=(12, 0))

    def draw_donut(self, slices, total, kind):
        canvas = self.donut
        canvas.delete("all")
        size, pad = int(canvas["width"]), self.px(6)
        center, hole = size / 2, (size / 2 - pad) * 0.6
        if not slices:
            canvas.create_oval(pad, pad, size - pad, size - pad, fill=LINE, outline="")
            canvas.create_oval(center - hole, center - hole, center + hole, center + hole,
                               fill=SURFACE, outline="")
            canvas.create_text(center, center, text=f"沒有{KIND_LABEL[kind]}記錄", fill=MUTED)
            return
        default = (f"總{KIND_LABEL[kind]}", money(total))
        start = 90.0  # 從 12 點鐘方向開始，順時針由大到小
        for name, amount, color in slices:
            extent = -360 * amount / total
            if len(slices) == 1:  # 360 度的扇形畫不出來，直接畫圓
                item = canvas.create_oval(pad, pad, size - pad, size - pad, fill=color, outline=SURFACE, width=2)
            else:
                item = canvas.create_arc(pad, pad, size - pad, size - pad, start=start, extent=extent,
                                         fill=color, outline=SURFACE, width=2)
            caption = (name, f"{money(amount)}（{share_text(amount, total)}）")
            canvas.tag_bind(item, "<Enter>", lambda event, caption=caption: self.set_donut_caption(*caption))
            canvas.tag_bind(item, "<Leave>", lambda event: self.set_donut_caption(*default))
            start += extent
        canvas.create_oval(center - hole, center - hole, center + hole, center + hole,
                           fill=SURFACE, outline=SURFACE)
        self.donut_caption = (
            canvas.create_text(center, center - self.px(14), fill=INK_2),
            canvas.create_text(center, center + self.px(10), fill=INK, font=(FONT, 13, "bold")))
        self.set_donut_caption(*default)

    def set_donut_caption(self, title, value):
        """圓環中央的文字：平常顯示總額，滑鼠移到某一塊時顯示該分類。"""
        self.donut.itemconfigure(self.donut_caption[0], text=title)
        self.donut.itemconfigure(self.donut_caption[1], text=value)

    # ---- 固定收支 ----

    def refresh_recurring(self):
        self.rec_tree.delete(*self.rec_tree.get_children())
        for item in self.ledger.recurring_items():
            sign = "-" if item["kind"] == EXPENSE else "+"
            self.rec_tree.insert(
                "", "end", iid=str(item["id"]), tags=() if item["active"] else ("off",),
                values=(f"每月 {item['day']} 號", KIND_LABEL[item["kind"]], item["category"], item["note"],
                        sign + money(item["amount"]), "啟用中" if item["active"] else "已停用"))
        self.update_recurring_buttons()

    def selected_recurring(self):
        selection = self.rec_tree.selection()
        return self.ledger.get_recurring(int(selection[0])) if selection else None

    def update_recurring_buttons(self):
        item = self.selected_recurring()
        for button in (self.rec_edit, self.rec_toggle, self.rec_delete):
            button.config(state="normal" if item else "disabled")
        self.rec_toggle.config(text="啟用" if item and not item["active"] else "停用")

    def after_recurring_change(self):
        created = self.ledger.apply_recurring()
        self.refresh_all()
        if created:
            self.set_status(f"已自動補上 {created} 筆固定收支記錄")

    def add_recurring(self):
        values = {"kind": EXPENSE, "amount": "", "category": "", "note": "", "day": date.today().day}
        result = EntryDialog(self, self.ledger, "新增固定收支", values, recurring=True,
                             ask_first_month=True).show()
        if result:
            self.ledger.add_recurring(**result)
            self.after_recurring_change()

    def edit_recurring(self):
        item = self.selected_recurring()
        if not item:
            return
        values = {key: item[key] for key in ("kind", "amount", "category", "note", "day")}
        result = EntryDialog(self, self.ledger, "編輯固定收支", values, recurring=True).show()
        if result:
            self.ledger.update_recurring(item["id"], **result)
            self.after_recurring_change()
            self.set_status("已更新固定收支；之前已經產生的記錄不會跟著改變")

    def toggle_recurring(self):
        item = self.selected_recurring()
        if item:
            self.ledger.set_recurring_active(item["id"], not item["active"])
            self.after_recurring_change()

    def delete_recurring(self):
        item = self.selected_recurring()
        if not item:
            return
        summary = f"每月 {item['day']} 號　{item['category']}　{item['note']}　{money(item['amount'])} 元"
        if messagebox.askyesno("刪除固定收支", f"確定要刪除這個固定項目嗎？之前已經產生的記錄會保留。\n\n{summary}",
                               parent=self):
            self.ledger.delete_recurring(item["id"])
            self.refresh_recurring()

    # ---- 分類、預算 ----

    def refresh_settings(self):
        budgets = self.ledger.budgets()
        for kind, tree in self.category_trees.items():
            tree.delete(*tree.get_children())
            for name in self.ledger.categories(kind):
                budget = money(budgets[name]) if name in budgets else "未設定"
                tree.insert("", "end", iid=name, values=(name, budget) if kind == EXPENSE else (name,))

    def selected_category(self, kind):
        selection = self.category_trees[kind].selection()
        if not selection:
            messagebox.showinfo("請先選擇分類", "請先在清單中點選一個分類。", parent=self)
            return None
        return selection[0]

    def add_category(self, kind):
        def parse(text):
            name = text.strip()
            if not name:
                raise ValueError("請輸入分類名稱")
            if len(name) > 10:
                raise ValueError("分類名稱請在 10 個字以內")
            if name in self.ledger.categories(kind):
                raise ValueError(f"已經有「{name}」這個分類了")
            return name

        name = InputDialog(self, f"新增{KIND_LABEL[kind]}分類", "分類名稱：", parse).show()
        if name:
            self.ledger.add_category(kind, name)
            self.refresh_all()

    def delete_category(self, kind):
        name = self.selected_category(kind)
        if name and messagebox.askyesno(
                "刪除分類", f"確定要刪除{KIND_LABEL[kind]}分類「{name}」嗎？\n\n已經記下的記錄不會受影響，只是之後不能再選這個分類。",
                parent=self):
            self.ledger.delete_category(kind, name)
            self.refresh_all()

    def edit_budget(self):
        name = self.selected_category(EXPENSE)
        if not name:
            return
        current = self.ledger.budgets().get(name)
        amount = InputDialog(self, "設定每月預算", f"「{name}」的每月預算（元），輸入 0 表示不設預算：",
                             lambda text: parse_amount(text, allow_zero=True), str(current or "")).show()
        if amount is not None:
            self.ledger.set_budget(name, amount)
            self.refresh_all()
            self.category_trees[EXPENSE].selection_set(name)

    # ---- 匯出與備份 ----

    def export_csv(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="匯出 CSV", defaultextension=".csv", filetypes=[("CSV 檔", "*.csv")],
            initialfile=f"記帳記錄_{date.today():%Y%m%d}.csv")
        if not path:
            return
        try:
            count = self.ledger.export_csv(path)
        except OSError as error:
            messagebox.showerror("匯出失敗", f"無法寫入檔案（如果檔案正在 Excel 中開啟，請先關閉）。\n\n{error}",
                                 parent=self)
            return
        self.set_status(f"已匯出 {count} 筆記錄到 {path}")

    def backup(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="備份資料", defaultextension=".db", filetypes=[("記帳備份檔", "*.db")],
            initialfile=f"記帳備份_{datetime.now():%Y%m%d_%H%M}.db")
        if not path:
            return
        try:
            self.ledger.backup_to(path)
        except (OSError, ValueError) as error:
            messagebox.showerror("備份失敗", str(error), parent=self)
            return
        self.set_status(f"已備份到 {path}")

    def restore(self):
        path = filedialog.askopenfilename(
            parent=self, title="選擇要還原的備份檔", filetypes=[("記帳備份檔", "*.db"), ("所有檔案", "*.*")])
        if not path or not messagebox.askyesno(
                "從備份還原", "目前的所有記錄、分類、預算與固定收支，都會被備份檔的內容取代。\n\n"
                "還原前會先把目前的資料另存一份到程式資料夾。確定要還原嗎？", parent=self):
            return
        safety = self.ledger.path.with_name(f"還原前的資料_{datetime.now():%Y%m%d_%H%M%S}.db")
        try:
            self.ledger.restore_from(path, keep_current_as=safety)
        except (OSError, ValueError) as error:
            messagebox.showerror("還原失敗", str(error), parent=self)
            return
        self.after_recurring_change()
        self.set_status(f"已還原。還原前的資料另存為 {safety.name}")


if __name__ == "__main__":
    try:
        # 讓工作列顯示程式自己的圖示，而不是 Python 的
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ledger.app")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高解析度螢幕上文字才不會模糊
    except (AttributeError, OSError):
        pass
    App().mainloop()
