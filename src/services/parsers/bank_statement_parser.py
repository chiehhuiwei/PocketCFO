"""
src/services/parsers/bank_statement_parser.py
Uses Gemini to extract structured data from bank statement PDFs.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from src.instances.gemini import extract_structured
from src.utils.date_utils import parse_period_date

BANK_STATEMENT_PROMPT = """
You are an expert financial data extraction assistant.
Analyze this Taiwan bank statement PDF (which may be a Consolidated Statement/綜合對帳單 containing bank savings, credit cards, funds, and securities) and extract the bank savings accounts and their transaction details in JSON format.

Return ONLY valid JSON with this exact schema:
{
  "institution": "string (bank name in Traditional Chinese, e.g. 國泰世華銀行, 玉山銀行, 台新銀行)",
  "period_year": integer,
  "period_month": integer,
  "accounts": [
    {
      "account_number": "string (MUST search the entire document to find and extract the FULL, UNMASKED account number for this specific savings account. Do not mask/truncate digits if the complete account number is visible anywhere in the file.)",
      "account_type_label": "string (the account type label as shown in the statement, e.g. 新臺幣活存_母帳戶, 新臺幣活存, 外幣活期存款)",
      "closing_balance": float,
      "currency": "string (e.g., TWD, USD, EUR, JPY, etc. Default to TWD if not specified)",
      "transactions": [
        {
          "date": "YYYY-MM-DD",
          "description": "string",
          "debit": float or null,
          "credit": float or null,
          "balance": float or null,
          "category": "string (薪資/投資/轉入/轉出/支出/食物/交通/醫療/娛樂/股利/利息/其他)"
        }
      ]
    }
  ]
}

Important rules:
- **Taiwan Consolidated Statements (綜合對帳單)**: Focus ONLY on bank savings sections (e.g., 台幣活期存款, 台幣活存, 活期性存款, 外幣存款, 活期存款, 外幣活期存款). Do NOT extract credit card consumption items, securities/stock inventory, or fund valuations as bank accounts or savings transactions.
- **台新銀行 (Taishin Bank) SPECIAL RULES — VERY IMPORTANT**:
  - The 台新/Richart 對帳單 lists many account rows in a summary table. You MUST only extract these two types:
    1. `新臺幣活存_母帳戶` (the Richart main account — 備註 column shows "Richart")
    2. `新臺幣活存` (the regular savings account — 備註 column is empty or does NOT say "Richart")
  - You MUST **skip** all rows where 帳戶類別 is `新臺幣活存_子帳戶`. These are Richart sub-accounts and must NOT be included.
  - If the PDF lists both a 母帳戶 and a 新臺幣活存, include BOTH as separate entries in the `accounts` array.
- **連線商業銀行 (LINE Bank) SPECIAL RULES** — 連線商業銀行 is LINE Bank's official registered name; always use `institution: "連線商業銀行"` for it, never "將來銀行" (a different, unrelated digital bank): LINE Bank statements list both regular bank-account transfers AND debit card (金融卡) purchases together under the same savings account, with a separate 備註/note column (e.g. showing the merchant name or a card-transaction marker) alongside the generic transaction summary. For every transaction row, you MUST combine BOTH the transaction summary AND the 備註/note into `description` (e.g. `"<原始摘要> - <備註內容>"` when a note exists) — do NOT drop the 備註 or only keep the generic summary line.
  - LINE Bank descriptions commonly include the literal patterns `連加＊` and `連支＊` (e.g. `連加＊超商代碼儲值`, `連支＊LINE Pay消費`). These are legitimate transaction-type labels, not OCR garbage — transcribe them exactly as printed (including the `＊`/`*` character) rather than guessing, correcting, or dropping them.
- **Foreign Currency Accounts**: If the statement contains foreign currency savings accounts (外幣存款, 外幣活期存款, e.g. USD, EUR, JPY), extract them as separate entries in `accounts` with the correct `currency` code. The `closing_balance` must be in the ORIGINAL foreign currency amount (do NOT convert to TWD yourself — the backend will do this). Populate `currency` with the 3-letter ISO currency code (e.g. "USD", "EUR", "JPY").
- **Category Classification**: Analyze the description and merchant details for each transaction, and classify it into one of these exact categories: (薪資/投資/轉入/轉出/支出/食物/交通/醫療/娛樂/股利/利息/其他). Note that cash withdrawals / ATM withdrawals (e.g. 現金提款, ATM提款, 提款, 提現, ATM) MUST be classified as "轉出" or "帳內互轉" (which map to internal transfers), and MUST NOT be classified as "支出" or "其他".
- **Account Number**: Look carefully for the full savings account number (存款帳號) associated with each savings section. If the account number contains asterisks/X and the full unmasked number is not found anywhere else, retain the asterisks/X. Do NOT guess digits.
- ALL dates MUST be formatted as YYYY-MM-DD in the Gregorian calendar (西元年). If the statement uses the Taiwan ROC calendar (民國), you MUST convert it by adding 1911 to the year. Examples: `113年10月31日 → 2024-10-31`, `113/10/31 → 2024-10-31`.
- `closing_balance` must be the final balance of this specific savings account at the end of the period, in the account's own currency.
- **Sign convention — VERY IMPORTANT**: `credit` (轉入/money coming IN to the account) MUST be a positive number; `debit` (轉出/money going OUT of the account) MUST be a positive number placed in the `debit` field (the backend treats `debit` as an outflow and negates it — do NOT put a negative number in `debit`). Never swap which field a transaction goes into: an incoming transfer (轉入/存入/匯入) is always `credit`, an outgoing transfer or purchase (轉出/提出/扣款) is always `debit`. Double-check each row against the running `balance` column (balance should increase for credit rows and decrease for debit rows) before finalizing.
- For description, combine transaction summary with any counterparty account or notes.
- If you cannot find a field, use null.
"""


async def parse_bank_statement(pdf_path: Path) -> dict[str, Any]:
    """Parse a bank statement PDF and return structured data."""
    data = await extract_structured(pdf_path, BANK_STATEMENT_PROMPT)
    return data


CREDIT_CARD_PROMPT = """
You are a financial data extraction assistant.
Analyze this Taiwan credit card statement PDF and extract data in JSON format.

Return ONLY valid JSON with this exact schema:
{
  "institution": "string (bank/card issuer name)",
  "card_last_four": "string",
  "period_year": integer,
  "period_month": integer,
  "total_amount": float or null,
  "payment_due_date": "YYYY-MM-DD or null",
  "currency": "TWD",
  "items": [
    {
      "date": "YYYY-MM-DD",
      "merchant": "string",
      "category": "string (餐飲/支出/交通/娛樂/醫療/其他)",
      "amount": float,
      "currency": "TWD",
      "is_refund": boolean
    }
  ]
}

Important:
- ALL dates MUST be formatted as YYYY-MM-DD in the Gregorian calendar (西元年). If the statement uses the Taiwan ROC calendar (民國), convert it by adding 1911 to the year.
- total_amount is the total billed amount for this cycle (本期應繳總額) as printed on the statement. If the document does not show it (e.g. screenshots of the transaction list only), return null — do NOT add up the items or guess.
- period_year and period_month MUST represent the month the transactions actually took place in (the statement cycle month), NOT the statement issue date or billing date. For example, a bill due in June for May's consumption should have period_month = 5.
- Refunds / 退款 should have is_refund: true and positive amount
- Categorize merchants as best as you can
"""


async def parse_credit_card_statement(pdf_path: Path) -> dict[str, Any]:
    """Parse a credit card statement PDF and return structured data."""
    return await extract_structured(pdf_path, CREDIT_CARD_PROMPT)


BROKERAGE_STATEMENT_PROMPT = """
You are a financial data extraction assistant.
Analyze this Taiwan brokerage account statement PDF and extract data in JSON format.

Return ONLY valid JSON with this exact schema:
{
  "institution": "string (broker name e.g. 永豐金/台新證券)",
  "account_number": "string",
  "period_year": integer,
  "period_month": integer,
  "cash_balance": float or null,
  "total_market_value": float or null,
  "holdings": [
    {
      "ticker": "string or null (e.g. 0050, 2330)",
      "name": "string",
      "quantity": float or null,
      "avg_cost": float or null,
      "current_price": float or null,
      "market_value": float or null,
      "unrealized_pnl": float or null
    }
  ],
  "transactions": [
    {
      "date": "YYYY-MM-DD",
      "action": "string (e.g., 買進 / 賣出)",
      "ticker": "string or null",
      "name": "string (stock name)",
      "quantity": float or null,
      "price": float or null,
      "amount": float or null,
      "fee": float or null
    }
  ]
}

Important rules:
- ALL dates MUST be formatted as YYYY-MM-DD.
- `holdings` represents the user's stock inventory (庫存餘額 / 集保餘額 / 證券庫存). You MUST extract every individual stock listed in the inventory section.
- If a stock doesn't have a ticker, use its name.
- `unrealized_pnl` is the unrealized profit/loss (預估損益 / 未實現損益).
- If `quantity` or `market_value` is present, you MUST include the holding even if other fields are missing.
- `transactions` represents the trading activity (交易明細 / 買賣紀錄) during the statement period.
- `amount` in transactions is the total settlement amount (應付/應收金額 或 價金). Use positive values.
- `fee` in transactions represents trading fees/taxes (手續費 + 交易稅).
- Ensure that the sum of `market_value` for all holdings roughly matches `total_market_value`.
"""


async def parse_brokerage_statement(pdf_path: Path) -> dict[str, Any]:
    """Parse a brokerage statement PDF and return structured data."""
    return await extract_structured(pdf_path, BROKERAGE_STATEMENT_PROMPT)


EINVOICE_PROMPT = """
You are a financial data extraction assistant.
Analyze this Taiwan Electronic Invoice (電子發票) statement / purchase details PDF or text and extract the data in JSON format.

Return ONLY valid JSON with this exact schema:
{
  "period_year": integer,
  "period_month": integer,
  "items": [
    {
      "date": "YYYY-MM-DD",
      "merchant": "string (store/merchant name)",
      "description": "string (brief summary of purchased items/services)",
      "amount": float,
      "payment_method": "string (e.g. 現金 / 信用卡 / 街口支付 / LINE Pay / 悠遊卡 / 其他)",
      "invoice_number": "string or null (e.g., AB-12345678)"
    }
  ]
}

Important rules:
- ALL dates MUST be formatted as YYYY-MM-DD in the Gregorian calendar (西元年). If the statement uses the Taiwan ROC calendar (民國), convert it by adding 1911 to the year.
- If payment method is not explicitly shown, use "其他" or guess if context allows (e.g. Apple Pay -> Apple Pay / 信用卡).
- Categorize the merchant and clean up common suffix words if possible.
"""


async def parse_einvoice_statement(pdf_path: Path) -> dict[str, Any]:
    """Parse an electronic invoice PDF and return structured data."""
    return await extract_structured(pdf_path, EINVOICE_PROMPT)


_EINVOICE_COLUMNS = [
    "載具自訂名稱", "發票日期", "發票號碼", "發票金額", "發票狀態", "折讓", "賣方統一編號", "賣方名稱",
    "賣方地址", "買方統編", "消費明細_數量", "消費明細_單價", "消費明細_金額", "消費明細_品名",
]
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_BUYER_ID_RE = re.compile(r"^(\d{8})?$")


def _split_einvoice_row(parts: list[str]) -> list[str] | None:
    """
    Map one carrier-CSV row onto _EINVOICE_COLUMNS. The export doesn't quote fields, so commas inside
    the seller address or item name shift later columns; anchor on the quantity/unit price/amount
    numbers (preceded by an empty or 8-digit buyer tax ID) instead of fixed positions.
    """
    fields = [p.strip() for p in parts]
    if len(fields) < 14 or not fields[2]:  # blank padding rows have no invoice number
        return None
    for i in range(9, len(fields) - 2):
        if _BUYER_ID_RE.match(fields[i - 1]) and all(_NUMBER_RE.match(fields[j]) for j in (i, i + 1, i + 2)):
            # Re-join split fields from the raw parts so spacing after embedded commas is kept
            address = ",".join(parts[8:i - 1]).strip()
            product_name = ",".join(parts[i + 3:]).rstrip(", ").strip()
            return fields[:8] + [address, fields[i - 1], fields[i], fields[i + 1], fields[i + 2], product_name]
    return None


async def parse_einvoice_csv(csv_path: Path) -> dict[str, Any]:
    """Parse an electronic invoice carrier CSV file and return aggregated structured data."""
    import pandas as pd
    from datetime import datetime

    rows = []
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        next(reader, None)  # header; column count varies between exports (trailing empty columns)
        for parts in reader:
            if any("注意" in p or "捐贈" in p or "字軌" in p for p in parts):
                continue
            row = _split_einvoice_row(parts)
            if row:
                rows.append(row)

    df = pd.DataFrame(rows, columns=_EINVOICE_COLUMNS)
    df['發票日期'] = df['發票日期'].astype(str)
    df['發票金額'] = pd.to_numeric(df['發票金額'], errors='coerce').fillna(0)
    
    period_year = None
    period_month = None
    
    # Group by invoice number to merge items
    grouped = df.groupby('發票號碼').agg({
        '發票日期': 'first',
        '賣方名稱': 'first',
        '載具自訂名稱': 'first',
        '發票金額': 'sum',  # each row's 發票金額 is that line's amount (discounts are negative lines)
        '消費明細_品名': lambda x: ', '.join(str(i).strip() for i in x if pd.notna(i))
    }).reset_index()
    
    items = []
    for _, row in grouped.iterrows():
        date_str = str(row['發票日期'])
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            formatted_date = dt.strftime("%Y-%m-%d")
            if period_year is None:
                period_year = dt.year
                period_month = dt.month
        except Exception:
            formatted_date = date_str
            
        merchant = str(row['賣方名稱']).strip()
        item_names = str(row['消費明細_品名']).strip()
        description = f"{merchant} - {item_names}" if item_names else merchant
        
        items.append({
            "date": formatted_date,
            "merchant": merchant,
            "description": description,
            "amount": float(row['發票金額']),
            "payment_method": str(row['載具自訂名稱']).strip() if pd.notna(row['載具自訂名稱']) and str(row['載具自訂名稱']).strip() else "電子載具",
            "invoice_number": str(row['發票號碼']).strip()
        })
        
    if period_year is None:
        period_year = datetime.now().year
        period_month = datetime.now().month
        
    return {
        "period_year": int(period_year),
        "period_month": int(period_month),
        "items": items
    }


