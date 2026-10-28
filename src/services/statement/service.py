"""
src/services/statement_service.py
Orchestrates PDF upload → Gemini parsing → DB persistence.
Handles bank statements, credit card bills, and brokerage statements.
"""
from __future__ import annotations

import json
import calendar
from datetime import date, timedelta
from pathlib import Path
from typing import Any
import re
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from src.dbs.models import (
    Account,
    AccountSnapshot,
    AccountType,
    Security,
    Transaction,
    TransactionCategory,
    TransactionSource,
)
from src.dbs.repository import (
    AccountRepository,
    CategoryRuleRepository,
    SecurityRepository,
    SnapshotRepository,
    TransactionRepository,
)
from src.instances.config import get_settings
from src.services.parsers.bank_statement_parser import (
    parse_bank_statement,
    parse_brokerage_statement,
    parse_credit_card_statement,
    parse_einvoice_statement,
    parse_einvoice_csv,
)
from src.services.parsers.firstrade_statement_parser import parse_firstrade_statement
from src.services.exchange_rate.service import get_usd_twd_rate, get_currency_twd_rate
from src.utils.date_utils import first_of_month, parse_tw_date_robust
from src.utils.transfer_detector import TransferDetector

log = logging.getLogger(__name__)
settings = get_settings()

# Short bank-name mapping used for display names
_BANK_SHORT: dict[str, str] = {
    "台新": "台新", "taishin": "台新", "richart": "台新",
    "玉山": "玉山", "esun": "玉山",
    "永豐": "永豐", "sinopac": "永豐",
    "國泰世華": "國泰", "國泰": "國泰", "cathay": "國泰",
    "中信": "中信", "中國信託": "中信", "ctbc": "中信",
    "第一": "第一", "first bank": "第一",
    "富邦": "富邦", "fubon": "富邦",
    "華南": "華南", "huanan": "華南",
    "星展": "星展", "dbs": "星展",
    "聯邦": "聯邦", "union": "聯邦",
    "將來": "將來",
    "連線": "LINE Bank", "line bank": "LINE Bank",
    "firstrade": "Firstrade",
}


def _smart_account_display_name(
    institution: str,
    account_type_label: str | None,
    currency: str,
) -> str:
    """
    Generate a human-readable account display name.
    Format: "[短銀行名] [帳戶類型] [幣別(if non-TWD)]"
    Examples:
      台新 Richart 台幣 / 台新 台幣活存 / 台新 外幣 USD / 玉山 台幣活存
    """
    inst_lower = institution.lower()
    short = institution  # fallback to full name
    for key, val in _BANK_SHORT.items():
        if key.lower() in inst_lower or key in institution:
            short = val
            break

    label = account_type_label or ""
    is_foreign = currency and currency != "TWD"

    if "母帳戶" in label:
        type_part = "Richart 台幣"
    elif is_foreign or "外幣" in label:
        ccy_str = currency if is_foreign else ""
        type_part = f"外幣 {ccy_str}".strip()
    elif "活存" in label or "活期" in label or "新臺幣" in label or "台幣" in label:
        type_part = "台幣活存"
    else:
        # fallback: use currency hint
        type_part = "台幣活存" if not is_foreign else f"外幣 {currency}"

    # Firstrade is always identified by name alone
    if short == "Firstrade":
        return f"Firstrade ({currency})" if is_foreign else "Firstrade"

    return f"{short} {type_part}"



# E-invoice merchants that are routinely paid via a known account: match by amount within ±days
EINVOICE_PAYMENT_RULES = [
    # 7-11 / 全家 / 萊爾富 / 三商家購(美聯社) → LINE Bank debit card (posting date can lag the purchase by days)
    {"merchants": ["統一超商", "7-11", "7-eleven", "全家", "fami", "萊爾富", "hi-life", "hilife", "三商家購", "美聯社"], "source": TransactionSource.BANK, "institutions": ["連線", "LINE Bank"], "days": 7},
    # 全聯 → 國泰世華 credit card
    {"merchants": ["全聯", "pxpay"], "source": TransactionSource.CREDIT_CARD, "institutions": ["國泰", "cathay"], "days": 7},
]


def match_payment_rule(merchant: str) -> dict | None:
    import unicodedata
    m = unicodedata.normalize('NFKC', merchant or "").lower().replace(" ", "")
    for rule in EINVOICE_PAYMENT_RULES:
        if any(k in m for k in rule["merchants"]):
            return rule
    return None


def is_merchant_overlap(m1: str, m2: str) -> bool:
    if not m1 or not m2:
        return False
    import unicodedata
    m1_norm = unicodedata.normalize('NFKC', m1)
    m2_norm = unicodedata.normalize('NFKC', m2)
    # Clean names
    m1_clean = m1_norm.lower().replace(" ", "").replace("股份有限公司", "").replace("分公司", "").replace("門市", "").replace("營業所", "")
    m2_clean = m2_norm.lower().replace(" ", "").replace("股份有限公司", "").replace("分公司", "").replace("門市", "").replace("營業所", "")
    
    aliases = {
        "全聯": ["pxpay", "px pay", "全聯"],
        "統一超商": ["7-11", "7-eleven", "統一超商", "ibon"],
        "全家": ["fami", "全家"],
        "中油": ["中油", "cpc"],
        "寶雅": ["poya", "寶雅"],
        "家樂福": ["carrefour", "家樂福", "統康"],
        "宜得利": ["nitori", "宜得利"],
        "蝦皮": ["shopee", "蝦皮"],
        "日月亭": ["日月亭"],
    }
    
    for key, vals in aliases.items():
        m1_has = any(val in m1_clean for val in vals) or (key in m1_clean)
        m2_has = any(val in m2_clean for val in vals) or (key in m2_clean)
        if m1_has and m2_has:
            return True
            
    # Substring check
    import re
    words1 = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+', m1_clean)
    words2 = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+', m2_clean)
    w1_str = "".join(words1)
    w2_str = "".join(words2)
    
    if len(w1_str) >= 2 and len(w2_str) >= 2:
        if w1_str in w2_str or w2_str in w1_str:
            return True
            
    return False


class StatementService:
    def __init__(self, db: AsyncSession, user_id: int) -> None:
        self.db = db
        self.user_id = user_id
        self.account_repo = AccountRepository(db, user_id)
        self.snapshot_repo = SnapshotRepository(db, user_id)
        self.security_repo = SecurityRepository(db, user_id)
        self.txn_repo = TransactionRepository(db, user_id)
        self.transfer_detector = None

    # ── Bank statement ─────────────────────────────────────────────────────

    async def ingest_bank_statement(
        self, pdf_path: Path, account_code: str | None = None, upload_history_id: int | None = None
    ) -> dict[str, Any]:
        """
        Parse a bank statement PDF and save.
        """
        log.info(f"ingest.bank_statement.start path={pdf_path}")
        data = await parse_bank_statement(pdf_path)
        if account_code:
            data["account_code"] = account_code
        return await self.save_bank_statement(data, upload_history_id)

    async def save_bank_statement(
        self, data: dict[str, Any], upload_history_id: int | None = None
    ) -> dict[str, Any]:
        import re
        import calendar
        period = first_of_month(data["period_year"], data["period_month"])

        # Resolve accounts. If not present in nested form, construct using top-level flat fields for backward compatibility.
        accounts_data = data.get("accounts", [])
        if not accounts_data:
            accounts_data = [{
                "account_number": data.get("account_number"),
                "currency": data.get("currency", "TWD"),
                "exchange_rate": data.get("exchange_rate", 1.0),
                "closing_balance": data.get("closing_balance"),
                "transactions": data.get("transactions", [])
            }]

        # Defensive filter: skip any Taishin 子帳戶 rows that Gemini may have accidentally included
        _SKIP_LABELS = {"新臺幣活存_子帳戶"}
        accounts_data = [
            a for a in accounts_data
            if a.get("account_type_label", "") not in _SKIP_LABELS
        ]

        # Pre-compute end-of-month date for exchange rate lookups
        last_day = calendar.monthrange(data["period_year"], data["period_month"])[1]
        from datetime import date as _date
        eom_date = _date(data["period_year"], data["period_month"], last_day)

        # Dynamically build internal accounts transfer detector from database once
        db_accounts = await self.account_repo.get_all()
        internal_aids = []
        for a in db_accounts:
            if a.is_internal:
                internal_aids.append(a.code)
                if "_" in a.code:
                    internal_aids.append(a.code.split("_")[-1])
                if a.notes:
                    internal_aids.append(a.notes)
        self.transfer_detector = TransferDetector(list(set(internal_aids)))

        results = []

        for acc_data in accounts_data:
            acc_num_raw = acc_data.get("account_number")
            # For matching: keep asterisks/X as wildcards (strip only hyphens/spaces)
            acc_num_match = re.sub(r'[^0-9*xX]', '', str(acc_num_raw)) if acc_num_raw else None
            # For storage as code: digits only (preserve leading zeros)
            acc_num_digits = re.sub(r'[^0-9]', '', str(acc_num_raw)) if acc_num_raw else None
            currency = acc_data.get("currency") or "TWD"

            # Resolve exchange rate:
            # 1. Use value from payload if already provided (e.g. from confirm step)
            # 2. For non-TWD accounts, fetch the end-of-month rate automatically
            # 3. Default to 1.0 for TWD
            exchange_rate = float(acc_data.get("exchange_rate") or data.get("exchange_rate") or 0)
            if exchange_rate <= 1.0 and currency != "TWD":
                try:
                    exchange_rate = await get_currency_twd_rate(eom_date, from_currency=currency)
                    log.info(f"Fetched {currency}/TWD rate for {eom_date}: {exchange_rate}")
                except Exception as _e:
                    log.warning(f"Could not fetch {currency}/TWD rate: {_e}, using 1.0")
                    exchange_rate = 1.0
            elif currency == "TWD":
                exchange_rate = 1.0

            # Generate unique code for this specific account
            account_code = data.get("account_code") if len(accounts_data) == 1 else None
            display_name = None
            account_type_label = acc_data.get("account_type_label")
            inst = data.get("institution", "unknown").strip()

            if not account_code:
                # Attempt to fuzzy match against existing accounts using the masked pattern,
                # requiring the currency to match so distinct currency sub-ledgers of one
                # physical account don't collide onto the same Account row.
                matching_acc = self._find_matching_db_account(inst, acc_num_match, AccountType.BANK, db_accounts, currency=currency)
                if matching_acc:
                    account_code = matching_acc.code
                    # Keep existing custom name; only overwrite if it looks like a bare institution name
                    existing_name = matching_acc.name or ""
                    if existing_name == inst or existing_name == inst + "銀行" or not existing_name:
                        display_name = _smart_account_display_name(inst, account_type_label, currency)
                    else:
                        display_name = existing_name
                else:
                    # New account — code is digits-only account number. If that code is
                    # already taken by another currency's Account (e.g. this physical
                    # account also holds a TWD or USD sub-ledger), suffix the currency so
                    # each sub-ledger gets its own row instead of colliding. Check the live
                    # DB (not just the in-memory db_accounts snapshot), since an earlier
                    # sub-ledger from this same statement may have just been created below
                    # and db_accounts isn't refreshed mid-loop.
                    if acc_num_digits:
                        code_taken = any(a.code == acc_num_digits for a in db_accounts) or bool(
                            await self.account_repo.get_by_code(acc_num_digits)
                        )
                        account_code = f"{acc_num_digits}_{currency}" if code_taken else acc_num_digits
                    else:
                        account_code = f"bank_{inst}_{currency}"

            if not display_name:
                display_name = _smart_account_display_name(inst, account_type_label, currency)

            account = await self._resolve_or_create_account(
                code=account_code,
                name=display_name,
                account_type=AccountType.BANK,
                institution=data.get("institution", ""),
                currency=currency,
            )
            # Keep the in-memory snapshot in sync so later iterations of this same
            # statement (e.g. another currency sub-ledger) see accounts just resolved.
            if account not in db_accounts:
                db_accounts.append(account)

            # Fallback to last transaction's balance if closing_balance is missing
            closing_balance_orig = float(acc_data.get("closing_balance") or 0)
            raw_txns = acc_data.get("transactions", [])
            if closing_balance_orig == 0 and raw_txns:
                last_txn_balance = raw_txns[-1].get("balance")
                if last_txn_balance is not None:
                    closing_balance_orig = float(last_txn_balance)

            closing_balance_twd = round(closing_balance_orig * exchange_rate)

            # Save snapshot
            snapshot = AccountSnapshot(
                account_id=account.id,
                period_date=period,
                balance=closing_balance_twd,
                original_balance=closing_balance_orig if currency != "TWD" else None,
                currency=currency,
                exchange_rate=exchange_rate,
                source="pdf",
                raw_data=json.dumps(data, ensure_ascii=False),
                upload_history_id=upload_history_id,
            )
            await self.snapshot_repo.upsert(snapshot)

            # Save transactions with transfer detection
            is_credit_card = account.account_type == AccountType.CREDIT_CARD
            txns = []
            for raw in raw_txns:
                if "amount" in raw:
                    amount_orig = float(raw["amount"])
                else:
                    amount_orig = float(raw.get("credit") or 0) - float(raw.get("debit") or 0)

                if is_credit_card:
                    is_ref = bool(raw.get("is_refund", False))
                    if not is_ref:
                        amount_orig = -abs(amount_orig) if amount_orig > 0 else amount_orig

                amount_twd = round(amount_orig * exchange_rate)
                is_transfer = self.transfer_detector.is_internal_transfer(raw.get("description", ""))
                is_taishin = "台新" in (account.institution or "")
                is_salary = is_taishin and amount_twd >= 60000
                
                cat_str = raw.get("category")
                if cat_str:
                    reverse_cat = {
                        "薪資": "salary",
                        "投資": "investment",
                        "轉入": "transfer_in",
                        "轉出": "transfer_out",
                        "固定支出": "expense",
                        "食物": "food",
                        "交通": "transport",
                        "醫療": "medical",
                        "娛樂": "entertainment",
                        "保險": "insurance",
                        "運動": "exercise",
                        "購物": "shopping",
                        "旅遊": "travel",
                        "學習": "study",
                        "信用卡繳款": "credit_card_payment",
                        "本金償還": "debt_repayment",
                        "股利": "dividend",
                        "利息": "interest",
                        "非固定支出": "other",
                        "非固定收入": "other",
                        "其他支出": "other",
                        "其他收入": "other",
                        "其他": "other",
                        "帳內互轉": "transfer_in"
                    }
                    cat_val = reverse_cat.get(cat_str, cat_str)
                    if isinstance(cat_val, str):
                        cat_val = cat_val.upper()
                    try:
                        category = TransactionCategory(cat_val)
                    except ValueError:
                        category = TransactionCategory.OTHER
                else:
                    category = (
                        TransactionCategory.TRANSFER_IN
                        if is_transfer and amount_twd > 0
                        else TransactionCategory.TRANSFER_OUT
                        if is_transfer and amount_twd < 0
                        else TransactionCategory.SALARY
                        if amount_twd > 0 and is_salary
                        else TransactionCategory.OTHER
                    )
                
                txn_date = raw.get("date")
                if isinstance(txn_date, str):
                    from datetime import datetime
                    try:
                        actual_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                    except ValueError:
                        actual_date = parse_tw_date_robust(txn_date) or period
                else:
                    actual_date = parse_tw_date_robust(raw.get("date")) or period

                txns.append(
                    Transaction(
                        account_id=account.id,
                        txn_date=actual_date,
                        source=TransactionSource.BANK,
                        category=category,
                        description=raw.get("description", ""),
                        amount=amount_twd,
                        original_amount=amount_orig if currency != "TWD" else None,
                        currency=currency,
                        exchange_rate=exchange_rate,
                        balance_after=float(raw.get("balance") or 0) if raw.get("balance") else None,
                        is_internal_transfer=is_transfer,
                        upload_history_id=upload_history_id,
                    )
                )
            if txns:
                await self.txn_repo.bulk_insert(txns)

            log.info(f"save.bank_statement.done account_code={account_code} txns={len(txns)} period={period}")
            results.append({
                "account_id": account.id,
                "period": str(period),
                "transactions": [
                    {
                        "date": str(t.txn_date),
                        "description": t.description,
                        "amount": t.amount,
                        "balance": t.balance_after,
                        "category": t.category.value if hasattr(t.category, "value") else str(t.category),
                    } for t in txns
                ]
            })

        if not results:
            return {"status": "empty"}
        if len(results) == 1:
            return results[0]
        # Multi-account: return all results, exposing first account_id at top level for compatibility
        return {
            "account_id": results[0]["account_id"],
            "period": results[0]["period"],
            "accounts": results,
        }

    # ── Credit card statement ──────────────────────────────────────────────

    async def ingest_credit_card_statement(
        self, pdf_path: Path, account_code: str | None = None, upload_history_id: int | None = None
    ) -> dict[str, Any]:
        """Parse a credit card statement PDF and save bill + line items."""
        log.info(f"ingest.credit_card.start path={pdf_path}")
        data = await parse_credit_card_statement(pdf_path)
        if account_code:
            data["account_code"] = account_code
        return await self.save_credit_card_statement(data, upload_history_id)

    async def save_credit_card_statement(
        self, data: dict[str, Any], upload_history_id: int | None = None
    ) -> dict[str, Any]:
        period = first_of_month(data["period_year"], data["period_month"])
        db_accounts = await self.account_repo.get_all()
        card_last_four = data.get("card_last_four")
        if card_last_four:
            card_last_four = re.sub(r'[^0-9]', '', str(card_last_four))
        account_code = data.get("account_code")
        card_name = None
        if not account_code:
            inst = data.get("institution", "").strip()
            matching_acc = self._find_matching_db_account(inst, card_last_four, AccountType.CREDIT_CARD, db_accounts)
            if matching_acc:
                account_code = matching_acc.code
                card_name = matching_acc.name
            else:
                account_code = f"cc_{card_last_four or 'xxxx'}"

        if not card_name:
            card_name = f"{data.get('institution', 'Card')} ****{card_last_four or '0000'}"

        account = await self._resolve_or_create_account(
            code=account_code,
            name=card_name,
            account_type=AccountType.CREDIT_CARD,
            institution=data.get("institution", ""),
        )

        payment_due = data.get("payment_due_date")
        payment_due_date = None
        if payment_due:
            if isinstance(payment_due, str):
                from datetime import datetime
                try:
                    payment_due_date = datetime.strptime(payment_due, "%Y-%m-%d").date()
                except ValueError:
                    payment_due_date = parse_tw_date_robust(payment_due)
            else:
                payment_due_date = parse_tw_date_robust(payment_due)

        # Save snapshot
        snapshot = AccountSnapshot(
            account_id=account.id,
            period_date=period,
            balance=-float(data.get("total_amount") or 0),
            currency="TWD",
            exchange_rate=1.0,
            payment_due_date=payment_due_date,
            source="pdf",
            raw_data=json.dumps(data, ensure_ascii=False),
            upload_history_id=upload_history_id,
        )
        await self.snapshot_repo.upsert(snapshot)

        # Save transactions
        txns = []
        raw_items = data.get("items") or data.get("transactions") or []
        for item in raw_items:
            is_ref = bool(item.get("is_refund", False))
            amt_orig = float(item.get("amount") or 0)
            if is_ref:
                amount = abs(amt_orig)
            else:
                amount = -abs(amt_orig) if amt_orig > 0 else amt_orig
            
            txn_date = item.get("date")
            if isinstance(txn_date, str):
                from datetime import datetime
                try:
                    actual_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    actual_date = parse_tw_date_robust(txn_date) or period
            else:
                actual_date = parse_tw_date_robust(item.get("date")) or period

            txns.append(
                Transaction(
                    account_id=account.id,
                    txn_date=actual_date,
                    merchant=item.get("merchant") or item.get("description") or "",
                    description=item.get("description") or item.get("merchant") or "",
                    amount=amount,
                    balance_after=None,
                    category=TransactionCategory.EXPENSE,
                    is_internal_transfer=False,
                    is_refund=bool(item.get("is_refund", False)),
                    raw_data=json.dumps(item, ensure_ascii=False),
                    source=TransactionSource.CREDIT_CARD,
                    payment_method=item.get("payment_method"),
                    invoice_number=None,
                    upload_history_id=upload_history_id,
                )
            )
        await self.txn_repo.bulk_insert(txns)

        # Classify with Gemini (merchant + description for full context)
        try:
            from src.utils.category_classifier import classify_transactions_batch, _category_to_enum
            rule_repo = CategoryRuleRepository(self.db, self.user_id)
            rules = list(await rule_repo.list_all())
            classify_items = [
                {"id": str(t.id), "merchant": t.merchant or "", "description": t.description or ""}
                for t in txns
            ]
            classification = await classify_transactions_batch(classify_items, rules)
            for t in txns:
                cat = classification.get(str(t.id))
                if cat:
                    t.category = _category_to_enum(cat)
            await self.db.flush()
        except Exception as e:
            log.warning(f"Credit card classification failed: {e}")

        await self.deduplicate_period(period)

        log.info(f"save.credit_card.done txns={len(txns)} period={period}")
        return {
            "account_id": account.id,
            "period": str(period),
            "total_amount": float(data.get("total_amount", 0)),
            "payment_due_date": str(payment_due_date) if payment_due_date else None,
            "transactions": [
                {
                    "date": str(t.txn_date),
                    "merchant": t.merchant,
                    "description": t.description,
                    "amount": t.amount,
                    "is_refund": t.is_refund,
                } for t in txns
            ]
        }

    # ── Brokerage statement ────────────────────────────────────────────────

    async def ingest_brokerage_statement(
        self, pdf_path: Path, account_code: str | None = None, upload_history_id: int | None = None, filename: str = ""
    ) -> dict[str, Any]:
        """Parse a brokerage statement PDF and save holdings + cash snapshot."""
        log.info(f"ingest.brokerage.start path={pdf_path} filename={filename}")
        
        is_firstrade = "firstrade" in filename.lower()
        if is_firstrade:
            data = await parse_firstrade_statement(pdf_path)
        else:
            data = await parse_brokerage_statement(pdf_path)
        if account_code:
            data["account_code"] = account_code
        return await self.save_brokerage_statement(data, upload_history_id)

    async def save_brokerage_statement(
        self, data: dict[str, Any], upload_history_id: int | None = None
    ) -> dict[str, Any]:
        currency = data.get("currency", "TWD")
        exchange_rate = float(data.get("exchange_rate", 1.0))

        import re
        import calendar
        from datetime import date
        is_firstrade = (data.get("institution") or "").lower() == "firstrade" or "firstrade" in (data.get("account_code") or "").lower()
        if is_firstrade:
            last_day = calendar.monthrange(data["period_year"], data["period_month"])[1]
            period = date(data["period_year"], data["period_month"], last_day)
        else:
            period = first_of_month(data["period_year"], data["period_month"])
        account_code = data.get("account_code")
        acc_num_raw = data.get("account_number")
        # For matching: keep asterisks/X as wildcards (strip only hyphens/spaces)
        acc_num_match = re.sub(r'[^0-9*xX]', '', str(acc_num_raw)) if acc_num_raw else None
        # For storage as code: digits only (preserve leading zeros)
        acc_num_digits = re.sub(r'[^0-9]', '', str(acc_num_raw)) if acc_num_raw else None
        db_accounts = await self.account_repo.get_all()
        display_name = None
        if not account_code:
            inst = data.get("institution", "unknown").strip()
            matching_acc = self._find_matching_db_account(inst, acc_num_match, AccountType.BROKERAGE, db_accounts)
            if matching_acc:
                account_code = matching_acc.code
                display_name = matching_acc.name
            else:
                # New account — code is digits-only account number
                if acc_num_digits:
                    account_code = acc_num_digits
                else:
                    account_code = f"broker_{inst}"

        if not display_name:
            display_name = data.get("institution", "Unknown Broker")

        account = await self._resolve_or_create_account(
            code=account_code,
            name=display_name,
            account_type=AccountType.BROKERAGE,
            institution=data.get("institution", ""),
            currency=currency,
        )

        total_market_value = float(data.get("total_market_value") or 0)
        
        # Standard domestic Taiwan brokerages settle through separate bank accounts (already tracked).
        # Therefore, we only count cash balance for foreign brokerages (e.g., Firstrade).
        inst_lower = (data.get("institution") or "").lower()
        if "firstrade" in inst_lower:
            cash_balance = float(data.get("cash_balance") or 0)
            # Firstrade's "TOTAL PRICED PORTFOLIO" already includes cash; derive securities value from holdings
            holdings_mv = sum(float(h.get("market_value") or 0) for h in data.get("holdings", []))
            if holdings_mv > 0:
                total_market_value = holdings_mv
        else:
            cash_balance = 0.0

        balance_orig = total_market_value + cash_balance
        
        # Save snapshot
        snapshot = AccountSnapshot(
            account_id=account.id,
            period_date=period,
            balance=round(balance_orig * exchange_rate),
            original_balance=balance_orig if currency != "TWD" else None,
            currency=currency,
            exchange_rate=exchange_rate,
            source="pdf",
            raw_data=json.dumps(data, ensure_ascii=False),
            upload_history_id=upload_history_id,
        )
        await self.snapshot_repo.upsert(snapshot)

        # Holdings / Securities
        securities = []
        for h in data.get("holdings", []):
            qty = float(h.get("quantity") or 0)
            avg_cost = float(h.get("avg_cost") or 0)
            price = float(h.get("current_price") or 0)
            
            mv_orig = float(h.get("market_value") or 0)
            if not mv_orig or mv_orig == 0:
                mv_orig = qty * price
                
            pnl_orig = float(h.get("unrealized_pnl") or 0)
            if not pnl_orig or pnl_orig == 0:
                if avg_cost > 0:
                    pnl_orig = (price - avg_cost) * qty
                else:
                    pnl_orig = 0.0

            securities.append(
                Security(
                    account_id=account.id,
                    period_date=period,
                    ticker=h.get("ticker") or h.get("name") or "Unknown",
                    name=h.get("name") or "",
                    quantity=qty,
                    avg_cost=round(avg_cost * exchange_rate),
                    current_price=round(price * exchange_rate),
                    market_value=round(mv_orig * exchange_rate),
                    unrealized_pnl=round(pnl_orig * exchange_rate),
                    original_avg_cost=avg_cost if currency != "TWD" else None,
                    original_current_price=price if currency != "TWD" else None,
                    original_market_value=mv_orig if currency != "TWD" else None,
                    original_unrealized_pnl=pnl_orig if currency != "TWD" else None,
                    currency=currency,
                    exchange_rate=exchange_rate,
                    upload_history_id=upload_history_id,
                )
            )

        # Fallback if no holdings
        if not securities and balance_orig > 0:
            securities.append(
                Security(
                    account_id=account.id,
                    period_date=period,
                    ticker="N/A",
                    name=f"{account.name} 總部位",
                    quantity=1,
                    avg_cost=round(total_market_value * exchange_rate),
                    current_price=round(total_market_value * exchange_rate),
                    market_value=round(total_market_value * exchange_rate),
                    unrealized_pnl=0,
                    original_avg_cost=total_market_value if currency != "TWD" else None,
                    original_current_price=total_market_value if currency != "TWD" else None,
                    original_market_value=total_market_value if currency != "TWD" else None,
                    original_unrealized_pnl=0 if currency != "TWD" else None,
                    currency=currency,
                    exchange_rate=exchange_rate,
                )
            )

        await self.security_repo.upsert_many(securities)

        # Transactions
        txns = []
        for t in data.get("transactions", []):
            action = t.get("action") or ""
            amt_orig = float(t.get("amount") or 0)
            if "買" in action or action.upper() == "BUY":
                amt_orig = -abs(amt_orig)
            elif "賣" in action or action.upper() == "SELL" or action.upper() == "DIVIDEND" or action.upper() == "INTEREST":
                amt_orig = abs(amt_orig)
            elif action.upper() == "TAX":
                amt_orig = -abs(amt_orig)
            else:
                amt_orig = abs(amt_orig)

            amt = round(amt_orig * exchange_rate)
            ticker = t.get("ticker")
            ticker_str = f" ({ticker})" if ticker else ""
            desc = f"{action} {t.get('name') or ''}{ticker_str}"
            fee_orig = float(t.get("fee") or 0)
            if fee_orig > 0:
                desc += f" (含手續費/稅: {fee_orig})"

            category = TransactionCategory.INVESTMENT
            if action.upper() == "DIVIDEND":
                category = TransactionCategory.DIVIDEND
            elif action.upper() == "INTEREST":
                category = TransactionCategory.INTEREST
            elif action.upper() == "TAX":
                category = TransactionCategory.EXPENSE

            txn_date = t.get("date")
            if isinstance(txn_date, str):
                from datetime import datetime
                try:
                    actual_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    actual_date = parse_tw_date_robust(txn_date) or period
            else:
                actual_date = parse_tw_date_robust(t.get("date")) or period

            txns.append(
                Transaction(
                    account_id=account.id,
                    txn_date=actual_date,
                    merchant="Brokerage Trade",
                    description=desc.strip(),
                    amount=amt,
                    original_amount=amt_orig if currency != "TWD" else None,
                    currency=currency,
                    exchange_rate=exchange_rate,
                    balance_after=None,
                    category=category,
                    is_internal_transfer=False,
                    source=TransactionSource.BROKERAGE,
                    raw_data=json.dumps(t, ensure_ascii=False),
                    upload_history_id=upload_history_id
                )
            )
        if txns:
            await self.txn_repo.bulk_insert(txns)

        log.info(f"save.brokerage.done holdings={len(securities)} txns={len(txns)} period={period}")
        return {
            "account_id": account.id,
            "period": str(period),
            "cash_balance": float(data.get("cash_balance") or 0),
            "total_market_value": snapshot.balance,
            "transactions": [
                {
                    "date": str(t.txn_date),
                    "amount": t.amount,
                    "description": t.description
                } for t in txns
            ],
            "holdings": [
                {
                    "ticker": s.ticker,
                    "name": s.name,
                    "quantity": s.quantity,
                    "avg_cost": s.avg_cost,
                    "current_price": s.current_price,
                    "market_value": s.market_value,
                    "unrealized_pnl": s.unrealized_pnl,
                } for s in securities
            ]
        }

    # ── E-Invoice Ingestion & Deduplication ────────────────────────────────
    
    async def ingest_einvoice(self, pdf_path: Path, upload_history_id: int | None = None) -> dict[str, Any]:
        """Parse an electronic invoice PDF and save items, flagging duplicates."""
        log.info(f"ingest.einvoice.start path={pdf_path}")
        data = await parse_einvoice_statement(pdf_path)
        return await self.save_einvoice_statement(data, upload_history_id)

    async def save_einvoice_statement(
        self, data: dict[str, Any], upload_history_id: int | None = None
    ) -> dict[str, Any]:
        period = first_of_month(data["period_year"], data["period_month"])

        txns = []
        unclassified: list[Transaction] = []
        raw_items = data.get("items") or data.get("transactions") or []
        for item in raw_items:
            # Skip database writes for duplicate transactions
            if item.get("is_duplicate", False):
                log.info(f"Skipping duplicate e-invoice transaction from database insertion: {item.get('merchant')}, {item.get('amount')}")
                continue

            txn_date = item.get("date")
            if isinstance(txn_date, str):
                from datetime import datetime
                try:
                    actual_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    actual_date = parse_tw_date_robust(txn_date) or period
            else:
                actual_date = parse_tw_date_robust(item.get("date")) or period

            cat_str = item.get("category")
            category = TransactionCategory.EXPENSE
            if cat_str:
                reverse_cat = {
                    "薪資": "SALARY",
                    "投資": "INVESTMENT",
                    "轉入": "TRANSFER_IN",
                    "轉出": "TRANSFER_OUT",
                    "固定支出": "EXPENSE",
                    "食物": "FOOD",
                    "交通": "TRANSPORT",
                    "醫療": "MEDICAL",
                    "娛樂": "ENTERTAINMENT",
                    "保險": "INSURANCE",
                    "運動": "EXERCISE",
                    "購物": "SHOPPING",
                    "旅遊": "TRAVEL",
                    "學習": "STUDY",
                    "信用卡繳款": "CREDIT_CARD_PAYMENT",
                    "本金償還": "DEBT_REPAYMENT",
                    "股利": "DIVIDEND",
                    "利息": "INTEREST",
                    "非固定支出": "OTHER",
                    "非固定收入": "OTHER",
                    "其他支出": "OTHER",
                    "其他收入": "OTHER",
                    "其他": "OTHER",
                }
                cat_val = reverse_cat.get(cat_str, cat_str)
                if isinstance(cat_val, str):
                    cat_val = cat_val.upper()
                try:
                    category = TransactionCategory(cat_val)
                except ValueError:
                    category = TransactionCategory.EXPENSE

            txn = Transaction(
                account_id=None,
                txn_date=actual_date,
                merchant=item.get("merchant") or "",
                description=item.get("description") or "",
                amount=-abs(float(item.get("amount") or 0)),
                balance_after=None,
                category=category,
                is_internal_transfer=False,
                is_refund=False,
                raw_data=json.dumps(item, ensure_ascii=False),
                source=TransactionSource.E_INVOICE,
                payment_method=item.get("payment_method", "其他"),
                invoice_number=item.get("invoice_number"),
                is_duplicate=False,  # They are saved, so they are not duplicates
                upload_history_id=upload_history_id,
            )
            txns.append(txn)
            # Keep the category confirmed in the preview; only classify items that have none
            if not cat_str:
                unclassified.append(txn)

        if txns:
            await self.txn_repo.bulk_insert(txns)

        # Classify with Gemini (merchant + description for full context)
        try:
            from src.utils.category_classifier import classify_transactions_batch, _category_to_enum
            rule_repo = CategoryRuleRepository(self.db, self.user_id)
            rules = list(await rule_repo.list_all())
            classify_items = [
                {"id": str(t.id), "merchant": t.merchant or "", "description": t.description or ""}
                for t in unclassified
            ]
            if classify_items:
                classification = await classify_transactions_batch(classify_items, rules)
                for t in unclassified:
                    cat = classification.get(str(t.id))
                    if cat:
                        t.category = _category_to_enum(cat)
                await self.db.flush()
        except Exception as e:
            log.warning(f"E-invoice classification failed: {e}")
        # Note: deduplicate_period is skipped here because duplicates are already excluded before insertion.

        log.info(f"save.einvoice.done items={len(txns)} period={period}")
        return {
            "period": str(period),
            "items": [
                {
                    "date": str(t.txn_date),
                    "merchant": t.merchant,
                    "description": t.description,
                    "amount": t.amount,
                    "payment_method": t.payment_method,
                    "invoice_number": t.invoice_number,
                } for t in txns
            ]
        }

    async def deduplicate_period(self, period_date: date) -> None:
        """Flags e-invoice transactions that overlap with credit card transactions."""
        cc_txns = await self.txn_repo.get_by_period_and_source(period_date, TransactionSource.CREDIT_CARD)
        einvoice_txns = await self.txn_repo.get_by_period_and_source(period_date, TransactionSource.E_INVOICE)
        if not einvoice_txns:
            return

        # Reset all duplicate flags for the current period first
        await self.txn_repo.mark_duplicates([e.id for e in einvoice_txns], False)

        matched_cc_ids = set()
        einvoice_dup_ids: list[int] = []

        for e in einvoice_txns:
            e_merchant = e.merchant or e.description or ""
            e_amount_abs = abs(e.amount)

            for c in cc_txns:
                if c.id in matched_cc_ids:
                    continue
                
                day_diff = abs((c.txn_date - e.txn_date).days)
                amount_diff = abs(abs(c.amount) - e_amount_abs)
                
                if day_diff <= 1 and amount_diff < 0.01:
                    c_merchant = c.merchant or c.description or ""
                    if is_merchant_overlap(e_merchant, c_merchant):
                        matched_cc_ids.add(c.id)
                        einvoice_dup_ids.append(e.id)
                        log.info(f"einvoice.deduplicate.match einvoice_txn={e.id} cc_txn={c.id} amount={e.amount}")
                        break

        if einvoice_dup_ids:
            await self.txn_repo.mark_duplicates(einvoice_dup_ids, True)

    async def parse_statement(self, pdf_path: Path, kind: str, filename: str = "") -> dict[str, Any]:
        """
        Parse a PDF statement without saving it to the database.
        Returns the raw parsed data, injecting exchange rate and other common details.
        """
        log.info(f"parse_statement path={pdf_path} kind={kind} filename={filename}")
        if kind == "bank":
            data = await parse_bank_statement(pdf_path)
            data["kind"] = "bank"
            # Inject end-of-month exchange rate for any non-TWD accounts during preview
            if data.get("accounts"):
                from datetime import date as _date
                py = data.get("period_year")
                pm = data.get("period_month")
                if py and pm:
                    last_day = calendar.monthrange(py, pm)[1]
                    eom_date = _date(py, pm, last_day)
                    for acc in data["accounts"]:
                        ccy = acc.get("currency", "TWD")
                        if ccy and ccy != "TWD":
                            try:
                                rate = await get_currency_twd_rate(eom_date, from_currency=ccy)
                                acc["exchange_rate"] = rate
                            except Exception as _e:
                                log.warning(f"Could not fetch {ccy}/TWD rate during parse: {_e}")
        elif kind == "credit_card":
            data = await parse_credit_card_statement(pdf_path)
            data["kind"] = "credit_card"
            # Screenshots of the transaction list often lack the billed total; estimate it from the items
            if data.get("total_amount") is None:
                data["total_amount"] = round(sum(
                    -abs(float(i.get("amount") or 0)) if i.get("is_refund") else abs(float(i.get("amount") or 0))
                    for i in data.get("items", [])
                ), 2)
                data["total_amount_computed"] = True
        elif kind == "brokerage":
            is_firstrade = "firstrade" in filename.lower()
            if is_firstrade:
                data = await parse_firstrade_statement(pdf_path)
            else:
                data = await parse_brokerage_statement(pdf_path)
            data["kind"] = "brokerage"
            
            currency = data.get("currency", "TWD")
            exchange_rate = 1.0
            if currency == "USD":
                period = first_of_month(data["period_year"], data["period_month"])
                last_day = calendar.monthrange(period.year, period.month)[1]
                target_date = date(period.year, period.month, last_day)
                try:
                    exchange_rate = await get_usd_twd_rate(target_date)
                except Exception as e:
                    log.warning(f"Could not fetch exchange rate during parse phase: {e}")
                    exchange_rate = 32.5
            data["exchange_rate"] = exchange_rate
            data["currency"] = currency
        elif kind == "einvoice":
            if filename.lower().endswith(".csv") or str(pdf_path).lower().endswith(".csv"):
                data = await parse_einvoice_csv(pdf_path)
            else:
                data = await parse_einvoice_statement(pdf_path)
            data["kind"] = "einvoice"
            
            # E-invoice items are always consumption (expenses) — normalize to negative
            for item in data.get("items", []):
                raw_amt = float(item.get("amount") or 0)
                item["amount"] = -abs(raw_amt)
            
            # Dry-run duplicate check against existing credit card transactions

            try:
                from datetime import datetime
                period_date = date(data["period_year"], data["period_month"], 1)
                cc_txns = await self.txn_repo.get_by_period_and_source(period_date, TransactionSource.CREDIT_CARD)

                last_day = calendar.monthrange(period_date.year, period_date.month)[1]
                rule_txns: dict[int, list] = {}
                for idx, rule in enumerate(EINVOICE_PAYMENT_RULES):
                    rule_txns[idx] = list(await self.txn_repo.get_txns_by_institution(
                        period_date - timedelta(days=rule["days"]),
                        period_date.replace(day=last_day) + timedelta(days=rule["days"]),
                        rule["source"],
                        rule["institutions"],
                    ))
                matched_rule_ids = set()

                matched_cc_ids = set()
                for item in data.get("items", []):
                    item_date_str = item.get("date")
                    try:
                        item_date = datetime.strptime(item_date_str, "%Y-%m-%d").date()
                    except Exception:
                        item_date = period_date
                    item_amt = float(item.get("amount") or 0)
                    item_merchant = item.get("merchant") or ""
                    
                    is_dup = False
                    for c in cc_txns:
                        if c.id in matched_cc_ids:
                            continue
                        day_diff = abs((c.txn_date - item_date).days)
                        amt_diff = abs(abs(c.amount) - abs(item_amt))
                        if day_diff <= 1 and amt_diff < 0.01:
                            c_merchant = c.merchant or c.description or ""
                            if is_merchant_overlap(item_merchant, c_merchant):
                                matched_cc_ids.add(c.id)
                                is_dup = True
                                break

                    rule = match_payment_rule(item_merchant)
                    if not is_dup and rule:
                        for t in rule_txns[EINVOICE_PAYMENT_RULES.index(rule)]:
                            if t.id in matched_rule_ids or t.id in matched_cc_ids or t.amount >= 0:
                                continue
                            day_diff = abs((t.txn_date - item_date).days)
                            amt_diff = abs(abs(t.amount) - abs(item_amt))
                            if day_diff <= rule["days"] and amt_diff < 0.01:
                                matched_rule_ids.add(t.id)
                                is_dup = True
                                log.info(f"einvoice.deduplicate.payment_rule_match txn={t.id} source={rule['source'].value} amount={item_amt}")
                                break
                    item["is_duplicate"] = is_dup

                try:
                    from src.utils.category_classifier import classify_transactions_batch, label
                    from src.dbs.repository import CategoryRuleRepository
                    rule_repo = CategoryRuleRepository(self.db, self.user_id)
                    rules = list(await rule_repo.list_all())
                    classify_items = [
                        {"id": str(idx), "merchant": item.get("merchant") or "", "description": item.get("description") or ""}
                        for idx, item in enumerate(data.get("items", []))
                    ]
                    if classify_items:
                        classification = await classify_transactions_batch(classify_items, rules)
                        for idx, item in enumerate(data.get("items", [])):
                            cat_key = classification.get(str(idx), "other")
                            item["category"] = label(cat_key)
                except Exception as e:
                    log.warning(f"Classification during parse phase failed: {e}")
            except Exception as e:
                log.warning(f"Dry-run duplicate check failed: {e}")
        else:
            raise ValueError(f"Unknown kind: {kind}")
            
        return data

    async def save_parsed_statement(self, data: dict[str, Any], upload_history_id: int | None = None) -> dict[str, Any]:
        """
        Save parsed and verified data payload into database.
        """
        kind = data.get("kind")
        if not kind:
            raise ValueError("Data payload is missing 'kind'")
            
        if kind == "bank":
            return await self.save_bank_statement(data, upload_history_id)
        elif kind == "credit_card":
            return await self.save_credit_card_statement(data, upload_history_id)
        elif kind == "brokerage":
            return await self.save_brokerage_statement(data, upload_history_id)
        elif kind == "einvoice":
            return await self.save_einvoice_statement(data, upload_history_id)
        else:
            raise ValueError(f"Unsupported kind for saving: {kind}")

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _resolve_or_create_account(
        self,
        code: str,
        name: str,
        account_type: AccountType,
        institution: str,
        currency: str = "TWD",
    ) -> Account:
        account = await self.account_repo.get_by_code(code)
        if not account:
            clean_code = re.sub(r'[^0-9]', '', code)
            all_accounts = await self.account_repo.get_all()
            for acc in all_accounts:
                # Match digits-only code against old-style "bank_inst_number" or "broker_inst_number"
                acc_digits = re.sub(r'[^0-9]', '', acc.code)
                if not (acc_digits and clean_code and acc_digits == clean_code):
                    continue
                # Don't collapse a different currency's sub-ledger onto this account
                # just because the digits-only code matches (see currency-aware
                # matching note in _find_matching_db_account).
                if acc.currency and acc.currency != currency:
                    continue
                account = acc
                break

        if not account:
            account = await self.account_repo.create(
                Account(
                    code=code, name=name, account_type=account_type, institution=institution, currency=currency
                )
            )
            log.info(f"account.created id={account.id} code={code} currency={currency}")
        elif account.currency != currency:
            account.currency = currency
            self.db.add(account)
            await self.db.flush()
            log.info(f"account.updated id={account.id} code={code} set currency={currency}")
        return account

    def _institutions_match(self, inst1: str, inst2: str) -> bool:
        if not inst1 or not inst2:
            return False
        
        def clean(s: str) -> str:
            s = s.lower()
            for suffix in ["銀行", "證券", "commercial", "bank", "securities", "co", "ltd"]:
                s = s.replace(suffix, "")
            import re
            s = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '', s)
            return s.strip()
        
        c1, c2 = clean(inst1), clean(inst2)
        if not c1 or not c2:
            return False
            
        if c1 in c2 or c2 in c1:
            return True
            
        synonyms = [
            {"台新", "taishin", "richart"},
            {"玉山", "esun"},
            {"永豐", "sinopac"},
            {"國泰", "cathay", "國泰世華"},
            {"中信", "ctbc", "中國信託"},
            {"第一", "first"},
            {"富邦", "fubon"},
            {"華南", "huanan", "sny"},
            {"星展", "dbs"},
            {"聯邦", "union"},
        ]
        for syn in synonyms:
            has_c1 = any(item in c1 or c1 in item for item in syn)
            has_c2 = any(item in c2 or c2 in item for item in syn)
            if has_c1 and has_c2:
                return True
                
        return False

    def _get_db_account_numbers(self, db_acc: Account) -> list[str]:
        candidates = []
        code = db_acc.code
        if "_" in code:
            # Old-style: bank_institution_number — take last segment (the number)
            parts = code.split("_")
            candidates.append(parts[-1])
        else:
            # New-style: just the digits
            candidates.append(code)

        import re
        # Also scan the name for any digit sequences (for accounts with numbers in name)
        name_candidates = re.findall(r'[0-9*xX\-]+', db_acc.name)
        for cand in name_candidates:
            clean = re.sub(r'[^0-9]', '', cand)
            if len(clean) >= 4 or '*' in cand or 'x' in cand.lower():
                candidates.append(cand)

        if db_acc.notes:
            notes_candidates = re.findall(r'[0-9*xX\-]+', db_acc.notes)
            for cand in notes_candidates:
                clean = re.sub(r'[^0-9]', '', cand)
                if len(clean) >= 4 or '*' in cand or 'x' in cand.lower():
                    candidates.append(cand)

        return list(set(candidates))

    def fuzzy_match_acc_nums(self, db_num: str, parsed_num: str) -> bool:
        import re
        clean_db = re.sub(r'[^0-9*xX]', '', db_num)
        clean_parsed = re.sub(r'[^0-9*xX]', '', parsed_num)
        
        if not clean_db or not clean_parsed:
            return False
            
        if clean_db == clean_parsed:
            return True
            
        db_digits = re.sub(r'[*xX]', '', clean_db)
        parsed_digits = re.sub(r'[*xX]', '', clean_parsed)
        
        if not db_digits or not parsed_digits:
            return False
            
        if len(parsed_digits) >= 4 and clean_db.endswith(parsed_digits):
            return True
        if len(db_digits) >= 4 and clean_parsed.endswith(db_digits):
            return True

        parts = [p for p in re.split(r'[*xX]+', clean_parsed) if p]
        if len(parts) >= 2:
            prefix = parts[0]
            suffix = parts[-1]
            if len(prefix) >= 3 and len(suffix) >= 3:
                if clean_db.startswith(prefix) and clean_db.endswith(suffix):
                    return True

        def match_equal_len(s1: str, s2: str) -> bool:
            match_count = 0
            for c1, c2 in zip(s1, s2):
                if c1 != c2 and c1 not in '*xX' and c2 not in '*xX':
                    return False
                if c1 == c2 and c1 not in '*xX':
                    match_count += 1
            return match_count >= 4
            
        len_db = len(clean_db)
        len_parsed = len(clean_parsed)
        
        if len_parsed == len_db:
            return match_equal_len(clean_parsed, clean_db)
        elif len_parsed < len_db:
            for start in range(len_db - len_parsed + 1):
                if match_equal_len(clean_db[start : start + len_parsed], clean_parsed):
                    return True
        else:
            for start in range(len_parsed - len_db + 1):
                if match_equal_len(clean_parsed[start : start + len_db], clean_db):
                    return True
                    
        return False

    def _find_matching_db_account(
        self,
        institution: str,
        parsed_acc_num: str,
        account_type: AccountType,
        db_accounts: list[Account],
        currency: str | None = None,
    ) -> Account | None:
        if not parsed_acc_num:
            return None

        for db_acc in db_accounts:
            if db_acc.account_type != account_type:
                continue
            if not self._institutions_match(db_acc.institution, institution):
                continue
            # One physical account number can hold multiple currency sub-ledgers
            # (e.g. a foreign-currency account with both JPY and USD balances).
            # Require the currency to match once it's known, otherwise a statement
            # for a different currency would collide onto the same Account row and
            # silently overwrite its balance/currency (see balance-sheet duplicate
            # rows caused by this collision).
            if currency and db_acc.currency and db_acc.currency != currency:
                continue

            db_nums = self._get_db_account_numbers(db_acc)
            for db_num in db_nums:
                if self.fuzzy_match_acc_nums(db_num, parsed_acc_num):
                    return db_acc
                    
        return None

