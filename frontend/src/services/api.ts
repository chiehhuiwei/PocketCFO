// frontend/src/services/api.ts
import axios from "axios";
import { toast } from "../store/useToastStore";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "/api/v1",
  timeout: 120_000, // PDF parsing can take time (increased to 2 min to prevent timeout)
});

// Interceptor to strip leading slash, inject JWT token, and confirm receipt of
// slow mutations (many backend actions — PDF parsing, broker syncs — take a
// while to respond, so let the user know the click was registered).
api.interceptors.request.use((config) => {
  if (config.url && config.url.startsWith("/")) {
    config.url = config.url.substring(1);
  }
  const token = localStorage.getItem("pocketcfo_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const method = (config.method || "get").toLowerCase();
  if (method !== "get" && !config.url?.includes("auth/login")) {
    toast.info("已送出請求，處理中…", 2500);
  }
  return config;
});

// Caching system for GET requests
const cache: Record<string, { data: any; expiry: number }> = {};
const CACHE_TTL = 300_000; // 5 minutes Cache Time-To-Live (cache is cleared on any mutation anyway)

async function fetchWithCache(url: string, params?: any) {
  const cacheKey = url + (params ? JSON.stringify(params) : "");
  const now = Date.now();
  if (cache[cacheKey] && cache[cacheKey].expiry > now) {
    return cache[cacheKey].data;
  }
  const data = await api.get(url, { params }).then(res => res.data);
  cache[cacheKey] = { data, expiry: now + CACHE_TTL };
  return data;
}

export function clearApiCache() {
  for (const key in cache) {
    delete cache[key];
  }
}

// Clear cache automatically on mutations and handle unauthorized access
api.interceptors.response.use(
  (response) => {
    if (response.config.method && response.config.method.toLowerCase() !== "get") {
      clearApiCache();
    }
    return response;
  },
  (error) => {
    if (error.config && error.config.method && error.config.method.toLowerCase() !== "get") {
      clearApiCache();
    }
    if (error.response && error.response.status === 401) {
      // Don't auto-redirect if we are already trying to login
      if (error.config && !error.config.url.includes("/auth/login")) {
        localStorage.removeItem("pocketcfo_token");
        window.dispatchEvent(new Event("pocketcfo_unauthorized"));
      }
    }
    return Promise.reject(error);
  }
);

// ── Uploads ──────────────────────────────────────────────────────────────────

export type StatementKind = "bank" | "credit_card" | "brokerage" | "einvoice";

export async function uploadStatement(
  file: File,
  kind: StatementKind,
  accountCode?: string,
  password?: string
) {
  const form = new FormData();
  form.append("file", file);
  form.append("kind", kind);
  if (accountCode) form.append("account_code", accountCode);
  if (password) form.append("password", password);
  const { data } = await api.post("/upload/statement", form);
  return data;
}

export type UploadHistoryRecord = {
  id: number;
  filename: string;
  kind: string;
  status: string;
  message: string | null;
  created_at: string;
};

export async function getUploadHistory() {
  const data = await fetchWithCache("/upload/history");
  return data as UploadHistoryRecord[];
}

export async function deleteUploadHistory(id: number) {
  const { data } = await api.delete(`/upload/history/${id}`);
  return data;
}

// ── Balance Sheet ────────────────────────────────────────────────────────────

export async function getBalanceSheetHistory() {
  const data = await fetchWithCache("/balance-sheet/");
  console.log("getBalanceSheetHistory data:", typeof data, Array.isArray(data), data);
  return data as BalanceSheetRecord[];
}

export async function computeBalanceSheet(year: number, month: number) {
  const { data } = await api.post("/balance-sheet/compute", null, {
    params: { year, month },
  });
  return data;
}

export async function syncBrokerData(year: number, month: number) {
  const { data } = await api.post("/balance-sheet/sync-broker", null, {
    params: { year, month },
  });
  return data;
}

// ── Income Statement ─────────────────────────────────────────────────────────

export async function getIncomeStatementHistory() {
  const data = await fetchWithCache("/income-statement/");
  console.log("getIncomeStatementHistory data:", typeof data, Array.isArray(data), data);
  return data as IncomeStatementRecord[];
}

export async function computeIncomeStatement(year: number, month: number) {
  const { data } = await api.post("/income-statement/compute", null, {
    params: { year, month },
  });
  return data;
}

// ── Accounts ─────────────────────────────────────────────────────────────────

export async function getAccounts(includeAll?: boolean) {
  const data = await fetchWithCache("/accounts/", { include_all: includeAll });
  return data as Account[];
}

export async function createAccount(
  name: string,
  type: string,
  institution: string,
  currency: string = "TWD",
  code?: string,
  isInstallment: boolean = false,
  installmentAmount: number = 0.0,
  installmentStartDate?: string,
  installmentCount?: number,
  installmentPaymentDay?: number
) {
  const { data } = await api.post("/accounts/", {
    name,
    account_type: type,
    institution,
    currency,
    code,
    is_installment: isInstallment,
    installment_amount: installmentAmount,
    installment_start_date: installmentStartDate,
    installment_count: installmentCount,
    installment_payment_day: installmentPaymentDay,
  });
  return data;
}

export interface AccountWithSnapshot extends Account {
  balance: number | null;
  original_balance: number | null;
  has_snapshot: boolean;
  snapshot_source: string | null;
  manual_cash_override: number | null;
}

export async function getAccountsWithSnapshots(year: number, month: number) {
  const data = await fetchWithCache("/accounts/snapshots", { year, month });
  return data as AccountWithSnapshot[];
}

export async function saveAccountSnapshot(
  accountId: number,
  periodDate: string,
  balance: number,
  cashOverride?: number | null
) {
  const { data } = await api.post(`/accounts/${accountId}/snapshots`, {
    period_date: periodDate,
    balance,
    cash_override: cashOverride ?? null,
  });
  return data;
}

export async function deleteAccountSnapshot(
  accountId: number,
  periodDate: string
) {
  const { data } = await api.delete(`/accounts/${accountId}/snapshots/${periodDate}`);
  return data;
}


// ── Types ────────────────────────────────────────────────────────────────────

export interface BalanceSheetRecord {
  period: string;
  total_assets: number;
  total_liabilities: number;
  net_worth: number;
  total_cash: number;
  total_securities_market_value: number;
  detail?: any;
}

export interface IncomeStatementRecord {
  period: string;
  total_income: number;
  total_expenses: number;
  net_savings: number;
  salary_income: number;
  investment_income: number;
  credit_card_expenses: number;
  bank_expenses: number;
  expense_category_breakdown?: Record<string, number>; // category key -> TWD amount
}

export interface Account {
  id: number;
  code: string;
  name: string;
  type: string;
  institution: string;
  currency: string;
  is_internal: boolean;
  is_installment?: boolean;
  installment_amount?: number;
  installment_start_date?: string | null;
  installment_count?: number | null;
  installment_payment_day?: number | null;
}

// ── Transactions ─────────────────────────────────────────────────────────────

export interface TransactionRecord {
  id: number;
  date: string;
  source: string;
  merchant: string;
  description: string;
  amount: number;
  category: string;
  is_refund: boolean;
  raw_category: string | null;
  institution?: string;
  account_id?: number | null;
  is_duplicate?: boolean;
}

export async function getTransactions(year: number, month?: number) {
  const data = await fetchWithCache("/transactions/", { year, month });
  console.log("getTransactions data:", typeof data, data);
  return data.transactions as TransactionRecord[];
}

export async function getStockTransactions(year: number, month?: number) {
  const data = await fetchWithCache("/transactions/stocks", { year, month });
  return data.transactions as TransactionRecord[];
}

// ── Securities ───────────────────────────────────────────────────────────────

export interface SecurityRecord {
  id?: number;
  account_id: number;
  account_name?: string;
  period_date: string;
  ticker: string;
  name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  original_avg_cost?: number;
  original_current_price?: number;
  original_market_value?: number;
  original_unrealized_pnl?: number;
  currency?: string;
  exchange_rate?: number;
  created_at?: string;
}

export async function getSecuritiesForPeriod(year: number, month: number, dateStr?: string) {
  const params: any = { year, month };
  if (dateStr) {
    params.date = dateStr;
  }
  const data = await fetchWithCache("/accounts/securities", params);
  return data as SecurityRecord[];
}

export async function getSecurityUpdateDates(year: number, month: number): Promise<string[]> {
  const data = await api.get("/accounts/securities/dates", { params: { year, month } }).then(res => res.data);
  return data as string[];
}

export async function saveSecuritiesForAccount(
  accountId: number,
  periodDate: string,
  securities: Array<{
    ticker: string;
    name?: string;
    quantity: number;
    avg_cost?: number;
    current_price?: number;
  }>
) {
  const { data } = await api.post(`/accounts/${accountId}/securities`, {
    securities
  }, {
    params: { period_date: periodDate }
  });
  return data;
}

export interface StockTransactionsSummaryItem {
  period: string;
  month_label: string;
  buys: number;
  sells: number;
  net: number;
  count: number;
}

export async function getStockTransactionsSummary(months: number = 6) {
  const data = await fetchWithCache("/transactions/stocks/summary", { months });
  return data.summary as StockTransactionsSummaryItem[];
}

export async function getSecuritiesHistory() {
  const data = await fetchWithCache("/accounts/securities/history");
  return data as SecurityRecord[];
}

export async function parseStatement(
  file: File | File[],
  kind: StatementKind,
  accountCode?: string,
  password?: string
) {
  const form = new FormData();
  // Several screenshots of one statement are sent together and stitched in order by the backend
  for (const f of Array.isArray(file) ? file : [file]) form.append("file", f);
  form.append("kind", kind);
  if (accountCode) form.append("account_code", accountCode);
  if (password) form.append("password", password);
  const { data } = await api.post("/upload/parse", form);
  return data;
}

export async function confirmStatement(payload: any) {
  const { data } = await api.post("/upload/confirm", payload);
  return data;
}

export async function updateTransaction(
  txnId: number,
  payload: {
    date?: string;
    merchant?: string;
    description?: string;
    amount?: number;
    category?: string;
    account_id?: number;
  }
) {
  const { data } = await api.put(`/transactions/${txnId}`, payload);
  return data;
}

export async function deleteTransaction(txnId: number) {
  const { data } = await api.delete(`/transactions/${txnId}`);
  return data;
}

export async function createTransaction(payload: {
  date: string;
  description: string;
  amount: number;
  category: string;
  source: string;
  merchant?: string;
  account_id?: number | null;
}) {
  const { data } = await api.post("/transactions/", payload);
  return data;
}

// ── Account Settings / Management ──────────────────────────────────────────

export async function updateAccount(
  accountId: number,
  payload: {
    name?: string;
    account_type?: string;
    institution?: string;
    currency?: string;
    is_internal?: boolean;
    code?: string;
    notes?: string;
    is_installment?: boolean;
    installment_amount?: number;
    installment_start_date?: string | null;
    installment_count?: number;
    installment_payment_day?: number;
  }
) {
  const { data } = await api.put(`/accounts/${accountId}`, payload);
  return data;
}

export async function deleteAccount(accountId: number) {
  const { data } = await api.delete(`/accounts/${accountId}`);
  return data;
}

// ── App Settings ───────────────────────────────────────────────────────────

export interface CredentialsSettings {
  gemini_api_key: string;
  gemini_model: string;
  esun_account: string;
  esun_api_key: string;
  has_esun_password?: boolean;
  has_esun_cert_password?: boolean;
  taishin_account_id: string;
  taishin_api_key: string;
  has_taishin_cert_password?: boolean;
  sinopac_account_id: string;
  sinopac_api_key: string;
  has_sinopac_cert_password?: boolean;
  cert_statuses: {
    taishin: boolean;
    sinopac: boolean;
    esun: boolean;
  };
}

export async function getSettings() {
  const data = await fetchWithCache("/settings/");
  return data as CredentialsSettings;
}

export async function saveSettings(payload: {
  gemini_api_key?: string;
  esun_account?: string;
  esun_password?: string;
  esun_cert_password?: string;
  esun_api_key?: string;
  esun_api_secret?: string;
  taishin_account_id?: string;
  taishin_api_key?: string;
  taishin_api_secret?: string;
  taishin_cert_password?: string;
  sinopac_account_id?: string;
  sinopac_api_key?: string;
  sinopac_api_secret?: string;
  sinopac_cert_password?: string;
}) {
  const { data } = await api.post("/settings/credentials", payload);
  return data;
}

export async function uploadCertificate(file: File, broker: "taishin" | "sinopac" | "esun") {
  const form = new FormData();
  form.append("file", file);
  form.append("broker", broker);
  const { data } = await api.post("/settings/upload-cert", form);
  return data;
}

export async function testConnection(broker: "taishin" | "sinopac" | "esun" | "gemini") {
  const { data } = await api.post("/settings/test-connection", { broker });
  return data;
}

export async function login(email: string, password: string) {
  const { data } = await api.post("/auth/login", { email, password });
  if (data.token) {
    localStorage.setItem("pocketcfo_token", data.token);
    localStorage.setItem("pocketcfo_login_time", String(Date.now()));
    if (data.user) {
      localStorage.setItem("pocketcfo_user", JSON.stringify(data.user));
    }
  }
  return data;
}

export async function inviteFriend(email: string) {
  const { data } = await api.post("/auth/invite", { email });
  return data;
}

export async function registerUser(email: string, password: string, pinCode: string) {
  const { data } = await api.post("/auth/register", {
    email,
    password,
    pin_code: pinCode,
  });
  return data;
}

export async function updateProfile(email?: string, password?: string) {
  const { data } = await api.put("/auth/profile", { email, password });
  if (data.user) {
    localStorage.setItem("pocketcfo_user", JSON.stringify(data.user));
  }
  return data;
}

export async function forgotPassword(email: string) {
  const { data } = await api.post("/auth/forgot-password", { email });
  return data;
}

export async function resetPassword(email: string, pinCode: string, newPassword: string) {
  const { data } = await api.post("/auth/reset-password", {
    email,
    pin_code: pinCode,
    new_password: newPassword,
  });
  return data;
}

export async function getSchedulerStatus() {
  const { data } = await api.get("/settings/scheduler-status");
  return data;
}

export async function triggerSchedulerSync() {
  const { data } = await api.post("/settings/scheduler-sync");
  return data;
}

export async function bulkDeleteTransactions(ids: number[]) {
  const { data } = await api.post("/transactions/bulk-delete", { ids });
  return data;
}

export async function bulkUpdateTransactionCategories(ids: number[], category: string) {
  const { data } = await api.post("/transactions/bulk-update-category", { ids, category });
  return data;
}

// ── Category Rules API ────────────────────────────────────────────────────────

export interface CategoryRuleItem {
  id: number;
  keyword: string;
  category: string;
  category_label: string;
  created_at?: string;
}

export async function listCategoryRules() {
  const { data } = await api.get("/category-rules/");
  return data as { rules: CategoryRuleItem[]; categories: { key: string; label: string }[] };
}

export async function createCategoryRule(keyword: string, category: string) {
  const { data } = await api.post("/category-rules/", { keyword, category });
  return data;
}

export async function updateCategoryRule(ruleId: number, keyword?: string, category?: string) {
  const { data } = await api.put(`/category-rules/${ruleId}`, { keyword, category });
  return data;
}

export async function deleteCategoryRule(ruleId: number) {
  const { data } = await api.delete(`/category-rules/${ruleId}`);
  return data;
}

export async function seedDefaultCategoryRules() {
  const { data } = await api.post("/category-rules/seed-defaults");
  return data;
}

// ── Savings Pots & Daily Tip API ────────────────────────────────────────────────

export interface SavingsPot {
  id: number;
  name: string;
  target_amount: number;
  allocated_amount: number;
  created_at?: string;
}

export async function getSavingsPots() {
  const { data } = await api.get("/savings-pots/");
  return data as { pots: SavingsPot[]; total_cash: number; latest_period?: string | null; missing_accounts?: string[] };
}

export async function createSavingsPot(name: string, targetAmount: number, allocatedAmount: number = 0) {
  const { data } = await api.post("/savings-pots/", {
    name,
    target_amount: targetAmount,
    allocated_amount: allocatedAmount,
  });
  return data;
}

export async function updateSavingsPot(
  potId: number,
  name?: string,
  targetAmount?: number,
  allocatedAmount?: number
) {
  const { data } = await api.put(`/savings-pots/${potId}`, {
    name,
    target_amount: targetAmount,
    allocated_amount: allocatedAmount,
  });
  return data;
}

export async function deleteSavingsPot(potId: number) {
  const { data } = await api.delete(`/savings-pots/${potId}`);
  return data;
}

export async function getDailyTip() {
  const { data } = await api.get("/settings/daily-tip");
  return data as { status: string; date: string; tip: string };
}

// ── AI Assistant & Developer SQL endpoints ─────────────────────────────────────

export interface ChatMessage {
  role: "user" | "model";
  content: string;
  timestamp?: number;
}

export async function sendAIChat(message: string, history: ChatMessage[]) {
  const { data } = await api.post("/ai/chat", {
    message,
    history,
  });
  return data as { response: string };
}

export interface PendingAction {
  type: "pending_action";
  action: string;
  args: Record<string, any>;
}

export async function sendAIChatStream(
  message: string,
  history: ChatMessage[],
  onChunk: (chunk: string) => void,
  model?: string,
  onPendingAction?: (action: PendingAction) => void
) {
  const token = localStorage.getItem("pocketcfo_token") || localStorage.getItem("token");
  const response = await fetch("/api/v1/ai/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ message, history, model }),
  });

  if (!response.ok) {
    let errDetail = `Server error ${response.status}`;
    try {
      const errJson = await response.json();
      errDetail = errJson.detail || errDetail;
    } catch (_) {
      const errText = await response.text();
      errDetail = errText || errDetail;
    }
    throw new Error(errDetail);
  }

  if (!response.body) {
    throw new Error("No response body received for streaming.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const dataStr = trimmed.replace(/^data:\s*/, "");
      if (dataStr === "[DONE]") {
        return;
      }
      try {
        const parsed = JSON.parse(dataStr);
        if (parsed.error) {
          throw new Error(parsed.error);
        }
        if (parsed.type === "pending_action") {
          onPendingAction?.(parsed as PendingAction);
        } else if (parsed.text) {
          onChunk(parsed.text);
        }
      } catch (e) {
        // Skip JSON parse error if partial chunk
      }
    }
  }
}

export interface SQLResult {
  columns: string[];
  rows: (string | null)[][];
}

export async function executeSQLQuery(query: string) {
  const { data } = await api.post("/ai/sql-query", {
    query,
  });
  return data as SQLResult;
}

export async function confirmAIAction(action: string, args: Record<string, any>) {
  const { data } = await api.post("/ai/chat/confirm-action", { action, args });
  return data as { action: string; result: any };
}

// ── Rebalance Strategy API ──────────────────────────────────────────────────

export interface RebalanceItem {
  category: "STOCK" | "BOND" | "CASH";
  ticker: string;
  name: string;
  quantity: number;
  current_price: number;
  current_market_value: number;
  actual_pct: number;
  target_pct: number;
  trade_amount: number;
  trade_shares: number;
  post_rebalance_shares: number;
  post_rebalance_market_value: number;
  post_rebalance_pct: number;
  leverage_multiplier: number;
}

export interface RebalanceAnalysis {
  period_date: string;
  total_portfolio_value: number;
  stock_market_value: number;
  bond_market_value: number;
  cash_market_value: number;
  current_stock_pct: number;
  current_bond_pct: number;
  current_cash_pct: number;
  target_stock_pct: number;
  target_bond_pct: number;
  target_cash_pct: number;
  stock_trigger_threshold: number;
  stock_min_threshold: number;
  assumed_rise_pct: number;
  bond_tickers: string;
  leveraged_tickers: string;
  leveraged_exposure_value: number;
  exposure_ratio: number;
  custom_cash_amount?: number | null;
  is_custom_cash?: boolean;
  enable_email_alert: boolean;
  is_triggered: boolean;
  trigger_direction: "RISE" | "FALL" | "NONE";
  rebalance_items: RebalanceItem[];
}

export async function getRebalanceAnalysis(date?: string) {
  const { data } = await api.get("/rebalance/", { params: { date } });
  return data as RebalanceAnalysis;
}

export interface RebalanceSettings {
  target_stock_pct: number;
  target_bond_pct: number;
  target_cash_pct: number;
  stock_trigger_threshold: number;
  stock_min_threshold: number;
  assumed_rise_pct: number;
  bond_tickers: string;
  leveraged_tickers: string;
  custom_cash_amount: number | null;
  enable_email_alert: boolean;
}

export async function getRebalanceSettings() {
  const { data } = await api.get("/rebalance/settings");
  return data as RebalanceSettings;
}

export async function updateRebalanceSettings(payload: {
  target_stock_pct?: number;
  target_bond_pct?: number;
  target_cash_pct?: number;
  assumed_rise_pct?: number;
  bond_tickers?: string;
  leveraged_tickers?: string;
  custom_cash_amount?: number | null;
  enable_email_alert?: boolean;
}) {
  const { data } = await api.put("/rebalance/settings", payload);
  return data;
}

export async function sendRebalanceAlertEmail(date?: string) {
  const { data } = await api.post("/rebalance/send-alert", null, { params: { date } });
  return data as { status: string; sent_to: string };
}

export interface PriceAlert {
  id: number;
  ticker: string;
  name: string | null;
  alert_type: "auto_trade" | "notify_price" | "notify_ma20";
  side: "buy" | "sell" | null;
  direction: "above" | "below" | null;
  broker: "esun" | "taishin" | "sinopac" | null;
  target_price: number;
  currency: string;
  quantity: number | null;
  status: "active" | "filled" | "failed" | "cancelled";
  order_result: string | null;
  triggered_at: string | null;
  created_at: string;
}

export const PRICE_ALERT_CURRENCIES = ["TWD", "USD", "EUR", "JPY", "GBP", "AUD", "CAD", "HKD", "SGD", "CNY", "CHF"] as const;

export async function listPriceAlerts() {
  const { data } = await api.get("/price-alerts/");
  return data as { alerts: PriceAlert[] };
}

export async function createPriceAlert(payload: {
  ticker: string;
  name?: string;
  alert_type?: "auto_trade" | "notify_price" | "notify_ma20";
  side?: "buy" | "sell";
  quantity?: number;
  broker?: "esun" | "taishin" | "sinopac";
  direction?: "above" | "below";
  target_price: number;
  currency?: string;
}) {
  const { data } = await api.post("/price-alerts/", payload);
  return data as PriceAlert;
}

export async function updatePriceAlert(
  alertId: number,
  payload: {
    ticker: string;
    name?: string;
    alert_type?: "auto_trade" | "notify_price" | "notify_ma20";
    side?: "buy" | "sell";
    quantity?: number;
    broker?: "esun" | "taishin" | "sinopac";
    direction?: "above" | "below";
    target_price: number;
    currency?: string;
  }
) {
  const { data } = await api.put(`/price-alerts/${alertId}`, payload);
  return data as PriceAlert;
}

export async function cancelPriceAlert(alertId: number) {
  const { data } = await api.delete(`/price-alerts/${alertId}`);
  return data as PriceAlert;
}

export async function reactivatePriceAlert(alertId: number) {
  const { data } = await api.post(`/price-alerts/${alertId}/reactivate`);
  return data as PriceAlert;
}

export async function deletePriceAlert(alertId: number) {
  await api.delete(`/price-alerts/${alertId}/permanent`);
}


