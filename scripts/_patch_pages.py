# -*- coding: utf-8 -*-
"""前端改造：页面遮蔽适配 + 大额警示行 + 实名认证（收件箱验证码）"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------------------------------------------------------------- ConfirmCard 大额警示行（红色渲染）
old = """              {k === '金额'
                ? <span className="text-[26px] leading-none font-semibold text-gold-600 font-mono tracking-tight">{v}</span>
                : <span className="text-[15px] text-slate-800 font-medium">{v}</span>}"""
new = """              {k === '警示'
                ? <span className="text-[14px] text-rose-600 font-semibold leading-snug">{v}</span>
                : k === '金额'
                  ? <span className="text-[26px] leading-none font-semibold text-gold-600 font-mono tracking-tight">{v}</span>
                  : <span className="text-[15px] text-slate-800 font-medium">{v}</span>}"""
assert old in s; s = s.replace(old, new, 1)

# ---------------------------------------------------------------- CardsPage / ProfilePage 金额 click 级
old = """                  <Amount value={a.balance} className="text-[15px] font-semibold" />"""
new = """                  <Amount value={a.balance} level="click" className="text-[15px] font-semibold" />"""
assert old in s; s = s.replace(old, new, 1)

# ProfilePage 账户概览（3300 区域）同款 —— 上面的替换只命中一处，逐个处理
old2 = """                <div key={a.name}
                     className="flex items-center justify-between py-2.5 border-b border-slate-100 last:border-0">
                  <div className="text-[13px] text-slate-700 truncate">{a.name}</div>
                  <Amount value={a.balance} level="click" className="text-[15px] font-semibold" />"""
assert old2 in s  # 确认上一步替换命中的是 ProfilePage（CardsPage 用的是别的写法则另行处理）

# ---------------------------------------------------------------- TransferPage 步骤②余额 click+会话内
old = """                      <div className="text-[13px] font-medium text-slate-700 truncate">{a.name}</div>
                      <div className="mt-1 text-[15px] font-mono font-semibold text-gold-600">
                        ¥{fmtMoney(a.balance)}
                      </div>"""
new = """                      <div className="text-[13px] font-medium text-slate-700 truncate">{a.name}</div>
                      {balShown ? (
                        <div className="mt-1 text-[15px] font-mono font-semibold text-gold-600 amount-fade">
                          ¥{fmtMoney(a.balance)}
                        </div>
                      ) : (
                        <button onClick={() => setBalShown(true)}
                          className="mt-1 text-[15px] font-mono font-semibold text-gold-600/70">
                          {MASK_TEXT}
                          <span className="ml-2 text-[11px] text-slate-400 font-sans">显示</span>
                        </button>
                      )}"""
assert old in s; s = s.replace(old, new, 1)

old = """  const amtNum = Number(amount);"""
new = """  /* A4：步骤②余额 click 级遮蔽；本会话内点开一次即保持（不自动回遮） */
  const [balShown, setBalShown] = useState(false);
  const amtNum = Number(amount);"""
assert old in s; s = s.replace(old, new, 1)

old = """                <span className="ml-auto text-[11px] text-slate-400">
                  可用余额 <b className="font-mono text-slate-500">¥{avail == null ? '—' : fmtMoney(avail)}</b>
                </span>"""
new = """                <span className="ml-auto text-[11px] text-slate-400">
                  可用余额{' '}
                  {balShown
                    ? <b className="font-mono text-slate-500">{avail == null ? '—' : '¥' + fmtMoney(avail)}</b>
                    : <button onClick={() => setBalShown(true)}
                        className="font-mono text-slate-400 hover:text-brand-600 transition-colors">
                        {MASK_TEXT}<span className="ml-1 font-sans">显示</span>
                      </button>}
                </span>"""
assert old in s; s = s.replace(old, new, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
