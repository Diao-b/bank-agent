# -*- coding: utf-8 -*-
"""前端改造：AccountBanner 遮蔽 + App 收件箱/隐私接线"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------------------------------------------------------------- AccountBanner（A4 顶栏三卡）
start = s.index('/* 对话页顶部资产横幅：三张并排，总资产字号最大，分割线区隔 */')
end = s.index('ReactDOM.createRoot(document.getElementById(\'root\')).render(<App />);')
new_banner = '''/* ============================================================
   A4 对话页顶部资产横幅：初始仅脱敏，点「显示」才 reveal，10s 回遮
   —— 初始不请求任何真值（/api/state 默认脱敏）；主动查询走
      /api/overview?reveal=true（A1）。
   ============================================================ */
function AccountBanner({ balance, todayExpense, monthExpense, mode, onNewChat, onReveal }) {
  const [revealed, setRevealed] = useState(false);
  const timerRef = useRef(null);
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  const dash = v => (v == null ? '—' : `¥${fmtMoney(v)}`);
  const reveal = () => {
    setRevealed(true);
    if (onReveal) onReveal();          // 页面实现：拉真实聚合
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setRevealed(false), 10000);   // 10s 回遮
  };
  const card = (label, value, tone) => (
    <div className="flex-1 min-w-0 flex flex-col items-center justify-center text-center px-5 py-2">
      <div className="text-[13px] text-slate-500 leading-none mb-2">{label}</div>
      {revealed && value != null ? (
        <div className={`font-mono text-[22px] leading-none font-semibold amount-fade ${tone}`}>{dash(value)}</div>
      ) : revealed && value == null ? (
        <div className="font-mono text-[22px] leading-none font-semibold text-slate-300">—</div>
      ) : (
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[22px] leading-none font-semibold text-gold-600/70">{MASK_TEXT}</span>
          <button onClick={reveal}
            className="text-[11px] leading-none px-1.5 py-0.5 rounded-md text-slate-400 border border-slate-200
                       hover:text-brand-600 hover:border-brand-200 hover:bg-brand-50 transition-colors">
            显示
          </button>
        </div>
      )}
    </div>
  );

  return (
    <div className="shrink-0 bg-white/80 backdrop-blur border-b border-slate-200/70 px-6 py-4">
      <div className="flex items-stretch gap-0">
        {card('总资产', balance, 'text-gold-600')}
        <div className="w-px self-stretch bg-gradient-to-b from-transparent via-slate-200 to-transparent" />
        {card('今日支出', todayExpense, 'text-rose-600')}
        <div className="w-px self-stretch bg-gradient-to-b from-transparent via-slate-200 to-transparent" />
        {card('本月支出', monthExpense, 'text-amber-600')}

        {/* 状态 + 新会话：与卡片同行右侧 */}
        <div className="flex flex-col items-end justify-center gap-2 pl-5 ml-3 border-l border-slate-200 shrink-0">
          <span className={'inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full border ' +
            (mode === 'real'
              ? 'text-emerald-600 bg-emerald-50 border-emerald-100'
              : 'text-slate-500 bg-slate-50 border-slate-200')}>
            <span className={'w-1.5 h-1.5 rounded-full ' + (mode === 'real' ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300')} />
            {mode === 'real' ? '小微在线 · Agent' : '演示模式'}
          </span>
          <button onClick={onNewChat}
            className="px-3 py-1 rounded-full text-[12px] font-medium text-slate-500 border border-slate-200
                       hover:text-brand-600 hover:border-brand-200 hover:bg-brand-50 transition-all duration-200">
            新会话
          </button>
        </div>
      </div>
    </div>
  );
}

'''
s = s[:start] + new_banner + s[end:]

# ---------------------------------------------------------------- App 接线
old_state = '''  const [toolCapsules, setToolCapsules] = useState([]);  // [{id,name,label,status:'loading'|'done',elapsed_s}]
  const [extStatus, setExtStatus] = useState(null);      // 慢调用状态条文案（null = 隐藏）'''
new_state = '''  const [toolCapsules, setToolCapsules] = useState([]);  // [{id,name,label,status:'loading'|'done',elapsed_s}]
  const [extStatus, setExtStatus] = useState(null);      // 慢调用状态条文案（null = 隐藏）
  /* C4 收件箱：入口未读数（5s 轮询，不用 SSE —— SSE 是对话流专用）+ 抽屉开关 */
  const [unread, setUnread] = useState(0);
  const [unreadTotal, setUnreadTotal] = useState(0);     // 上次读数：用于"新消息短暂高亮"
  const [inboxOpen, setInboxOpen] = useState(false);
  const [inboxFlash, setInboxFlash] = useState(false);'''
assert old_state in s
s = s.replace(old_state, new_state, 1)

old_refresh = '''  /* 从 /api/state 同步真实余额与操作日志（银行服务离线时静默跳过） */
  const refreshState = () => fetch('/api/state').then(r => r.json()).then(s => {'''
new_refresh = '''  /* C4：未读数轮询（5s）。有新消息 → 入口文字短暂高亮 2s */
  const refreshUnread = () => fetch('/api/inbox/unread_count', { headers: authHeaders() })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) return;
      const n = Number(d.count) || 0;
      setUnread(prev => {
        if (n > prev) {
          setInboxFlash(true);
          setTimeout(() => setInboxFlash(false), 2000);
        }
        return n;
      });
      setUnreadTotal(n);
    }).catch(() => {});

  useEffect(() => {
    refreshUnread();
    const id = setInterval(refreshUnread, 5000);   // ★轮询而非 SSE：对话流专用通道不混用
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* A4：主动查询（顶栏「显示」）→ /api/overview?reveal=true 下发真实聚合；
     日常轮转仍走脱敏态 /api/state（无真值字段） */
  const revealOverview = () => fetch('/api/overview?reveal=true', { headers: authHeaders() })
    .then(r => r.json())
    .then(d => {
      if (!d.ok || d.masked) return;
      setBalance(d.total_assets);
      setTodayExpense(d.today_expense);
      setMonthExpense(d.month_expense);
    }).catch(() => {});

  /* 从 /api/state 同真实操作日志；金额字段默认脱敏（A1：初始请求无真值） */
  const refreshState = () => fetch('/api/state').then(r => r.json()).then(s => {'''
assert old_refresh in s
s = s.replace(old_refresh, new_refresh, 1)

# 新会话：隐私强制重置为"开"（A3）+ 收件箱刷新
old_newchat = '''  const handleNewChat = () => {
    setMessages([]);
    setSms(null);
    setConfirmStage(null);
    setTyping(false);'''
new_newchat = '''  const handleNewChat = () => {
    setMessages([]);
    setSms(null);
    setConfirmStage(null);
    setTyping(false);
    resetPrivacyOn();            // A3：新会话强制重置隐私模式为"开"
    setPrivacy(true);
    refreshUnread();'''
assert old_newchat in s
s = s.replace(old_newchat, new_newchat, 1)

# App 需要拿到 setPrivacy —— 用 hooks 顶部取
old_meta = '''  const meta = routeMeta(route);
  const shownName = userName || (mode === 'real' ? '用户' : PLACEHOLDER.userName);

  return (
    <BalanceCtx.Provider value={balance}>
      <div className="h-screen flex flex-col bg-canvas">
        <TopBar userName={shownName} mode={mode} title={meta.title} icon={meta.icon} />'''
new_meta = '''  const meta = routeMeta(route);
  const shownName = userName || (mode === 'real' ? '用户' : PLACEHOLDER.userName);
  const { setPrivacy } = usePrivacy();

  return (
    <BalanceCtx.Provider value={balance}>
      <div className="h-screen flex flex-col bg-canvas">
        <TopBar userName={shownName} mode={mode} title={meta.title} icon={meta.icon}
                unread={unread} inboxOpen={inboxOpen} onToggleInbox={() => setInboxOpen(v => !v)}
                inboxFlash={inboxFlash} />'''
assert old_meta in s
s = s.replace(old_meta, new_meta, 1)

# ChatPane 传 onReveal（AccountBanner 主动查询）
old_banner_use = '''          <AccountBanner balance={balance} todayExpense={todayExpense}
                         monthExpense={monthExpense} mode={mode} onNewChat={onNewChat} />'''
new_banner_use = '''          <AccountBanner balance={balance} todayExpense={todayExpense}
                         monthExpense={monthExpense} mode={mode} onNewChat={onNewChat}
                         onReveal={onRevealOverview} />'''
assert old_banner_use in s
s = s.replace(old_banner_use, new_banner_use, 1)

old_cp_props = '''onSend={handleSend} onNewChat={handleNewChat}'''
new_cp_props = '''onSend={handleSend} onNewChat={handleNewChat} onRevealOverview={revealOverview}'''
assert old_cp_props in s
s = s.replace(old_cp_props, new_cp_props, 1)

old_cp_sig = '''function ChatPane({ messages, input, setInput, sms, confirmStage, mode, typing,
                    balance, todayExpense, monthExpense, toolCapsules, extStatus,
                    onSend, onNewChat, onConfirm, onCancel, onRealConfirm, onRealCancel,
                    scrollRef, inputRef, streamedRef, onStreamed }) {'''
new_cp_sig = '''function ChatPane({ messages, input, setInput, sms, confirmStage, mode, typing,
                    balance, todayExpense, monthExpense, toolCapsules, extStatus,
                    onSend, onNewChat, onConfirm, onCancel, onRealConfirm, onRealCancel,
                    onRevealOverview,
                    scrollRef, inputRef, streamedRef, onStreamed }) {'''
assert old_cp_sig in s
s = s.replace(old_cp_sig, new_cp_sig, 1)

# 抽屉挂载 + PrivacyProvider 包裹
old_close = '''        </div>
      </div>
    </BalanceCtx.Provider>
  );
}'''
new_close = '''        </div>

        {/* C4 收件箱抽屉（右侧滑出） */}
        <InboxDrawer open={inboxOpen} onClose={() => setInboxOpen(false)} onChanged={refreshUnread} />
      </div>
    </BalanceCtx.Provider>
  );
}'''
assert old_close in s
s = s.replace(old_close, new_close, 1)

old_root = "ReactDOM.createRoot(document.getElementById('root')).render(<App />);"
new_root = "ReactDOM.createRoot(document.getElementById('root')).render(<PrivacyProvider><App /></PrivacyProvider>);"
assert old_root in s
s = s.replace(old_root, new_root, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
