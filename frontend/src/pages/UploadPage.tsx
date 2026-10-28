import { useEffect, useState } from "react";
import {
  parseStatement,
  confirmStatement,
  StatementKind,
  getUploadHistory,
  deleteUploadHistory,
  UploadHistoryRecord,
} from "../services/api";
import { toast } from "../store/useToastStore";
import { formatUtc8 } from "../utils/formatters";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faFileLines, faTriangleExclamation, faCircleCheck } from "@fortawesome/free-solid-svg-icons";

const KINDS = [
  { id: "bank", num: 1, title: "銀行對帳單", sub: "請上傳您的銀行對帳單", ext: "PDF 或 圖片檔案" },
  { id: "credit_card", num: 2, title: "信用卡對帳單", sub: "請上傳您的信用卡帳單", ext: "PDF 或 圖片檔案" },
  { id: "brokerage", num: 3, title: "證券對帳單", sub: "請上傳您的證券對帳單", ext: "PDF 或 圖片檔案" },
  { id: "einvoice", num: 4, title: "電子發票載具清單", sub: "請上傳您的電子發票清單", ext: "PDF、CSV 或 圖片檔案" },
];

const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"];

function isImageFile(f: File): boolean {
  return IMAGE_EXTENSIONS.some((ext) => f.name.toLowerCase().endsWith(ext));
}

export default function UploadPage() {
  const [files, setFiles] = useState<Record<string, File[]>>({
    bank: [],
    credit_card: [],
    brokerage: [],
    einvoice: [],
  });
  const [password, setPassword] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [parseStep, setParseStep] = useState<number>(0);

  // States for the two-step verification flow
  const [editData, setEditData] = useState<any | null>(null);
  const [activeAccountTab, setActiveAccountTab] = useState(0);

  const [history, setHistory] = useState<UploadHistoryRecord[]>([]);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);

  const fetchHistory = async () => {
    setIsHistoryLoading(true);
    try {
      const data = await getUploadHistory();
      setHistory(data);
    } catch (e) {
      console.error(e);
    } finally {
      setIsHistoryLoading(false);
    }
  };

  useEffect(() => {
    fetchHistory();
  }, []);

  const handleDeleteHistory = async (id: number) => {
    if (!window.confirm("確定要刪除這筆上傳紀錄嗎？（注意：這只會刪除紀錄解除重複檔案鎖定，不會刪除已解析出的交易明細喔！）")) {
      return;
    }
    try {
      await deleteUploadHistory(id);
      toast.success("紀錄刪除成功！");
      fetchHistory();
    } catch (e) {
      console.error(e);
      toast.error("刪除失敗");
    }
  };

  const updateAccountField = (tabIdx: number, field: string, value: any) => {
    setEditData((prev: any) => {
      const copy = { ...prev };
      copy.accounts = [...copy.accounts];
      copy.accounts[tabIdx] = { ...copy.accounts[tabIdx], [field]: value };
      return copy;
    });
  };


  const handleFileDrop = (e: React.DragEvent, kindId: string) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      setFiles({ ...files, [kindId]: [...files[kindId], ...Array.from(e.dataTransfer.files)] });
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>, kindId: string) => {
    if (e.target.files && e.target.files.length > 0) {
      setFiles({ ...files, [kindId]: [...files[kindId], ...Array.from(e.target.files)] });
    }
    e.target.value = ""; // allow re-selecting the same file after removing it
  };

  const handleFileRemove = (kindId: string, index: number) => {
    setFiles({ ...files, [kindId]: files[kindId].filter((_, i) => i !== index) });
  };

  const uploadedCount = Object.values(files).reduce((acc, curr) => acc + curr.length, 0);

  // STEP 1: Upload and Parse Statement
  const handleParse = async () => {
    setIsProcessing(true);
    setEditData(null);
    setParseStep(1); // Step 1: Uploading

    const t1 = setTimeout(() => setParseStep(2), 600);   // Step 2 after 600ms
    const t2 = setTimeout(() => setParseStep(3), 1500);  // Step 3 after 1500ms

    try {
      let activeKind: StatementKind | null = null;
      let activeFiles: File[] = [];

      // Use the first statement type that has files selected
      for (const [kind, fileList] of Object.entries(files)) {
        if (fileList.length > 0) {
          activeKind = kind as StatementKind;
          activeFiles = fileList;
          break;
        }
      }

      if (activeFiles.length === 0 || !activeKind) {
        throw new Error("請先選擇對帳單檔案");
      }
      if (activeFiles.length > 1 && !activeFiles.every(isImageFile)) {
        throw new Error("一次選多個檔案時僅支援圖片（同一份對帳單的截圖）；PDF 請一次上傳一份");
      }

      const res = await parseStatement(activeFiles, activeKind, undefined, password);

      clearTimeout(t1);
      clearTimeout(t2);
      setParseStep(4); // Step 4: Mapping structures

      // Normalise response into editable state
      const rawData = res.parsed_data || {};
      const kind = rawData.kind || activeKind;
      
      let accountsList = null;
      if (kind === "bank") {
        if (rawData.accounts) {
          accountsList = rawData.accounts.map((acc: any) => ({
            account_number: acc.account_number || "",
            currency: acc.currency || "TWD",
            exchange_rate: acc.exchange_rate || 1.0,
            closing_balance: acc.closing_balance != null ? acc.closing_balance : 0,
            transactions: (acc.transactions || []).map((t: any) => ({
              date: t.date || "",
              description: t.description || t.merchant || "",
              merchant: t.merchant || t.description || "",
              amount: t.amount != null ? t.amount : (parseFloat(t.credit || 0) - parseFloat(t.debit || 0)),
              category: t.category || "其他",
              is_refund: !!t.is_refund,
              payment_method: t.payment_method || "",
              invoice_number: t.invoice_number || "",
              action: t.action || "",
              ticker: t.ticker || "",
              quantity: t.quantity != null ? t.quantity : 0,
              price: t.price != null ? t.price : 0,
              fee: t.fee != null ? t.fee : 0
            }))
          }));
        } else {
          accountsList = [{
            account_number: rawData.account_number || "",
            currency: rawData.currency || "TWD",
            exchange_rate: rawData.exchange_rate || 1.0,
            closing_balance: rawData.closing_balance != null ? rawData.closing_balance : 0,
            transactions: (rawData.transactions || []).map((t: any) => ({
              date: t.date || "",
              description: t.description || t.merchant || "",
              merchant: t.merchant || t.description || "",
              amount: t.amount != null ? t.amount : (parseFloat(t.credit || 0) - parseFloat(t.debit || 0)),
              category: t.category || "其他",
              is_refund: !!t.is_refund,
              payment_method: t.payment_method || "",
              invoice_number: t.invoice_number || "",
              action: t.action || "",
              ticker: t.ticker || "",
              quantity: t.quantity != null ? t.quantity : 0,
              price: t.price != null ? t.price : 0,
              fee: t.fee != null ? t.fee : 0
            }))
          }];
        }
      }

      setActiveAccountTab(0);
      
      // Short delay for fluid UI rendering transitions
      await new Promise(resolve => setTimeout(resolve, 300));

      setEditData({
        kind,
        filename: res.filename,
        file_hash: res.file_hash,
        period_year: rawData.period_year || new Date().getFullYear(),
        period_month: rawData.period_month || (new Date().getMonth() + 1),
        institution: rawData.institution || "",
        currency: rawData.currency || "TWD",
        exchange_rate: rawData.exchange_rate || 1.0,
        account_code: rawData.account_code || "",
        account_number: rawData.account_number || "",
        card_last_four: rawData.card_last_four || "",
        closing_balance: rawData.closing_balance != null ? rawData.closing_balance : 0,
        total_amount: rawData.total_amount != null ? rawData.total_amount : 0,
        payment_due_date: rawData.payment_due_date || "",
        cash_balance: rawData.cash_balance != null ? rawData.cash_balance : 0,
        total_market_value: rawData.total_market_value != null ? rawData.total_market_value : 0,
        holdings: (rawData.holdings || []).map((h: any) => ({
          ticker: h.ticker || "",
          name: h.name || "",
          quantity: h.quantity != null ? h.quantity : 0,
          avg_cost: h.avg_cost != null ? h.avg_cost : 0,
          current_price: h.current_price != null ? h.current_price : 0,
        })),
        transactions: (rawData.transactions || rawData.items || []).map((t: any) => {
          let amt = t.amount != null ? t.amount : (parseFloat(t.credit || 0) - parseFloat(t.debit || 0));
          if (kind === "credit_card" || kind === "einvoice") {
            const isRefund = !!t.is_refund;
            amt = isRefund ? Math.abs(amt) : -Math.abs(amt);
          }
          return {
            date: t.date || "",
            description: t.description || t.merchant || "",
            merchant: t.merchant || t.description || "",
            amount: amt,
            category: t.category || "",
            is_refund: !!t.is_refund,
            payment_method: t.payment_method || "",
            invoice_number: t.invoice_number || "",
            action: t.action || "",
            ticker: t.ticker || "",
            quantity: t.quantity != null ? t.quantity : 0,
            price: t.price != null ? t.price : 0,
            fee: t.fee != null ? t.fee : 0,
            is_duplicate: !!t.is_duplicate
          };
        }),
        accounts: accountsList
      });

      setParseStep(5); // Complete
      toast.success("對帳單解析成功！請在右側核對並調整資料。");
    } catch (e: any) {
      clearTimeout(t1);
      clearTimeout(t2);
      setParseStep(0);
      console.error(e);
      const errorMsg = e.response?.data?.detail || e.message || "發生未知錯誤。";
      toast.error(`解析錯誤：${errorMsg}`);
    } finally {
      setIsProcessing(false);
    }
  };

  // STEP 2: Save Confirmed Statement to DB
  const handleConfirm = async () => {
    if (!editData || isProcessing) return;
    setIsProcessing(true);

    try {
      await confirmStatement(editData);
      toast.success("資料已成功寫入資料庫並完成結算！");
      setEditData(null);
      setFiles({ bank: [], credit_card: [], brokerage: [], einvoice: [] });
      setPassword("");
      setParseStep(0);
      fetchHistory();
    } catch (e: any) {
      console.error(e);
      toast.error(`確認儲存失敗：${e.response?.data?.detail || e.message || "發生未知錯誤。"}`);
    } finally {
      setIsProcessing(false);
    }
  };

  const handleCancelReview = () => {
    setEditData(null);
    setParseStep(0);
  };

  const updateField = (field: string, value: any) => {
    setEditData((prev: any) => ({ ...prev, [field]: value }));
  };

  const updateHolding = (index: number, field: string, value: any) => {
    setEditData((prev: any) => {
      const copy = { ...prev };
      copy.holdings = [...copy.holdings];
      copy.holdings[index] = { ...copy.holdings[index], [field]: value };
      return copy;
    });
  };

  const addHoldingRow = () => {
    setEditData((prev: any) => ({
      ...prev,
      holdings: [...(prev.holdings || []), { ticker: "", name: "", quantity: 0, avg_cost: 0, current_price: 0 }]
    }));
  };

  const removeHoldingRow = (index: number) => {
    setEditData((prev: any) => ({
      ...prev,
      holdings: prev.holdings.filter((_: any, i: number) => i !== index)
    }));
  };

  const updateTransactionRow = (index: number, field: string, value: any) => {
    setEditData((prev: any) => {
      const copy = { ...prev };
      if (copy.accounts) {
        copy.accounts = [...copy.accounts];
        const accCopy = { ...copy.accounts[activeAccountTab] };
        accCopy.transactions = [...accCopy.transactions];
        accCopy.transactions[index] = { ...accCopy.transactions[index], [field]: value };
        copy.accounts[activeAccountTab] = accCopy;
      } else {
        copy.transactions = [...copy.transactions];
        copy.transactions[index] = { ...copy.transactions[index], [field]: value };
      }
      return copy;
    });
  };

  const addTransactionRow = () => {
    setEditData((prev: any) => {
      const copy = { ...prev };
      const newRow = { date: new Date().toISOString().split('T')[0], description: "", amount: 0, category: "其他" };
      if (copy.accounts) {
        copy.accounts = [...copy.accounts];
        const accCopy = { ...copy.accounts[activeAccountTab] };
        accCopy.transactions = [...(accCopy.transactions || []), newRow];
        copy.accounts[activeAccountTab] = accCopy;
      } else {
        copy.transactions = [...(copy.transactions || []), newRow];
      }
      return copy;
    });
  };

  const removeTransactionRow = (index: number) => {
    setEditData((prev: any) => {
      const copy = { ...prev };
      if (copy.accounts) {
        copy.accounts = [...copy.accounts];
        const accCopy = { ...copy.accounts[activeAccountTab] };
        accCopy.transactions = accCopy.transactions.filter((_: any, i: number) => i !== index);
        copy.accounts[activeAccountTab] = accCopy;
      } else {
        copy.transactions = copy.transactions.filter((_: any, i: number) => i !== index);
      }
      return copy;
    });
  };

  const activeAcc = editData ? (editData.accounts ? editData.accounts[activeAccountTab] : null) : null;

  return (
    <div className="max-w-[1600px] mx-auto min-h-screen bg-[#f8fafc] text-slate-800 p-6 flex gap-6 animate-in fade-in duration-500">
      
      {/* LEFT COLUMN: Upload Panel */}
      <div className="w-[420px] shrink-0 bg-white rounded-2xl shadow-sm border border-slate-200 p-6 flex flex-col h-[calc(100vh-100px)] sticky top-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="bg-blue-600 text-white px-3 py-1.5 rounded-lg text-sm font-bold shadow-md">上傳</div>
          <div>
            <h1 className="text-xl font-bold text-slate-900">上傳對帳單</h1>
            <p className="text-xs text-slate-500 mt-1">請上傳單份 PDF 對帳單，先解析並核對無誤後再匯入</p>
          </div>
        </div>

        <div className="flex justify-between text-sm font-bold text-slate-700 mb-4 px-1">
          <span>請選擇檔案上傳</span>
          <span className="text-slate-400 font-normal text-xs flex items-center gap-1">
            安全加密不外洩
          </span>
        </div>

        <div className="space-y-4 flex-1 overflow-y-auto pr-2 pb-4">
          {KINDS.map((k) => (
            <div key={k.id} className="relative border border-slate-200 rounded-xl p-4 flex gap-4 bg-white transition-all hover:border-blue-400 hover:shadow-sm">
              <div className="flex-shrink-0 flex items-center justify-center w-8 h-8 rounded-full bg-blue-600 text-white font-bold text-sm shadow-md">
                {k.num}
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <h3 className="font-bold text-slate-800">{k.title}</h3>
                </div>
                <p className="text-xs text-slate-500 mb-3">{k.sub}</p>
                <div className="inline-block px-2 py-1 bg-red-50 text-red-600 text-[10px] font-bold rounded">
                  {k.ext}
                </div>
                {files[k.id].length > 0 && (
                  <ol className="mt-2 space-y-1 text-[11px] text-slate-600">
                    {files[k.id].map((f, i) => (
                      <li key={`${f.name}-${i}`} className="flex items-center gap-1.5">
                        <span className="text-slate-400 w-4 text-right">{i + 1}.</span>
                        <span className="truncate max-w-[220px]">{f.name}</span>
                        <button
                          onClick={() => handleFileRemove(k.id, i)}
                          className="text-slate-400 hover:text-rose-600 cursor-pointer"
                          aria-label={`移除 ${f.name}`}
                        >
                          ✕
                        </button>
                      </li>
                    ))}
                  </ol>
                )}
                {files[k.id].length > 1 && (
                  <p className="mt-1 text-[10px] text-slate-400">多張截圖會依上面的順序接成一份對帳單解析</p>
                )}
              </div>
              <div 
                className={`w-[140px] border border-dashed rounded-lg flex flex-col items-center justify-center text-xs p-2 transition-colors cursor-pointer ${files[k.id].length > 0 ? 'border-blue-500 bg-blue-50' : 'border-slate-300 hover:bg-slate-50'}`}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => handleFileDrop(e, k.id)}
                onClick={() => document.getElementById(`file-${k.id}`)?.click()}
              >
                <span className="text-slate-600 text-center font-medium leading-tight">
                  {files[k.id].length > 0 ? <span className="text-blue-700 font-bold">已選 {files[k.id].length} 檔</span> : '拖曳檔案或'}
                </span>
                {files[k.id].length === 0 && <button className="mt-1 bg-blue-600 text-white px-3 py-1 rounded text-[10px] shadow-sm">瀏覽</button>}
                <input 
                  type="file" 
                  id={`file-${k.id}`} 
                  className="hidden" 
                  accept={
                    k.id === "einvoice" 
                      ? ".pdf,.csv,.png,.jpg,.jpeg,.webp,.heic,.heif" 
                      : ".pdf,.png,.jpg,.jpeg,.webp,.heic,.heif"
                  } 
                  multiple
                  onChange={(e) => handleFileSelect(e, k.id)} 
                />
              </div>
            </div>
          ))}
        </div>

        <div className="mt-4 pt-4 border-t border-slate-100 flex flex-col gap-4">
          <div className="flex items-center gap-3 bg-slate-50 p-3 rounded-lg border border-slate-200">
            <div className="flex-1">
              <div className="text-xs font-bold text-slate-700 mb-1">PDF 密碼解鎖 (若無免填)</div>
              <input 
                type="password" 
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="請輸入解鎖密碼" 
                className="w-full bg-white border border-slate-300 rounded px-3 py-1.5 text-sm outline-none focus:border-blue-500 transition-colors"
              />
            </div>
          </div>

          <button 
            onClick={handleParse}
            disabled={uploadedCount === 0 || isProcessing}
            className={`w-full py-4 rounded-xl font-bold text-lg text-white shadow-lg transition-all flex items-center justify-center gap-2 ${uploadedCount > 0 && !isProcessing ? 'bg-gradient-to-r from-blue-600 to-indigo-600 hover:scale-[1.02] hover:shadow-xl' : 'bg-slate-300 cursor-not-allowed'}`}
          >
            {isProcessing ? '處理中請稍候...' : '開始 AI 解析檔案'}
          </button>
        </div>
        {isProcessing && parseStep > 0 && (
          <div className="mt-4 p-4 bg-slate-50 border border-slate-200 rounded-xl space-y-3 animate-in fade-in duration-300">
            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">AI 解析進度</div>
            <div className="space-y-2.5">
              {[
                { step: 1, label: "傳送檔案至伺服器" },
                { step: 2, label: "讀取內容與解密檢查" },
                { step: 3, label: "Gemini AI 智慧識別與結構化" },
                { step: 4, label: "資料欄位整合與對帳單比對" },
              ].map((s) => {
                const isActive = parseStep === s.step;
                const isCompleted = parseStep > s.step;
                return (
                  <div key={s.step} className="flex items-center gap-3 text-xs font-semibold">
                    <div className="shrink-0">
                      {isCompleted ? (
                        <div className="w-5 h-5 rounded-full bg-emerald-500 text-white flex items-center justify-center">
                          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                          </svg>
                        </div>
                      ) : isActive ? (
                        <div className="w-5 h-5 rounded-full bg-blue-600 text-white flex items-center justify-center animate-pulse">
                          <span className="w-1.5 h-1.5 rounded-full bg-white" />
                        </div>
                      ) : (
                        <div className="w-5 h-5 rounded-full border-2 border-slate-300 text-slate-400 flex items-center justify-center bg-white">
                          {s.step}
                        </div>
                      )}
                    </div>
                    <span className={isCompleted ? "text-slate-400 line-through" : isActive ? "text-blue-600 font-bold" : "text-slate-500"}>
                      {s.label}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* RIGHT COLUMN: Review & Confirm Dashboard */}
      <div className="flex-1 flex flex-col h-[calc(100vh-100px)] overflow-y-auto">
        {!editData ? (
          <div className="flex flex-col gap-6 h-full">
            {/* Instruction Card */}
            <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-8 flex flex-col items-center justify-center text-center">
              <div className="w-12 h-12 bg-blue-50 text-blue-600 rounded-xl flex items-center justify-center text-xl font-bold shadow-inner mb-4">
                <FontAwesomeIcon icon={faFileLines} />
              </div>
              <h2 className="text-base font-bold text-slate-800 mb-2">等待開始解析</h2>
              <p className="text-xs text-slate-500 max-w-md leading-relaxed">
                請在左側選單選擇並上傳您的銀行對帳單、信用卡單、證券庫存或發票明細，接著點擊「開始 AI 解析檔案」。系統將利用 Gemini AI 智慧讀取並為您在此生成對帳確認列表。
              </p>
            </div>

            {/* Recent Upload History Table */}
            <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 flex-1 flex flex-col overflow-hidden">
              <div className="flex justify-between items-center mb-4">
                <h3 className="font-bold text-slate-800 text-sm">最近上傳紀錄 (最近50筆)</h3>
                <span className="text-[10px] text-slate-400 font-semibold">
                  注意：刪除上傳紀錄僅解除重複檔案上傳限制，不會刪除已產生的收支明細
                </span>
              </div>
              
              <div className="flex-1 overflow-y-auto border border-slate-100 rounded-xl">
                {isHistoryLoading ? (
                  <div className="py-20 text-center text-slate-400 text-xs font-bold animate-pulse">載入中...</div>
                ) : history.length === 0 ? (
                  <div className="py-20 text-center text-slate-300 text-xs font-medium">目前尚無任何對帳單上傳歷史紀錄</div>
                ) : (
                  <table className="w-full text-left text-xs">
                    <thead className="bg-slate-50 text-slate-600 font-bold border-b border-slate-200 sticky top-0 z-10">
                      <tr>
                        <th className="px-4 py-3">上傳時間</th>
                        <th className="px-4 py-3">檔案名稱</th>
                        <th className="px-4 py-3">類型</th>
                        <th className="px-4 py-3">狀態</th>
                        <th className="px-4 py-3 text-right">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {history.map((record) => {
                        const getKindLabel = (kind: string) => {
                          switch (kind) {
                            case "bank": return "銀行對帳單";
                            case "credit_card": return "信用卡帳單";
                            case "brokerage": return "證券對帳單";
                            case "einvoice": return "發票載具";
                            default: return kind;
                          }
                        };
                        return (
                          <tr key={record.id} className="hover:bg-slate-50 transition-colors">
                            <td className="px-4 py-3 whitespace-nowrap text-slate-500 font-mono">
                              {formatUtc8(record.created_at)}
                            </td>
                            <td className="px-4 py-3 font-semibold text-slate-700 max-w-[200px] truncate" title={record.filename}>
                              {record.filename}
                            </td>
                            <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                              {getKindLabel(record.kind)}
                            </td>
                            <td className="px-4 py-3 whitespace-nowrap">
                              {record.status === "success" ? (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-50 text-green-700 border border-green-200/60 text-[10px] font-bold">
                                  <span className="w-1 h-1 rounded-full bg-green-500"></span>
                                  成功
                                </span>
                              ) : (
                                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-50 text-red-700 border border-red-200/60 text-[10px] font-bold" title={record.message || ""}>
                                  <span className="w-1 h-1 rounded-full bg-red-500"></span>
                                  失敗
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-3 text-right whitespace-nowrap">
                              <button
                                onClick={() => handleDeleteHistory(record.id)}
                                className="px-2 py-1 text-red-500 hover:bg-red-50 rounded font-bold text-[10px] transition-colors"
                              >
                                刪除
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 space-y-6">
            
            {/* Header */}
            <div className="flex justify-between items-center border-b border-slate-100 pb-4">
              <div>
                <h2 className="text-lg font-bold text-slate-800 flex items-center gap-2">
                  <span>步驟二：核對並確認解析資料</span>
                </h2>
                <p className="text-xs text-slate-500 mt-1">請仔細核對以下欄位是否正確，確認無誤後即可點擊最下方的「確認並寫入資料庫」</p>
              </div>
            </div>

            {/* Tab selector for multiple bank accounts */}
            {editData.accounts && editData.accounts.length > 1 && (
              <div className="flex gap-2 pb-2 overflow-x-auto border-b border-slate-100">
                {editData.accounts.map((acc: any, idx: number) => (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => setActiveAccountTab(idx)}
                    className={`px-4 py-2 rounded-xl text-sm font-bold transition-all border ${
                      activeAccountTab === idx
                        ? "bg-blue-600 border-blue-600 text-white shadow-md shadow-blue-500/25"
                        : "bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100 hover:border-slate-300"
                    }`}
                  >
                    帳戶 {idx + 1}: {acc.currency} {acc.account_number ? `(${acc.account_number})` : ""}
                  </button>
                ))}
              </div>
            )}

            {/* Basic Config Card */}
            <div className="bg-slate-50 rounded-xl p-5 border border-slate-200 space-y-4">
              <h3 className="font-bold text-slate-800 text-sm">基本資訊</h3>
              <div className="grid grid-cols-4 gap-4">
                <div>
                  <label className="block text-xs font-bold text-slate-500 mb-1">記帳期間 (年)</label>
                  <input 
                    type="number"
                    value={editData.period_year}
                    onChange={e => updateField("period_year", parseInt(e.target.value) || 0)}
                    className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-bold text-slate-500 mb-1">記帳期間 (月)</label>
                  <input 
                    type="number"
                    value={editData.period_month}
                    onChange={e => updateField("period_month", parseInt(e.target.value) || 0)}
                    className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-bold text-slate-500 mb-1">金融機構名稱</label>
                  <input 
                    type="text"
                    value={editData.institution}
                    onChange={e => updateField("institution", e.target.value)}
                    className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-bold text-slate-500 mb-1">幣別</label>
                  <input 
                    type="text"
                    value={activeAcc ? activeAcc.currency : editData.currency}
                    onChange={e => {
                      if (activeAcc) {
                        updateAccountField(activeAccountTab, "currency", e.target.value);
                      } else {
                        updateField("currency", e.target.value);
                      }
                    }}
                    className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                  />
                </div>
              </div>

              <div className="grid grid-cols-4 gap-4">
                {((activeAcc ? activeAcc.currency : editData.currency) !== "TWD") && (
                  <div>
                    <label className="block text-xs font-bold text-slate-500 mb-1">
                      {(activeAcc ? activeAcc.currency : editData.currency)}/TWD 匯率
                    </label>
                    <input 
                      type="number"
                      step="0.0001"
                      value={activeAcc ? activeAcc.exchange_rate : editData.exchange_rate}
                      onChange={e => {
                        const val = parseFloat(e.target.value) || 1.0;
                        if (activeAcc) {
                          updateAccountField(activeAccountTab, "exchange_rate", val);
                        } else {
                          updateField("exchange_rate", val);
                        }
                      }}
                      className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm font-semibold focus:outline-none focus:border-blue-500"
                    />
                  </div>
                )}
                {(editData.kind === "bank" || editData.kind === "brokerage") && (
                  <div>
                    <label className="block text-xs font-bold text-slate-500 mb-1">帳戶號碼 / 帳號 (可留空/修改)</label>
                    <input 
                      type="text"
                      value={activeAcc ? activeAcc.account_number : (editData.account_number || "")}
                      onChange={e => {
                        if (activeAcc) {
                          updateAccountField(activeAccountTab, "account_number", e.target.value);
                        } else {
                          updateField("account_number", e.target.value);
                        }
                      }}
                      placeholder="留空使用預設帳戶"
                      className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                    />
                  </div>
                )}
                {editData.kind === "bank" && (
                  <div>
                    <label className="block text-xs font-bold text-slate-500 mb-1">
                      結餘金額 ({activeAcc ? activeAcc.currency : "TWD"})
                    </label>
                    <input 
                      type="number"
                      value={activeAcc ? activeAcc.closing_balance : editData.closing_balance}
                      onChange={e => {
                        const val = parseFloat(e.target.value) || 0;
                        if (activeAcc) {
                          updateAccountField(activeAccountTab, "closing_balance", val);
                        } else {
                          updateField("closing_balance", val);
                        }
                      }}
                      className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm font-semibold focus:outline-none focus:border-blue-500"
                    />
                  </div>
                )}
                {editData.kind === "credit_card" && (
                  <>
                    <div>
                      <label className="block text-xs font-bold text-slate-500 mb-1">當期帳單總金額</label>
                      <input 
                        type="number"
                        value={editData.total_amount}
                        onChange={e => updateField("total_amount", parseFloat(e.target.value) || 0)}
                        className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm font-semibold focus:outline-none focus:border-blue-500"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-bold text-slate-500 mb-1">繳款截止日 (YYYY-MM-DD)</label>
                      <input 
                        type="text"
                        placeholder="2026-05-25"
                        value={editData.payment_due_date}
                        onChange={e => updateField("payment_due_date", e.target.value)}
                        className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-bold text-slate-500 mb-1">卡號末四碼</label>
                      <input 
                        type="text"
                        value={editData.card_last_four}
                        onChange={e => updateField("card_last_four", e.target.value)}
                        className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:border-blue-500"
                      />
                    </div>
                  </>
                )}
                {editData.kind === "brokerage" && (
                  <>
                    <div>
                      <label className="block text-xs font-bold text-slate-500 mb-1">現金餘額 (原始幣別)</label>
                      <input 
                        type="number"
                        value={editData.cash_balance}
                        onChange={e => updateField("cash_balance", parseFloat(e.target.value) || 0)}
                        className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm font-semibold focus:outline-none focus:border-blue-500"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-bold text-slate-500 mb-1">證券總市值 (原始幣別)</label>
                      <input 
                        type="number"
                        value={editData.total_market_value}
                        onChange={e => updateField("total_market_value", parseFloat(e.target.value) || 0)}
                        className="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-sm font-semibold focus:outline-none focus:border-blue-500"
                      />
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* Holdings Card (For Brokerage only) */}
            {editData.kind === "brokerage" && (
              <div className="space-y-4">
                <div className="flex justify-between items-center">
                  <h3 className="font-bold text-slate-800 text-sm">證券持股部位 (Holdings)</h3>
                  <button 
                    onClick={addHoldingRow}
                    className="px-3 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-bold rounded-lg transition-colors border border-slate-300"
                  >
                    新增持股列
                  </button>
                </div>
                
                <div className="border border-slate-200 rounded-xl overflow-hidden">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                      <tr>
                        <th className="px-3 py-2 w-20">標的代號</th>
                        <th className="px-3 py-2 w-48">標的名稱</th>
                        <th className="px-3 py-2 text-right">持有數量</th>
                        <th className="px-3 py-2 text-right">平均成本</th>
                        <th className="px-3 py-2 text-right">收盤現價</th>
                        <th className="px-3 py-2 text-center w-12">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {editData.holdings?.length === 0 ? (
                        <tr>
                          <td colSpan={6} className="text-center py-6 text-slate-400">尚無持有部位，可手動新增。</td>
                        </tr>
                      ) : (
                        editData.holdings.map((h: any, idx: number) => (
                          <tr key={idx} className="hover:bg-slate-50/50">
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                value={h.ticker}
                                onChange={e => updateHolding(idx, "ticker", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                value={h.name}
                                onChange={e => updateHolding(idx, "name", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={h.quantity}
                                onChange={e => updateHolding(idx, "quantity", parseFloat(e.target.value) || 0)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={h.avg_cost}
                                onChange={e => updateHolding(idx, "avg_cost", parseFloat(e.target.value) || 0)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={h.current_price}
                                onChange={e => updateHolding(idx, "current_price", parseFloat(e.target.value) || 0)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <button 
                                onClick={() => removeHoldingRow(idx)}
                                className="text-red-500 hover:bg-red-50 px-2 py-0.5 rounded text-xs transition-colors"
                              >
                                刪除
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Transactions Card */}
            <div className="space-y-4">
              <div className="flex justify-between items-center">
                <h3 className="font-bold text-slate-800 text-sm">交易明細 (Transactions)</h3>
                <button 
                  onClick={addTransactionRow}
                  className="px-3 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-bold rounded-lg transition-colors border border-slate-300"
                >
                  新增交易列
                </button>
              </div>

              <div className="border border-slate-200 rounded-xl overflow-x-auto">
                {editData.kind === "brokerage" ? (
                  /* Brokerage specific transaction table */
                  <table className="w-full text-left text-sm min-w-[1080px]">
                    <thead className="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                      <tr>
                        <th className="px-3 py-2 w-36">交易日期</th>
                        <th className="px-3 py-2 w-24">標的</th>
                        <th className="px-3 py-2 w-40">動作</th>
                        <th className="px-3 py-2 w-28 text-right">交易股數</th>
                        <th className="px-3 py-2 w-28 text-right">成交單價</th>
                        <th className="px-3 py-2 w-24 text-right">手續費</th>
                        <th className="px-3 py-2 w-36 text-right">收付金額</th>
                        <th className="px-3 py-2 w-48">交易說明</th>
                        <th className="px-3 py-2 text-center w-16">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {editData.transactions?.length === 0 ? (
                        <tr>
                          <td colSpan={9} className="text-center py-8 text-slate-400">尚無交易明細，可點擊右上角手動新增。</td>
                        </tr>
                      ) : (
                        editData.transactions.map((t: any, idx: number) => (
                          <tr key={idx} className="hover:bg-slate-50/50">
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                placeholder="YYYY-MM-DD"
                                value={t.date}
                                onChange={e => updateTransactionRow(idx, "date", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                value={t.ticker}
                                onChange={e => updateTransactionRow(idx, "ticker", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <select
                                value={t.action || ""}
                                onChange={e => {
                                  const act = e.target.value;
                                  updateTransactionRow(idx, "action", act);
                                  // Auto-align category
                                  if (act === "BUY" || act === "SELL") {
                                    updateTransactionRow(idx, "category", "投資");
                                  } else if (act === "DIVIDEND") {
                                    updateTransactionRow(idx, "category", "股利");
                                  } else if (act === "INTEREST") {
                                    updateTransactionRow(idx, "category", "利息");
                                  } else if (act === "TAX") {
                                    updateTransactionRow(idx, "category", "支出");
                                  }
                                }}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              >
                                <option value="">無/其他</option>
                                <option value="BUY">買進 (BUY)</option>
                                <option value="SELL">賣出 (SELL)</option>
                                <option value="DIVIDEND">配息 (DIVIDEND)</option>
                                <option value="INTEREST">利息 (INTEREST)</option>
                                <option value="TAX">稅金/支出 (TAX)</option>
                              </select>
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={t.quantity}
                                onChange={e => {
                                  const qty = parseFloat(e.target.value) || 0;
                                  updateTransactionRow(idx, "quantity", qty);
                                  // Recalculate amount if logic allows
                                  const price = t.price || 0;
                                  const fee = t.fee || 0;
                                  const factor = t.action === "BUY" ? -1 : 1;
                                  updateTransactionRow(idx, "amount", factor * (qty * price) - fee);
                                }}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={t.price}
                                onChange={e => {
                                  const prc = parseFloat(e.target.value) || 0;
                                  updateTransactionRow(idx, "price", prc);
                                  const qty = t.quantity || 0;
                                  const fee = t.fee || 0;
                                  const factor = t.action === "BUY" ? -1 : 1;
                                  updateTransactionRow(idx, "amount", factor * (qty * prc) - fee);
                                }}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={t.fee}
                                onChange={e => {
                                  const fee = parseFloat(e.target.value) || 0;
                                  updateTransactionRow(idx, "fee", fee);
                                  const qty = t.quantity || 0;
                                  const prc = t.price || 0;
                                  const factor = t.action === "BUY" ? -1 : 1;
                                  updateTransactionRow(idx, "amount", factor * (qty * prc) - fee);
                                }}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={t.amount}
                                onChange={e => updateTransactionRow(idx, "amount", parseFloat(e.target.value) || 0)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right font-semibold focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                value={t.description || ""}
                                onChange={e => {
                                  updateTransactionRow(idx, "description", e.target.value);
                                  updateTransactionRow(idx, "merchant", e.target.value);
                                }}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <button 
                                onClick={() => removeTransactionRow(idx)}
                                className="text-red-500 hover:bg-red-50 px-2 py-0.5 rounded text-xs transition-colors"
                              >
                                刪除
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                ) : (
                  <table className="w-full text-left text-sm min-w-[720px]">
                    <thead className="bg-slate-50 text-slate-600 font-bold border-b border-slate-200">
                      <tr>
                        <th className="px-3 py-2 w-36">交易日期</th>
                        <th className="px-3 py-2 w-40">交易類別</th>
                        <th className="px-3 py-2">交易摘要 / 商家</th>
                        <th className="px-3 py-2 text-right w-40">金額 (原始幣別)</th>
                        <th className="px-3 py-2 text-center w-16">操作</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {((activeAcc ? activeAcc.transactions : editData.transactions) || []).length === 0 ? (
                        <tr>
                          <td colSpan={5} className="text-center py-8 text-slate-400">尚無交易明細，可點擊右上角手動新增。</td>
                        </tr>
                      ) : (
                        (activeAcc ? activeAcc.transactions : editData.transactions).map((t: any, idx: number) => (
                          <tr key={idx} className="hover:bg-slate-50/50">
                            <td className="px-3 py-1.5">
                              <input 
                                type="text"
                                placeholder="YYYY-MM-DD"
                                value={t.date}
                                onChange={e => updateTransactionRow(idx, "date", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5">
                              <select
                                value={t.category === "其他" ? (t.amount > 0 ? "非固定收入" : "非固定支出") : (t.category || "非固定支出")}
                                onChange={e => updateTransactionRow(idx, "category", e.target.value)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                              >
                                {t.amount > 0 ? (
                                  <>
                                    <optgroup label="收入類別">
                                      {["薪資", "股利", "利息", "非固定收入"].map(cat => <option key={cat} value={cat}>{cat}</option>)}
                                    </optgroup>
                                    <optgroup label="通用類別">
                                      {["投資", "信用卡繳款", "本金償還", "轉入", "轉出", "帳內互轉"].map(cat => <option key={cat} value={cat}>{cat}</option>)}
                                    </optgroup>
                                  </>
                                ) : (
                                  <>
                                    <optgroup label="支出類別">
                                      {["固定支出", "食物", "交通", "醫療", "娛樂", "保險", "運動", "購物", "旅遊", "學習", "非固定支出"].map(cat => <option key={cat} value={cat}>{cat}</option>)}
                                    </optgroup>
                                    <optgroup label="通用類別">
                                      {["投資", "信用卡繳款", "本金償還", "轉入", "轉出", "帳內互轉"].map(cat => <option key={cat} value={cat}>{cat}</option>)}
                                    </optgroup>
                                  </>
                                )}
                              </select>
                            </td>
                            <td className="px-3 py-1.5">
                              <div className="flex flex-col gap-1">
                                <input 
                                  type="text"
                                  value={t.description || t.merchant || ""}
                                  onChange={e => {
                                    updateTransactionRow(idx, "description", e.target.value);
                                    updateTransactionRow(idx, "merchant", e.target.value);
                                  }}
                                  className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs focus:outline-none focus:border-blue-500 focus:bg-white"
                                />
                                {editData.kind === "einvoice" && (
                                  <div className="flex items-center gap-2 mt-0.5">
                                    {t.is_duplicate ? (
                                      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-50 text-amber-700 border border-amber-200/60">
                                        <FontAwesomeIcon icon={faTriangleExclamation} className="mr-1" />重複 (已在信用卡/帳單記錄)
                                      </span>
                                    ) : (
                                      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold bg-green-50 text-green-700 border border-green-200/60">
                                        <FontAwesomeIcon icon={faCircleCheck} className="mr-1" />獨特 (將寫入資料庫)
                                      </span>
                                    )}
                                    <label className="inline-flex items-center text-[10px] text-slate-500 font-semibold cursor-pointer hover:text-slate-700 select-none">
                                      <input 
                                        type="checkbox" 
                                        checked={!!t.is_duplicate} 
                                        onChange={e => updateTransactionRow(idx, "is_duplicate", e.target.checked)}
                                        className="mr-1 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                                      />
                                      標記為重複交易 (重複者不寫入資料庫)
                                    </label>
                                  </div>
                                )}
                              </div>
                            </td>
                            <td className="px-3 py-1.5">
                              <input 
                                type="number"
                                value={t.amount}
                                onChange={e => updateTransactionRow(idx, "amount", parseFloat(e.target.value) || 0)}
                                className="w-full px-2 py-1 bg-slate-50 border border-slate-200 rounded text-xs text-right font-semibold focus:outline-none focus:border-blue-500 focus:bg-white"
                              />
                            </td>
                            <td className="px-3 py-1.5 text-center">
                              <button 
                                onClick={() => removeTransactionRow(idx)}
                                className="text-red-500 hover:bg-red-50 px-2 py-0.5 rounded text-xs transition-colors"
                              >
                                刪除
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                )}
              </div>
            </div>

            {/* Bottom Actions */}
            <div className="flex justify-end gap-3 border-t border-slate-100 pt-4">
              <button
                onClick={handleCancelReview}
                disabled={isProcessing}
                className="px-5 py-2.5 border border-slate-200 text-slate-600 rounded-xl text-sm font-bold hover:bg-slate-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                取消核對
              </button>
              <button
                onClick={handleConfirm}
                disabled={isProcessing}
                className="px-6 py-2.5 bg-blue-600 text-white rounded-xl text-sm font-bold hover:bg-blue-700 shadow-md hover:shadow-lg hover:scale-[1.01] transition-all disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
              >
                {isProcessing ? '寫入中請稍候...' : '確認無誤並寫入資料庫'}
              </button>
            </div>

          </div>
        )}
      </div>

    </div>
  );
}
