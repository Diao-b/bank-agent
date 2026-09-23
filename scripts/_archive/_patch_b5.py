# -*- coding: utf-8 -*-
"""前端改造 B5/C3：移除模拟短信 UI，新增 PasswordCard / VerifyPanel，TransferPage 与对话流适配"""
import io, re

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------------------------------------------------------------- 1) 删除 SmsCard 与 Sidebar（模拟短信 UI 零残留）
i0 = s.index('/* 4) 模拟短信：手机短信样式；收到验证码时卡片橙色闪烁 */')
i1 = s.index('const LOG_TONES = [')
s = s[:i0] + '''/* 4) 原「模拟短信」UI 已按展示安全方案整体移除：
      验证码一律送达站内收件箱（C3），前端不再展示任何"短信"载体。 */
''' + s[i1:]

i0 = s.index('function Sidebar({ balance, sms, logs, mode, accounts, todayExpense, monthExpense, onPick }) {')
i1 = s.index('/* ============================================================\n   多页面通用组件层')
s = s[:i0] + '''/* 旧侧栏（含模拟短信卡）已随信息架构调整移除；顶栏资产横幅承担其职责。 */

''' + s[i1:]

# animate-sms-flash CSS 一并移除
s = s.replace("""  /* ---- 模拟短信：收到验证码时橙色边框闪烁 ---- */
  @keyframes smsFlash{ 0%,100%{ box-shadow:0 0 0 0 rgba(245,158,11,0);} 50%{ box-shadow:0 0 0 3px rgba(245,158,11,0.35);} }
  .animate-sms-flash{ animation:smsFlash 1.1s ease-in-out 3; }
""", "  /* 模拟短信闪烁样式已随「模拟短信」UI 一并移除（验证码走收件箱） */\n")

# ---------------------------------------------------------------- 2) PasswordCard + VerifyPanel（B5）
anchor = '/* ============================================================\n   轻量 Markdown → React 节点解析'
new_blocks = '''/* ============================================================
   B5 支付密码卡：6 格分体（44×52，圆点，自动跳格，满 6 自动提交）
   —— 错误 shake + "剩余N次"；锁定红卡；首次使用进入"设置"模式。
   —— ★密码禁入任何持久化记录：核验走 /api/pay/password/verify，
      成功后前端只回传固定占位符 resume（对话里只落"[已输入支付密码]"）。
   ============================================================ */
function PasswordCard({ busy, errorText, attemptsLeft, locked, needSet, onFilled, onCancel }) {
  const [digits, setDigits] = useState(['', '', '', '', '', '']);
  const [shake, setShake] = useState(false);
  const refs = useRef([]);
  useEffect(() => { if (refs.current[0]) refs.current[0].focus(); }, []);
  useEffect(() => {
    if (!errorText) return undefined;
    setShake(true);
    setDigits(['', '', '', '', '', '']);
    const t = setTimeout(() => {
      setShake(false);
      if (refs.current[0]) refs.current[0].focus();
    }, 480);
    return () => clearTimeout(t);
  }, [errorText]);

  const submit = (arr) => {
    const v = arr.join('');
    if (v.length === 6 && /^\\d{6}$/.test(v) && !busy) onFilled(v);
  };
  const setAt = (i, ch) => {
    if (!/^\\d$/.test(ch)) return;
    const next = digits.slice(); next[i] = ch; setDigits(next);
    if (i < 5 && refs.current[i + 1]) refs.current[i + 1].focus();
    submit(next);
  };
  const onKey = (i, e) => {
    if (e.key === 'Backspace') {
      e.preventDefault();
      const next = digits.slice();
      if (next[i]) { next[i] = ''; setDigits(next); }
      else if (i > 0) { next[i - 1] = ''; setDigits(next); refs.current[i - 1].focus(); }
    }
  };
  const onPaste = (e) => {
    const txt = (e.clipboardData.getData('text') || '').replace(/\\D/g, '').slice(0, 6);
    if (!txt) return;
    e.preventDefault();
    const next = ['0', '0', '0', '0', '0', '0'].map((_, i) => txt[i] || '');
    setDigits(next);
    if (txt.length === 6) submit(next);
  };

  return (
    <div className={`w-[340px] rounded-2xl p-[2px] ${shake ? 'animate-shake' : ''}
      ${locked ? 'bg-rose-500' : 'bg-slate-200'}`}
      style={{ boxShadow: locked ? '0 8px 24px rgba(244,63,94,0.25)' : '0 6px 20px rgba(59,130,246,0.16)' }}>
      <div className="rounded-[14px] bg-white px-5 py-4">
        <div className="flex items-center justify-between">
          <span className={`text-[14px] font-semibold ${locked ? 'text-rose-600' : 'text-slate-700'}`}>
            {locked ? '支付密码已锁定' : (needSet ? '设置支付密码' : '输入支付密码')}
          </span>
          <span className="text-[11px] text-slate-400">6 位数字</span>
        </div>
        <div className="mt-1 text-[11px] text-slate-400 leading-relaxed">
          {locked
            ? '连续错误 3 次，已锁定至今日 23:59:59。查询不受影响。'
            : (needSet
              ? '首次使用需设置支付密码，设置后本次操作将继续。'
              : '输入 6 位支付密码完成身份核验（你知）。密码不会保存在对话记录中。')}
        </div>
        <div className="mt-3 flex justify-between gap-2" onPaste={onPaste}>
          {[0, 1, 2, 3, 4, 5].map(i => (
            <input key={i} value={digits[i]} inputMode="numeric" maxLength={1} disabled={locked || busy}
              ref={el => { refs.current[i] = el; }}
              onChange={e => setAt(i, e.target.value)}
              onKeyDown={e => onKey(i, e)}
              className={'w-[44px] h-[52px] rounded-xl border text-center text-[20px] font-mono '
                + 'outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-100 '
                + 'disabled:bg-slate-50 disabled:cursor-not-allowed '
                + (locked ? 'border-rose-200' : 'border-slate-200')}
              style={{ textAlign: 'center' }} />
          ))}
        </div>
        <div className="mt-2 min-h-[18px] text-[12px]">
          {errorText && <span className="text-rose-500">{errorText}</span>}
          {!errorText && attemptsLeft != null && <span className="text-amber-600">剩余尝试次数：{attemptsLeft}</span>}
          {!errorText && attemptsLeft == null && !locked && <span className="text-slate-300">满 6 位自动提交</span>}
        </div>
        {onCancel && (
          <button onClick={onCancel} disabled={busy}
            className="mt-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors">
            放弃本次操作
          </button>
        )}
      </div>
    </div>
  );
}

/* ============================================================
   C3/B4 安全验证面板（TransferPage 与对话流共用）
   —— 按 factor 渲染：sms_code → 收件箱取码提示 + 输码；
      pay_password → PasswordCard；队列显示"第 x/y 步"。
   ============================================================ */
const PWD_RESUME_PLACEHOLDER = '[已输入支付密码]';
const PWD_SET_PLACEHOLDER = '[已设置支付密码]';

function VerifyPanel({ info, busy, onCodeSubmit, onPasswordPassed, onOpenInbox, onCancel }) {
  const [code, setCode] = useState('');
  const [pwdErr, setPwdErr] = useState('');
  const [attemptsLeft, setAttemptsLeft] = useState(null);
  const [locked, setLocked] = useState(false);
  const [needSet, setNeedSet] = useState(!!(info && info.need_set));
  const factor = (info && (info.factor || info.verify)) || 'sms_code';
  const queue = (info && info.queue) || [];
  const idx = Math.max(0, queue.indexOf(factor));

  const submitPassword = async (pwd) => {
    if (busy || locked) return;
    const url = needSet ? '/api/pay/password/set' : '/api/pay/password/verify';
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ password: pwd }),
      });
      const d = await res.json().catch(() => ({}));
      if (d.ok) { setPwdErr(''); setAttemptsLeft(null); onPasswordPassed(needSet); return; }
      if (d.locked) setLocked(true);
      if (d.need_set) setNeedSet(true);
      setPwdErr(d.error || '核验未通过');
      if (d.attempts_left != null) setAttemptsLeft(d.attempts_left);
    } catch (e) {
      setPwdErr('网络错误，请重试');
    }
  };

  return (
    <div className="space-y-3">
      {queue.length > 1 && (
        <div className="text-[11px] text-slate-400">
          安全验证 第 {idx + 1}/{queue.length} 步：
          {factor === 'sms_code' ? '验证码（你有）' : '支付密码（你知）'}
          {queue.length > 1 ? ' · 双要素组合' : ''}
        </div>
      )}
      {factor === 'sms_code' ? (
        <div className="rounded-2xl bg-white border border-slate-200/70 p-4" style={{ boxShadow: '0 4px 16px rgba(59,130,246,0.10)' }}>
          <div className="text-[14px] font-semibold text-slate-700">验证码已发送至您的收件箱</div>
          <div className="mt-1 text-[12px] text-slate-400 leading-relaxed">
            收件箱就是您的"手机"：打开右上角「收件箱」，查看金色验证码并输入。5 分钟内有效。
          </div>
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <button onClick={onOpenInbox}
              className="px-3.5 py-2 rounded-xl text-[12px] font-medium text-brand-600 bg-brand-50
                         border border-brand-100 hover:bg-brand-100 transition-colors">
              查看验证码（打开收件箱）
            </button>
            <input value={code} inputMode="numeric" placeholder="6 位验证码"
              onChange={e => setCode(e.target.value.replace(/\\D/g, '').slice(0, 6))}
              className="w-44 px-3.5 py-2.5 rounded-xl border border-slate-200 bg-white text-[18px]
                         font-mono tracking-[0.3em] text-slate-700 outline-none
                         focus:border-brand-400 focus:ring-2 focus:ring-brand-100 transition" />
            <button disabled={code.length < 4 || busy} onClick={() => { setCode(''); onCodeSubmit(code); }}
              className="px-4 py-2.5 rounded-xl text-[13px] font-semibold text-white bg-brand-600
                         hover:bg-brand-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
              {busy ? '验证中…' : '提交验证码'}
            </button>
            {onCancel && (
              <button disabled={busy} onClick={onCancel}
                className="px-4 py-2.5 rounded-xl text-[13px] font-medium border border-slate-200
                           text-slate-500 hover:bg-slate-50 transition-colors">放弃</button>
            )}
          </div>
          <div className="mt-3 text-[11px] text-slate-400">
            验证码错误 3 次将作废本次操作。验证码不会写入对话记录。
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-4 flex-wrap">
          <PasswordCard busy={busy} errorText={pwdErr} attemptsLeft={attemptsLeft}
            locked={locked} needSet={needSet}
            onFilled={submitPassword}
            onCancel={onCancel ? () => onCancel() : null} />
        </div>
      )}
    </div>
  );
}

/* ============================================================
   轻量 Markdown → React 节点解析'''
assert anchor in s
s = s.replace(anchor, new_blocks, 1)

# shake CSS
s = s.replace("  /* A2：金额 reveal / 静默回遮 fade 0.3s */",
"""  /* B5：密码卡错误 shake */
  @keyframes shakeX{ 0%,100%{transform:translateX(0);} 20%{transform:translateX(-7px);} 40%{transform:translateX(7px);} 60%{transform:translateX(-4px);} 80%{transform:translateX(4px);} }
  .animate-shake{ animation:shakeX .48s ease; }
  /* A2：金额 reveal / 静默回遮 fade 0.3s */""", 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('part1 OK')
