# -*- coding: utf-8 -*-
"""前端改造：C4 收件箱（顶栏入口 + 抽屉 + 轮询）与 A4 顶栏三卡遮蔽"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------------------------------------------------------------- TopBar
old_topbar = '''function TopBar({ userName, mode, title, icon }) {
  return ('''
new_topbar = '''function TopBar({ userName, mode, title, icon, unread = 0, inboxOpen = false, onToggleInbox, inboxFlash = false }) {
  /* A3 隐私胶囊 + C4 收件箱入口：文字按钮表达一切（0 icon 约束） */
  return ('''
assert old_topbar in s
s = s.replace(old_topbar, new_topbar, 1)

old_right = '''      <div className="flex items-center gap-5">
        {/* 常驻信任锚点：绿色呼吸盾牌 */}'''
new_right = '''      <div className="flex items-center gap-5">
        {/* A3 隐私模式胶囊：全站金额遮蔽总开关 */}
        <PrivacyToggle />

        {/* C4 收件箱入口：文字 + 未读数（12px，有未读时亮色；新消息短暂高亮） */}
        <button onClick={onToggleInbox}
          className={`relative px-3 py-2 rounded-full text-[13px] font-medium border transition-all duration-200
            ${inboxOpen
              ? 'bg-white/25 border-white/40 text-white'
              : unread > 0
                ? 'bg-white/15 border-white/30 text-white hover:bg-white/25'
                : 'bg-transparent border-white/20 text-white/65 hover:text-white hover:bg-white/10'}`}>
          收件箱{unread > 0 ? <span className="ml-1.5 text-[12px] font-semibold">{unread}</span> : null}
          {inboxFlash && <span className="absolute inset-0 rounded-full bg-white/40 animate-pulse pointer-events-none" />}
        </button>

        {/* 常驻信任锚点：绿色呼吸盾牌 */}'''
assert old_right in s
s = s.replace(old_right, new_right, 1)

# ---------------------------------------------------------------- PrivacyToggle + InboxDrawer
anchor = '''/* ============================================================
   左侧固定导航栏（200px）'''
new_components = '''/* ============================================================
   A3 顶栏隐私胶囊
   ============================================================ */
function PrivacyToggle() {
  const { privacy, setPrivacy } = usePrivacy();
  return (
    <button onClick={() => setPrivacy(!privacy)} title="隐私模式：开启时全站金额脱敏"
      className={`px-3.5 py-2 rounded-full text-[13px] font-medium border transition-all duration-200
        ${privacy
          ? 'bg-white/15 border-white/30 text-white hover:bg-white/25'
          : 'bg-transparent border-white/25 text-white/70 hover:text-white hover:bg-white/10'}`}>
      隐私 {privacy ? '开' : '关'}
    </button>
  );
}

/* ============================================================
   C4 收件箱抽屉（右侧滑出 360px）
   —— 未读左侧亮色竖条 + 标题 + 相对时间，点击展开；
      验证码消息：金色大字（32px 等宽）+ 剩余有效期文字倒计时；
      已过期灰色置底；「标记全部已读」文字按钮；
      转账回执带「查看对话」链接。
   ============================================================ */
function relTime(iso) {
  if (!iso) return '';
  const t = new Date(String(iso).replace(' ', 'T'));
  if (isNaN(t)) return '';
  const diff = Math.max(0, (Date.now() - t.getTime()) / 1000);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  return Math.floor(diff / 86400) + ' 天前';
}

function remainText(expiresAt, now) {
  if (!expiresAt) return '';
  const t = new Date(String(expiresAt).replace(' ', 'T'));
  const ms = t.getTime() - now;
  if (isNaN(t)) return '';
  if (ms <= 0) return '已过期';
  const sec = Math.floor(ms / 1000);
  return Math.floor(sec / 60) + '分' + String(sec % 60).padStart(2, '0') + '秒后过期';
}

function InboxDrawer({ open, onClose, onChanged }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const [now, setNow] = useState(Date.now());   // 验证码倒计时心跳（每秒重渲染）

  const load = () => {
    setLoading(true);
    fetch('/api/inbox', { headers: authHeaders() })
      .then(r => r.json())
      .then(d => { if (d.ok) setItems(d.messages || []); })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { if (open) { setExpanded(null); load(); } }, [open]);
  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [open]);

  const markRead = (id) => {
    fetch(`/api/inbox/${id}/read`, { method: 'POST', headers: authHeaders() })
      .then(r => r.json()).then(() => { load(); onChanged && onChanged(); }).catch(() => {});
  };
  const markAll = () => {
    fetch('/api/inbox/read_all', { method: 'POST', headers: authHeaders() })
      .then(r => r.json()).then(() => { load(); onChanged && onChanged(); }).catch(() => {});
  };

  /* 已过期灰色置底 */
  const sorted = items.slice().sort((a, b) => {
    if (!!a.expired !== !!b.expired) return a.expired ? 1 : -1;
    return b.id - a.id;
  });

  return (
    <div className={`fixed inset-0 z-50 ${open ? '' : 'pointer-events-none'}`} aria-hidden={!open}>
      {/* 遮罩 */}
      <div onClick={onClose}
        className={`absolute inset-0 bg-slate-900/40 backdrop-blur-[2px] transition-opacity duration-300
          ${open ? 'opacity-100' : 'opacity-0'}`} />
      {/* 抽屉主体：深色玻璃 */}
      <aside className={`absolute right-0 top-0 h-full w-[360px] max-w-full flex flex-col
        bg-slate-900/95 backdrop-blur-xl border-l border-white/10 shadow-2xl
        transition-transform duration-300 ${open ? 'translate-x-0' : 'translate-x-full'}`}>
        <div className="shrink-0 flex items-center justify-between px-5 py-4 border-b border-white/10">
          <div className="text-[15px] font-semibold text-white">收件箱</div>
          <div className="flex items-center gap-3">
            <button onClick={markAll}
              className="text-[12px] text-white/60 hover:text-white transition-colors">标记全部已读</button>
            <button onClick={onClose}
              className="text-[12px] text-white/60 hover:text-white transition-colors">关闭</button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2.5 scroll-thin">
          {loading && items.length === 0 && (
            <div className="text-[12px] text-white/40 text-center py-8">正在读取…</div>
          )}
          {!loading && sorted.length === 0 && (
            <div className="text-[12px] text-white/40 text-center py-10">暂无消息，验证码与转账回执会送达这里</div>
          )}
          {sorted.map(m => {
            const isCode = m.type === 'verify_code';
            const code = (m.meta && m.meta.code) || '';
            const showCode = isCode && !m.expired && code;
            const isOpen = expanded === m.id;
            return (
              <div key={m.id}
                onClick={() => { setExpanded(isOpen ? null : m.id); if (!m.is_read) markRead(m.id); }}
                className={`relative rounded-xl border cursor-pointer transition-all duration-200
                  ${m.expired ? 'bg-white/[0.03] border-white/5 opacity-60'
                    : 'bg-white/[0.06] border-white/10 hover:bg-white/[0.09]'}
                  ${isOpen ? 'ring-1 ring-white/25' : ''}`}>
                {/* 未读亮色竖条 */}
                {!m.is_read && !m.expired && (
                  <span className="absolute left-0 top-2.5 bottom-2.5 w-[3px] rounded-full bg-sky-400" />
                )}
                <div className="px-4 py-3 pl-5">
                  <div className="flex items-baseline gap-2">
                    <span className={`text-[13px] font-semibold truncate flex-1
                      ${m.expired ? 'text-white/40' : 'text-white/90'}`}>{m.title}</span>
                    <span className="text-[11px] text-white/35 shrink-0">{relTime(m.created_at)}</span>
                  </div>
                  <div className={`mt-1 text-[12px] leading-relaxed ${m.expired ? 'text-white/30' : 'text-white/60'}`}>
                    {isOpen ? m.content : (m.summary || m.content)}
                  </div>

                  {/* 验证码特殊渲染：金色大字 + 倒计时 */}
                  {isOpen && showCode && (
                    <div className="mt-3 rounded-lg bg-black/30 border border-amber-400/25 px-4 py-3">
                      <div className="text-[32px] leading-none font-mono font-bold tracking-[0.18em] text-amber-300">
                        {code}
                      </div>
                      <div className="mt-2 text-[11px] text-amber-200/70">{remainText(m.expires_at, now)}</div>
                    </div>
                  )}
                  {isOpen && isCode && m.expired && (
                    <div className="mt-2 text-[11px] text-white/35">验证码已过期，请重新获取</div>
                  )}

                  {/* 转账回执：查看对话 */}
                  {isOpen && m.type === 'transfer_receipt' && (
                    <button
                      onClick={(e) => { e.stopPropagation(); goChatAfterTask(); onClose(); }}
                      className="mt-3 text-[12px] text-sky-300 hover:text-sky-200 underline underline-offset-2">
                      查看对话
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <div className="shrink-0 px-5 py-3 border-t border-white/10 text-[11px] text-white/35">
          验证码等通知真实写入收件箱，可追溯；外部通道为可选并行增强。
        </div>
      </aside>
    </div>
  );
}

/* ============================================================
   左侧固定导航栏（200px）'''
assert anchor in s
s = s.replace(anchor, new_components, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
