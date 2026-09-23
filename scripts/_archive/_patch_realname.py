# -*- coding: utf-8 -*-
"""实名认证（验证码走收件箱）+ ProfilePage 集成"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

anchor = 'function ProfilePage() {'
comp = '''/* ============================================================
   实名认证（C3-验收#3）：验证码同样走站内收件箱送达
   —— 发码 → 收件箱取码 → 核验通过才落 users.real_name
   ============================================================ */
function RealnameCard({ currentName }) {
  const [name, setName] = useState(currentName || '');
  const [code, setCode] = useState('');
  const [msg, setMsg] = useState(null);      // {ok, text}
  const [busy, setBusy] = useState(false);

  const send = async () => {
    setBusy(true); setMsg(null);
    try {
      const res = await fetch('/api/realname/send_code', { method: 'POST', headers: authHeaders() });
      const d = await res.json().catch(() => ({}));
      setMsg(res.ok && d.ok ? { ok: true, text: '验证码已发送至您的收件箱，5 分钟内有效' } : { ok: false, text: d.detail || '发送失败' });
    } catch (e) { setMsg({ ok: false, text: '网络错误' }); }
    setBusy(false);
  };
  const confirm = async () => {
    setBusy(true); setMsg(null);
    try {
      const res = await fetch('/api/realname/confirm', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ real_name: name, code }),
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.ok) {
        setMsg({ ok: true, text: '实名认证完成' });
        setCode('');
        setTimeout(() => window.location.reload(), 1200);
      } else {
        setMsg({ ok: false, text: d.error || d.detail || '验证失败' });
      }
    } catch (e) { setMsg({ ok: false, text: '网络错误' }); }
    setBusy(false);
  };

  return (
    <Panel title="实名认证" icon="shield">
      <div className="space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <input value={name} onChange={e => setName(e.target.value)} maxLength={20} placeholder="真实姓名"
            className="w-44 px-3.5 py-2.5 rounded-xl border border-slate-200 bg-white text-sm text-slate-700
                       outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-100 transition" />
          <input value={code} onChange={e => setCode(e.target.value.replace(/\\D/g, '').slice(0, 6))}
            inputMode="numeric" placeholder="6 位验证码"
            className="w-40 px-3.5 py-2.5 rounded-xl border border-slate-200 bg-white text-[16px] font-mono
                       tracking-[0.25em] text-slate-700 outline-none focus:border-brand-400 transition" />
          <button onClick={send} disabled={busy || !name.trim()}
            className="px-3.5 py-2.5 rounded-xl text-[12px] font-medium text-brand-600 bg-brand-50
                       border border-brand-100 hover:bg-brand-100 disabled:opacity-40 transition-colors">
            发送验证码
          </button>
          <button onClick={confirm} disabled={busy || code.length < 4 || !name.trim()}
            className="px-4 py-2.5 rounded-xl text-[12px] font-semibold text-white bg-brand-600
                       hover:bg-brand-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
            {busy ? '处理中…' : '提交认证'}
          </button>
        </div>
        <div className="text-[11px] text-slate-400">
          验证码发送至站内收件箱（右上角打开），核验通过后即完成实名。
        </div>
        {msg && (
          <div className={'text-[12px] ' + (msg.ok ? 'text-emerald-600' : 'text-rose-500')}>{msg.text}</div>
        )}
      </div>
    </Panel>
  );
}

function ProfilePage() {'''
assert anchor in s
s = s.replace(anchor, comp, 1)

old = """      {/* 只读账户概览（金色等宽数字） */}
      <Panel title="账户概览" icon="wallet">"""
new = """      {/* 实名认证（验证码走收件箱） */}
      <RealnameCard currentName={(profile && profile.real_name) || ''} />

      {/* 只读账户概览（金色等宽数字） */}
      <Panel title="账户概览" icon="wallet">"""
assert old in s
s = s.replace(old, new, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
