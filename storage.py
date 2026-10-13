"""記帳系統的資料層：所有資料存在同一個 SQLite 檔，每次變更都立即寫入。"""
import calendar
import csv
import sqlite3
from datetime import date
from pathlib import Path

EXPENSE, INCOME = "expense", "income"
KIND_LABEL = {EXPENSE: "支出", INCOME: "收入"}

DEFAULT_CATEGORIES = {
    EXPENSE: ["餐飲", "交通", "購物", "娛樂", "居住", "醫療", "其他"],
    INCOME: ["薪資", "獎金", "其他"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,              -- YYYY-MM-DD
    kind TEXT NOT NULL,              -- expense / income
    amount INTEGER NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recurring_id INTEGER             -- 由固定收支自動產生時，記下來源
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (kind, name)
);
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,       -- 支出分類
    amount INTEGER NOT NULL          -- 每月預算
);
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    day INTEGER NOT NULL,            -- 每月幾號
    active INTEGER NOT NULL DEFAULT 1,
    next_month TEXT NOT NULL         -- 下一個要補記的月份 YYYY-MM
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def month_key(year, month):
    return f"{year:04d}-{month:02d}"


def following_month(key):
    year, month = map(int, key.split("-"))
    return month_key(year + month // 12, month % 12 + 1)


def month_bounds(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return f"{month_key(year, month)}-01", f"{month_key(year, month)}-{last_day:02d}"


def csv_safe(text):
    """避免 Excel 把開頭是 = + - @ 的文字當成公式執行。"""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._prepare()

    def _prepare(self):
        self.conn.executescript(SCHEMA)
        if self.get_setting("initialized") is None:
            with self.conn:
                for kind, names in DEFAULT_CATEGORIES.items():
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO categories (kind, name) VALUES (?, ?)",
                        [(kind, name) for name in names],
                    )
            self.set_setting("initialized", "1")

    def close(self):
        self.conn.close()

    # ---- 記錄 ----

    def add_transaction(self, day, kind, amount, category, note="", recurring_id=None):
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO transactions (date, kind, amount, category, note, recurring_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), kind, amount, category, note, recurring_id),
            )
        return cursor.lastrowid

    def update_transaction(self, tx_id, day, kind, amount, category, note):
        with self.conn:
            self.conn.execute(
                "UPDATE transactions SET date = ?, kind = ?, amount = ?, category = ?, note = ? "
                "WHERE id = ?",
                (day.isoformat(), kind, amount, category, note, tx_id),
            )

    def delete_transaction(self, tx_id):
        with self.conn:
            self.conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))

    def get_transaction(self, tx_id):
        return self.conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()

    def transactions_on(self, day):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE date = ? ORDER BY id", (day.isoformat(),)
        ).fetchall()

    def daily_totals(self, year, month):
        """回傳 {日: {支出或收入: 金額}}，只包含有記錄的日子。"""
        totals = {}
        rows = self.conn.execute(
            "SELECT date, kind, SUM(amount) AS total FROM transactions "
            "WHERE date BETWEEN ? AND ? GROUP BY date, kind",
            month_bounds(year, month),
        )
        for row in rows:
            totals.setdefault(int(row["date"][8:]), {})[row["kind"]] = row["total"]
        return totals

    def month_totals(self, year, month):
        totals = {EXPENSE: 0, INCOME: 0}
        rows = self.conn.execute(
            "SELECT kind, SUM(amount) AS total FROM transactions "
            "WHERE date BETWEEN ? AND ? GROUP BY kind",
            month_bounds(year, month),
        )
        for row in rows:
            totals[row["kind"]] = row["total"]
        return totals

    def category_totals(self, year, month, kind):
        """回傳 [(分類, 金額)]，金額由大到小。"""
        rows = self.conn.execute(
            "SELECT category, SUM(amount) AS total FROM transactions "
            "WHERE kind = ? AND date BETWEEN ? AND ? GROUP BY category ORDER BY total DESC, category",
            (kind, *month_bounds(year, month)),
        )
        return [(row["category"], row["total"]) for row in rows]

    # ---- 分類與預算 ----

    def categories(self, kind):
        rows = self.conn.execute("SELECT name FROM categories WHERE kind = ? ORDER BY id", (kind,))
        return [row["name"] for row in rows]

    def add_category(self, kind, name):
        """新增分類；已經有同名分類時回傳 False。"""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO categories (kind, name) VALUES (?, ?)", (kind, name)
            )
        return cursor.rowcount == 1

    def delete_category(self, kind, name):
        """只把分類從選單移除，已經記下的記錄不受影響。"""
        with self.conn:
            self.conn.execute("DELETE FROM categories WHERE kind = ? AND name = ?", (kind, name))
            if kind == EXPENSE:
                self.conn.execute("DELETE FROM budgets WHERE category = ?", (name,))

    def budgets(self):
        return {row["category"]: row["amount"] for row in self.conn.execute("SELECT * FROM budgets")}

    def set_budget(self, category, amount):
        """設定分類的每月預算；金額為 0 表示取消預算。"""
        with self.conn:
            if amount:
                self.conn.execute(
                    "INSERT INTO budgets (category, amount) VALUES (?, ?) "
                    "ON CONFLICT (category) DO UPDATE SET amount = excluded.amount",
                    (category, amount),
                )
            else:
                self.conn.execute("DELETE FROM budgets WHERE category = ?", (category,))

    def budget_usage(self, year, month):
        """回傳 [(分類, 已花費, 預算)]，只包含有設定預算的分類。"""
        budgets = self.budgets()
        spent = dict(self.category_totals(year, month, EXPENSE))
        return [
            (name, spent.get(name, 0), budgets[name])
            for name in self.categories(EXPENSE)
            if name in budgets
        ]

    # ---- 記憶：設定值與常用輸入 ----

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def note_suggestions(self, kind, text="", limit=6):
        """以前輸入過且包含 text 的備註，最常用的排前面。

        每一列附上該備註最近一次使用的分類與金額（SQLite 在只有一個 MAX() 時，
        其他欄位會取自最大值所在的那一列）。自動產生的固定收支不列入。
        """
        return self.conn.execute(
            "SELECT note, category, amount, COUNT(*) AS uses, MAX(id) AS latest "
            "FROM transactions "
            "WHERE kind = ? AND note <> '' AND recurring_id IS NULL "
            "AND (? = '' OR instr(lower(note), lower(?)) > 0) "
            "GROUP BY note ORDER BY uses DESC, latest DESC LIMIT ?",
            (kind, text, text, limit),
        ).fetchall()

    # ---- 固定收支 ----

    def recurring_items(self):
        return self.conn.execute("SELECT * FROM recurring ORDER BY day, id").fetchall()

    def get_recurring(self, rec_id):
        return self.conn.execute("SELECT * FROM recurring WHERE id = ?", (rec_id,)).fetchone()

    def add_recurring(self, kind, amount, category, note, day, first_month):
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO recurring (kind, amount, category, note, day, next_month) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, amount, category, note, day, first_month),
            )
        return cursor.lastrowid

    def update_recurring(self, rec_id, kind, amount, category, note, day):
        with self.conn:
            self.conn.execute(
                "UPDATE recurring SET kind = ?, amount = ?, category = ?, note = ?, day = ? "
                "WHERE id = ?",
                (kind, amount, category, note, day, rec_id),
            )

    def set_recurring_active(self, rec_id, active, today=None):
        """停用或重新啟用；重新啟用時不回頭補記停用期間的月份。"""
        today = today or date.today()
        with self.conn:
            self.conn.execute("UPDATE recurring SET active = ? WHERE id = ?", (int(active), rec_id))
            if active:
                self.conn.execute(
                    "UPDATE recurring SET next_month = max(next_month, ?) WHERE id = ?",
                    (month_key(today.year, today.month), rec_id),
                )

    def delete_recurring(self, rec_id):
        with self.conn:
            self.conn.execute("DELETE FROM recurring WHERE id = ?", (rec_id,))

    def apply_recurring(self, today=None):
        """把已到期的固定收支補記成記錄，回傳新增的筆數。

        每個項目每個月只會產生一次；沒有那一天的月份（例如 2 月 31 號）記在月底。
        """
        today = today or date.today()
        this_month = month_key(today.year, today.month)
        created = 0
        with self.conn:
            for item in self.conn.execute("SELECT * FROM recurring WHERE active = 1").fetchall():
                month = item["next_month"]
                while month <= this_month:
                    year, month_number = map(int, month.split("-"))
                    last_day = calendar.monthrange(year, month_number)[1]
                    due = date(year, month_number, min(item["day"], last_day))
                    if due > today:
                        break
                    self.conn.execute(
                        "INSERT INTO transactions (date, kind, amount, category, note, recurring_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (due.isoformat(), item["kind"], item["amount"], item["category"],
                         item["note"], item["id"]),
                    )
                    created += 1
                    month = following_month(month)
                if month != item["next_month"]:
                    self.conn.execute(
                        "UPDATE recurring SET next_month = ? WHERE id = ?", (month, item["id"])
                    )
        return created

    # ---- 匯出與備份 ----

    def export_csv(self, path):
        """把所有記錄匯出成 Excel 可直接開啟的 CSV，回傳筆數。"""
        rows = self.conn.execute("SELECT * FROM transactions ORDER BY date, id").fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["日期", "類型", "分類", "金額", "備註"])
            for row in rows:
                writer.writerow([
                    row["date"], KIND_LABEL[row["kind"]], csv_safe(row["category"]),
                    row["amount"], csv_safe(row["note"]),
                ])
        return len(rows)

    def backup_to(self, path):
        path = Path(path)
        if path.resolve() == self.path.resolve():
            raise ValueError("備份檔不能跟目前使用中的資料檔是同一個檔案")
        path.unlink(missing_ok=True)
        target = sqlite3.connect(path)
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def restore_from(self, path, keep_current_as=None):
        """用備份檔的內容取代目前所有資料；確認備份檔可用後，先把目前的資料另存到 keep_current_as。"""
        path = Path(path)
        if path.resolve() == self.path.resolve():
            raise ValueError("這是目前使用中的資料檔，不需要還原")
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            try:
                tables = {row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
            except sqlite3.DatabaseError:
                tables = set()
            if not {"transactions", "categories"} <= tables:
                raise ValueError("這個檔案不是記帳系統的備份檔")
            if keep_current_as:
                self.backup_to(keep_current_as)
            source.backup(self.conn)
        finally:
            source.close()
        self._prepare()
