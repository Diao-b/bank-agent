# -*- coding: utf-8 -*-
# C8：顶栏/侧栏 chrome 全面接入主题变量（日间=米白纸质+墨蓝字）
import io

p = 'web/index.html'
s = io.open(p, encoding='utf-8').read()
n0 = len(s)

PAIRS = []

# ---- PrivacyToggle / ThemeToggle 激活与未激活态 ----
PAIRS.append((
 """          ? 'bg-[rgba(212,160,23,0.16)] border-[rgba(212,160,23,0.50)] text-[#F0D590]'
          : 'bg-transparent border-white/15 text-white/60 hover:text-white hover:bg-white/5'}`}""",
 """          ? 'bg-[color:var(--chrome-accent-bg)] border-[color:var(--chrome-accent-border)] text-[color:var(--chrome-accent)]'
          : 'bg-transparent border-[color:var(--chrome-line)] text-[color:var(--chrome-fg-dim)] hover:text-[color:var(--chrome-fg)] hover:bg-[color:var(--chrome-hover)]'}`}"""))

# ---- TopBar 页面标题分隔线 ----
PAIRS.append((
 '''          <div className="flex items-center gap-2.5 ml-3 pl-5 border-l border-white/15 animate-slide-up" key={title}>''',
 '''          <div className="flex items-center gap-2.5 ml-3 pl-5 animate-slide-up"
               style={{ borderLeft: '1px solid var(--chrome-line)' }} key={title}>'''))

# ---- UserMenu 头部手机尾号 ----
PAIRS.append((
 '''              <div className="text-[11px] text-white/45 font-mono truncate mt-0.5">
                {phoneTail || '手机未绑定'}
              </div>''',
 '''              <div className="text-[11px] font-mono truncate mt-0.5"
                   style={{ color: 'var(--chrome-fg-dim)' }}>
                {phoneTail || '手机未绑定'}
              </div>'''))

# ---- UserMenu 下拉面板：底色/描边随主题 ----
PAIRS.append((
 '''        <div className="absolute right-0 top-[52px] w-[240px] rounded-2xl border z-[75] overflow-hidden animate-slide-up"
             style={{ background: 'rgba(13,19,29,0.96)', backdropFilter: 'blur(16px)',
                      borderColor: 'rgba(212,160,23,0.25)',
                      boxShadow: '0 24px 60px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.05)' }}>''',
 '''        <div className="absolute right-0 top-[52px] w-[240px] rounded-2xl border z-[75] overflow-hidden animate-slide-up"
             style={{ background: 'var(--chrome-panel)', backdropFilter: 'blur(16px)',
                      borderColor: 'var(--chrome-accent-border)',
                      boxShadow: '0 24px 60px var(--chrome-shadow), inset 0 1px 0 var(--metal-hi)' }}>'''))
PAIRS.append((
 '''            <div className="px-4 py-3.5 border-b border-white/10 flex items-center gap-3">''',
 '''            <div className="px-4 py-3.5 border-b flex items-center gap-3"
                 style={{ borderColor: 'var(--chrome-line-soft)' }}>'''))
# 菜单项文字
PAIRS.append((
 """            <button className={item + ' text-slate-200 hover:bg-white/[0.06]'}>
              个人信息
            </button>""",
 """            <button className={item + ' hover:bg-[color:var(--chrome-hover)]'}
                    style={{ color: 'var(--chrome-fg-strong)' }}>
              个人信息
            </button>"""))
PAIRS.append((
 """            <button className={item + ' text-slate-200 hover:bg-white/[0.06]'}>
              实名认证
            </button>""",
 """            <button className={item + ' hover:bg-[color:var(--chrome-hover)]'}
                    style={{ color: 'var(--chrome-fg-strong)' }}>
              实名认证
            </button>"""))
PAIRS.append((
 """              <button className={item + ' text-cyan-300 hover:bg-cyan-400/10'}
                      onClick={() => { window.location.href = '/admin'; }}>""",
 """              <button className={item + ' hover:bg-[color:var(--chrome-hover)]'}
                      style={{ color: 'var(--chrome-safe)' }}
                      onClick={() => { window.location.href = '/admin'; }}>"""))

# ---- SideNav：底色/边线/导航项/底部提示 ----
PAIRS.append((
 '''    <nav className="w-[232px] shrink-0 border-r border-white/[0.06] flex flex-col"
         style={{ background: 'rgba(8,12,20,0.86)', backdropFilter: 'blur(14px)' }}>''',
 '''    <nav className="w-[232px] shrink-0 flex flex-col"
         style={{ background: 'var(--chrome-solid)', backdropFilter: 'blur(14px)',
                  borderRight: '1px solid var(--chrome-line-soft)' }}>'''))
PAIRS.append((
 '''                ${active
                  ? 'text-[#F0D590] bg-white/[0.05] shadow-[inset_0_0_0_1px_rgba(212,160,23,0.22)]'
                  : 'text-white/55 hover:text-white hover:bg-white/[0.04]'}''',
 '''                ${active
                  ? 'text-[color:var(--chrome-accent)] bg-[color:var(--chrome-hover)] shadow-[inset_0_0_0_1px_rgba(212,160,23,0.22)]'
                  : 'text-[color:var(--chrome-fg-dim)] hover:text-[color:var(--chrome-fg)] hover:bg-[color:var(--chrome-hover)]'}'''))
PAIRS.append((
 '''      <div className="shrink-0 px-4 pb-3 pt-1 border-t border-white/[0.06]">
        <div className="text-[11px] text-white/35 leading-relaxed">
          个人信息与退出：顶栏右上角用户区
        </div>
      </div>''',
 '''      <div className="shrink-0 px-4 pb-3 pt-1"
           style={{ borderTop: '1px solid var(--chrome-line-soft)' }}>
        <div className="text-[11px] leading-relaxed" style={{ color: 'var(--chrome-fg-faint)' }}>
          个人信息与退出：顶栏右上角用户区
        </div>
      </div>'''))

# ---- SideNav QuickActions（常暗玻璃 → chrome） ----
PAIRS.append((
 '''        <span className="text-xs font-semibold text-white/45">快捷操作</span>''',
 '''        <span className="text-xs font-semibold" style={{ color: 'var(--chrome-fg-faint)' }}>快捷操作</span>'''))
PAIRS.append((
 '''            className="flex items-center justify-center py-2.5 px-2 rounded-xl text-[12px] font-medium
                       text-white/70 bg-white/[0.04] border border-white/[0.08]
                       hover:text-[#F0D590] hover:border-[rgba(212,160,23,0.40)] hover:bg-white/[0.07]
                       transition-all duration-200">''',
 '''            className="flex items-center justify-center py-2.5 px-2 rounded-xl text-[12px] font-medium
                       bg-[color:var(--chrome-hover)] border transition-all duration-200
                       hover:text-[color:var(--chrome-accent)] hover:border-[color:var(--chrome-accent-border)]
                       hover:bg-[color:var(--chrome-accent-bg)]"
            style={{ color: 'var(--chrome-fg-dim)', borderColor: 'var(--chrome-line)' }}>'''))

ok = 0
miss = []
for old, new in PAIRS:
    if old in s:
        s = s.replace(old, new)
        ok += 1
    else:
        miss.append(old[:70])

io.open(p, 'w', encoding='utf-8', newline='').write(s)
print(f'APPLIED {ok}/{len(PAIRS)}')
for m in miss:
    print('MISS:', m.replace('\n', ' | '))
