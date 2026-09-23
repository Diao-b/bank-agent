# -*- coding: utf-8 -*-
"""前端改造：TransferPage 验证阶段改走 VerifyPanel；对话流 verify 卡片；演示流去短信化"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------------------------------------------------------------- TransferPage
old = '''  const [stage, setStage]   = useState('idle');       // idle|running|verify|confirm|done|error
  const [sms, setSms]       = useState(null);'''
new = '''  const [stage, setStage]   = useState('idle');       // idle|running|verify|confirm|done|error
  /* C3/B4：验证阶段信息 {factor, need_set, queue}——按类型渲染验证码/密码卡 */
  const [verifyInfo, setVerifyInfo] = useState(null);'''
assert old in s; s = s.replace(old, new, 1)

old = '''  const resetAll = () => {
    setStep(1); setStage('idle'); setNodes([]); setSms(null); setConfirmCard(null);'''
new = '''  const resetAll = () => {
    setStep(1); setStage('idle'); setNodes([]); setVerifyInfo(null); setConfirmCard(null);'''
assert old in s; s = s.replace(old, new, 1)

old = '''    setStep(4); setStage('running'); setNodes([]); setErrText(''); setReceipt(null);
    setSms(null); setConfirmCard(null); setBusy(true);'''
new = '''    setStep(4); setStage('running'); setNodes([]); setErrText(''); setReceipt(null);
    setVerifyInfo(null); setConfirmCard(null); setBusy(true);'''
assert old in s; s = s.replace(old, new, 1)

old = '''    if (p.kind === 'verify')  { setSms(p.sms || null); setCode(''); setStage('verify');  return; }'''
new = '''    if (p.kind === 'verify')  { setVerifyInfo({ factor: p.factor || p.verify, need_set: p.need_set, queue: p.queue || [] }); setStage('verify'); return; }'''
assert old in s; s = s.replace(old, new, 1)

# verify 阶段 UI：替换原短信卡 + 输码块
old = '''          {/* 短信验证 */}
          {stage === 'verify' && (
            <Panel title="短信验证" icon="smartphone">
              {sms && <div className="mb-4"><SmsCard sms={sms} /></div>}
              <div className="flex items-center gap-3">
                <input value={code} onChange={e => setCode(e.target.value.replace(/\\D/g, '').slice(0, 6))}
                       inputMode="numeric" placeholder="6 位验证码"
                       className="w-44 px-3.5 py-2.5 rounded-xl border border-slate-200 bg-white text-[18px]
                                  font-mono tracking-[0.3em] text-slate-700 outline-none
                                  focus:border-brand-400 focus:ring-2 focus:ring-brand-100 transition" />
                <button disabled={code.length < 4 || busy} onClick={() => resume(code)} className={btnPrimary}>
                  {busy ? '验证中…' : '提交验证码'}
                </button>
                <button disabled={busy} onClick={() => resume('取消')} className={btnGhost}>放弃本次转账</button>
              </div>
              <div className="mt-3 text-[11px] text-slate-400">
                验证码错误 3 次将作废本次转账，需重新发起。验证码不会写入对话记录。
              </div>
            </Panel>
          )}'''
new = '''          {/* 安全验证（C3/B4）：按 factor 渲染验证码（收件箱取码）/ 支付密码卡 */}
          {stage === 'verify' && (
            <Panel title="安全验证" icon="shield-check">
              <VerifyPanel info={verifyInfo} busy={busy}
                onCodeSubmit={(c) => resume(c)}
                onPasswordPassed={(needSet) => resume(needSet ? PWD_SET_PLACEHOLDER : PWD_RESUME_PLACEHOLDER)}
                onOpenInbox={() => { window.dispatchEvent(new CustomEvent('xb:open-inbox')); }}
                onCancel={() => resume('取消')} />
            </Panel>
          )}'''
assert old in s; s = s.replace(old, new, 1)

# willVerify 文案
old = """                {payee && !payee.is_frequent ? '收款人为非常用，' : '金额超过 ¥5,000，'}
                提交后需完成短信验证"""
new = """                {payee && !payee.is_frequent ? '收款人为非常用，' : '金额超过阈值，'}
                提交后需完成安全验证（验证码 / 支付密码按风险分级组合）"""
assert old in s; s = s.replace(old, new, 1)

# 步骤③提示文案
old = "点击「提交」不会立即扣款。小微会先做风险检查{willVerify ? '、下发短信验证码' : ''}，"
new = "点击「提交」不会立即扣款。小微会先做风险检查{willVerify ? '、下发安全验证（验证码/支付密码）' : ''}，"
assert old in s; s = s.replace(old, new, 1)

# code state 不再需要（VerifyPanel 自管）
old = """  const [code, setCode]     = useState('');
  const [busy, setBusy]     = useState(false);"""
new = """  const [busy, setBusy]     = useState(false);"""
assert old in s; s = s.replace(old, new, 1)
old = """    setReceipt(null); setErrText(''); setCode(''); setAmount(''); setRemark('');"""
new = """    setReceipt(null); setErrText(''); setAmount(''); setRemark('');"""
assert old in s; s = s.replace(old, new, 1)

# ---------------------------------------------------------------- App：对话流 verify 卡片
old = """  const [inboxFlash, setInboxFlash] = useState(false);"""
new = """  const [inboxFlash, setInboxFlash] = useState(false);
  /* B4：对话流安全验证卡（按 factor 渲染验证码 / 支付密码） */
  const [chatVerify, setChatVerify] = useState(null);"""
assert old in s; s = s.replace(old, new, 1)

old = """    } else if (data.kind === 'verify') {
      const id = nextId();
      setMessages(ms => [...ms, { id, role: 'system', text: data.reply }]);
      if (data.sms) setSms(data.sms);
    } else {"""
new = """    } else if (data.kind === 'verify') {
      const id = nextId();
      setMessages(ms => [...ms, { id, role: 'system', text: data.reply }]);
      /* B4：记录 verify 类型字段，渲染对应卡片（验证码 / 密码绝不搞混） */
      setChatVerify({ factor: data.factor || data.verify, need_set: data.need_set, queue: data.queue || [] });
    } else {
      setChatVerify(null);   // 终态：清掉挂起的验证卡"""
assert old in s; s = s.replace(old, new, 1)

# 新会话 / 切换会话清验证卡
old = """  const handleNewChat = () => {
    setMessages([]);
    setSms(null);
    setConfirmStage(null);"""
new = """  const handleNewChat = () => {
    setMessages([]);
    setSms(null);
    setChatVerify(null);
    setConfirmStage(null);"""
assert old in s; s = s.replace(old, new, 1)

old = """  const switchConversation = (tid) => {
    if (tid === threadId) return;
    setMessages([]);
    setSms(null);
    setConfirmStage(null);"""
new = """  const switchConversation = (tid) => {
    if (tid === threadId) return;
    setMessages([]);
    setSms(null);
    setChatVerify(null);
    setConfirmStage(null);"""
assert old in s; s = s.replace(old, new, 1)

# ChatPane 传参与渲染
old = """onSend={handleSend} onNewChat={handleNewChat} onRevealOverview={revealOverview}"""
new = """onSend={handleSend} onNewChat={handleNewChat} onRevealOverview={revealOverview}
                    chatVerify={chatVerify}
                    onVerifyCode={(c) => { setChatVerify(null); realSend(c); }}
                    onVerifyPassword={(needSet) => { setChatVerify(null); realSend(needSet ? PWD_SET_PLACEHOLDER : PWD_RESUME_PLACEHOLDER); }}
                    onOpenInbox={() => setInboxOpen(true)}"""
assert old in s; s = s.replace(old, new, 1)

old = """function ChatPane({ messages, input, setInput, sms, confirmStage, mode, typing,
                    balance, todayExpense, monthExpense, toolCapsules, extStatus,
                    onSend, onNewChat, onConfirm, onCancel, onRealConfirm, onRealCancel,
                    onRevealOverview,
                    scrollRef, inputRef, streamedRef, onStreamed }) {"""
new = """function ChatPane({ messages, input, setInput, sms, confirmStage, mode, typing,
                    balance, todayExpense, monthExpense, toolCapsules, extStatus,
                    onSend, onNewChat, onConfirm, onCancel, onRealConfirm, onRealCancel,
                    onRevealOverview, chatVerify, onVerifyCode, onVerifyPassword, onOpenInbox,
                    scrollRef, inputRef, streamedRef, onStreamed }) {"""
assert old in s; s = s.replace(old, new, 1)

old = """          <ChatInput input={input} setInput={setInput} onSend={onSend} typing={typing} inputRef={inputRef} />"""
new = """          {/* B4：对话内安全验证卡（挂起时显示；完成后自动消失） */}
          {chatVerify && (
            <div className="shrink-0 px-8 pb-3">
              <VerifyPanel info={chatVerify} busy={typing}
                onCodeSubmit={onVerifyCode}
                onPasswordPassed={onVerifyPassword}
                onOpenInbox={onOpenInbox} />
            </div>
          )}
          <ChatInput input={input} setInput={setInput} onSend={onSend} typing={typing} inputRef={inputRef} />"""
assert old in s; s = s.replace(old, new, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('part2 OK')
