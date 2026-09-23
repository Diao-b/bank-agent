# -*- coding: utf-8 -*-
"""把 web/index.html 里的 ④ 卡片管理页 整段替换为「卡片-账户一体化」版本。

用法：.venv/Scripts/python.exe scripts/_patch_cards_page.py
（改完必须跑 scripts/_check_babel.js 校验语法，JSX 错误会整页白屏）
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "web", "index.html")

START = "/* ============================================================\n   ④ 卡片管理页"
END = "/* ============================================================\n   ⑤ 订阅管理页"

NEW = r'''/* ============================================================
   ④ 卡片管理页（卡片-账户一体化）
   —— 卡片必须挂靠真实账户：卡是账户的入口，不是装饰记录。
      · 储蓄卡：默认开新账户（生成账号 + 合法卡号）或挂靠已有账户（单账户 ≤2 张）
      · 信用卡：挂信用账户（每人一个，共享额度 ¥5,000）—— 只展示不转账
      · 同一账户下的卡余额同步；卡片里的钱用「归集到主账户」搬到主账户花
   ============================================================ */
const CARD_KIND_LABEL = {
  debit_new:  '储蓄卡 · 开新账户',
  debit_link: '储蓄卡 · 挂靠已有账户',
  credit:     '信用卡 · 额度 ¥5,000',
};

function CardsPage() {
  /* A4：额度 click 级遮蔽（会话内点开一次即保持） */
  const [limitShown, setLimitShown] = useState(false);
  const [ver, setVer] = useState(0);
  const cs = useApi('/api/cards', [ver]);
  const as = useApi('/api/accounts', [ver]);
  const { phoneBound } = useUser();              // 写操作闸门：申请新卡前先查绑定态
  const cards = (cs.data && cs.data.cards) || [];
  const limits = (cs.data && cs.data.limits) || { debit: { used: 0, max: 3 }, credit: { used: 0, max: 2 } };
  const accounts = (as.data && as.data.accounts) || [];
  const debitAccounts = accounts.filter(a => (a.account_type || 'debit') !== 'credit');
  const mainAccount = debitAccounts.find(a => a.is_primary) || debitAccounts[0] || null;
  const debitFull = limits.debit.used >= limits.debit.max;
  const creditFull = limits.credit.used >= limits.credit.max;

  /* 申请新卡：卡类型三选一（开新账户 / 挂靠已有账户 / 信用卡） */
  const [kind, setKind] = useState('debit_new');
  const [linkAccId, setLinkAccId] = useState('');
  const [applyBusy, setApplyBusy] = useState(false);
  const [applyMsg, setApplyMsg] = useState(null);   // {ok: bool, text: string}
  const [newCard, setNewCard] = useState(null);     // 申请成功 → 完整卡号只展示一次
  const kindFull = (kind === 'credit') ? creditFull : debitFull;

  /* 归集弹层：金额 → 支付密码 → 完成（本人账户间划转，免验证码） */
  const [collect, setCollect] = useState(null);
  const [collectBusy, setCollectBusy] = useState(false);
  const [collectErr, setCollectErr] = useState('');
  const [collectNeedSet, setCollectNeedSet] = useState(false);

  const inputCls = 'w-full px-3.5 py-2.5 rounded-xl border border-slate-200 bg-white text-sm text-slate-700 '
                 + 'outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-100 transition';

  const applyCard = async (e) => {
    e.preventDefault();
    if (!requireBind(phoneBound)) return;        // 申请新卡是写操作，未绑定先引导
    if (kindFull) return;
    const formEl = e.currentTarget;   // React 合成事件：await 后 e.currentTarget 会被置 null，先缓存
    const f = new FormData(formEl);
    const ctype = kind === 'credit' ? 'credit' : 'debit';
    const accountId = kind === 'debit_link' ? Number(linkAccId || 0) : null;
    if (kind === 'debit_link' && !accountId) {
      setApplyMsg({ ok: false, text: '请选择要挂靠的账户' });
      return;
    }
    setApplyBusy(true);
    setApplyMsg(null);
    setNewCard(null);
    try {
      const res = await fetch('/api/cards/apply', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          name: (String(f.get('name') || '').trim()) || null,
          type: ctype,
          account_id: accountId,
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) throw new Error(d.error || d.detail || ('HTTP ' + res.status));
      formEl.reset();
      setNewCard(d);                             // ★ 完整卡号 4-4-4-4 只在这里展示一次
      setApplyMsg({ ok: true, text: d.message || '申请成功' });
      setVer(v => v + 1);
    } catch (err) {
      setApplyMsg({ ok: false, text: '申请失败：' + err.message });
    } finally {
      setApplyBusy(false);
    }
  };

  /* ---- 打开归集弹层：查支付密码状态（未设置 → 先设置；已锁定 → 直接提示） ---- */
  const openCollect = async (card) => {
    if (!requireBind(phoneBound)) return;
    if (!mainAccount) { setApplyMsg({ ok: false, text: '没有可用主账户，暂时无法归集' }); return; }
    const others = debitAccounts.filter(a => a.id !== card.account_id);
    setCollectErr('');
    setCollectBusy(false);
    setCollect({
      card,
      fromId: String(card.account_id || ''),
      toId: String((mainAccount && mainAccount.id) || (others[0] && others[0].id) || ''),
      amt: '',
      step: 'amount',
      result: null,
    });
    try {
      const res = await fetch('/api/pay/password/status', { headers: authHeaders() });
      const d = await res.json().catch(() => ({}));
      setCollectNeedSet(d && d.has_password === false);
      if (d && d.locked) setCollectErr('支付密码已锁定（连续错误 3 次），请明日再试');
    } catch (err) { setCollectNeedSet(false); }
  };

  const collectAmount = Number(collect && collect.amt || 0);

  const submitPwd = async (pwd) => {
    if (!collect) return;
    if (!(collectAmount > 0)) { setCollectErr('请输入正确的归集金额'); return; }
    setCollectBusy(true);
    setCollectErr('');
    try {
      const url = collectNeedSet ? '/api/pay/password/set' : '/api/pay/password/verify';
      let res = await fetch(url, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ password: pwd }),
      });
      let d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) throw new Error(d.error || d.message || '支付密码核验失败');
      res = await fetch('/api/accounts/transfer-internal', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          from_account_id: Number(collect.fromId),
          to_account_id: Number(collect.toId),
          amount: collectAmount,
        }),
      });
      d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) throw new Error(d.error || d.detail || '归集失败');
      setCollect(c => ({ ...c, step: 'done', result: d }));
      setVer(v => v + 1);
    } catch (err) {
      setCollectErr(err.message);
    } finally {
      setCollectBusy(false);
    }
  };

  const fromAccount = collect
    ? (accounts.find(a => String(a.id) === String(collect.fromId)) || {}) : {};
  const toAccount = collect
    ? (accounts.find(a => String(a.id) === String(collect.toId)) || {}) : {};

  return (
    <PageShell className="space-y-5">
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="持有卡片" value={cards.length} icon="credit-card" tone="blue" raw />
        <StatCard label="使用中" value={cards.filter(c => c.status === 'normal').length}
                  icon="check-circle" tone="emerald" raw />
        <StatCard label="已冻结" value={cards.filter(c => c.status !== 'normal').length}
                  icon="snowflake" tone="cyan" raw />
      </div>

      {/* 申请新卡：卡片挂靠真实账户（储蓄卡开新账户/挂靠已有账户；信用卡挂信用账户） */}
      <Panel title="申请新卡" icon="credit-card"
             right={<span className="text-[11px] text-slate-400">
               储蓄卡 {limits.debit.used}/{limits.debit.max} · 信用卡 {limits.credit.used}/{limits.credit.max}
             </span>}>
        <form onSubmit={applyCard} className="space-y-3">
          <div className="flex items-end gap-3">
            <label className="flex-1">
              <span className="block text-[11px] text-slate-400 mb-1">卡面名称（选填）</span>
              <input name="name" className={inputCls} placeholder="如：家庭共享卡 / 出差专用卡"
                     maxLength={20} />
            </label>
            <label className="w-56 shrink-0">
              <span className="block text-[11px] text-slate-400 mb-1">卡类型</span>
              <select value={kind} onChange={e => setKind(e.target.value)} className={inputCls}>
                <option value="debit_new">储蓄卡 · 开新账户</option>
                <option value="debit_link">储蓄卡 · 挂靠已有账户</option>
                <option value="credit">信用卡 · 额度 ¥5,000</option>
              </select>
            </label>
            <button type="submit" disabled={applyBusy || kindFull}
              className="shrink-0 inline-flex items-center justify-center gap-1.5 rounded-xl px-4 py-2.5 text-xs font-medium
                         bg-brand-600 hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed text-white
                         shadow-[0_4px_14px_rgba(59,130,246,0.28)] transition-all active:scale-[0.98]">
              {applyBusy ? '申请中…' : (kindFull ? '已达上限' : '立即申请')}
            </button>
          </div>

          {/* 挂靠模式：下拉列出本人账户（同一账户最多 2 张卡；已满的置灰不可选） */}
          {kind === 'debit_link' && (
            <label className="block">
              <span className="block text-[11px] text-slate-400 mb-1">挂靠到哪个账户</span>
              <select value={linkAccId} onChange={e => setLinkAccId(e.target.value)} className={inputCls}>
                <option value="">请选择账户</option>
                {debitAccounts.map(a => (
                  <option key={a.id} value={a.id} disabled={!a.can_bind_more}>
                    {a.name}（尾号 {a.card_tail || '—'}）· 已挂 {a.card_count}/2 张卡
                  </option>
                ))}
              </select>
              <span className="block mt-1 text-[11px] text-slate-400">
                挂靠的卡与账户同余额：同一账户下的卡余额同步显示；卡可注销，账户余额不受影响。
              </span>
            </label>
          )}

          <div className="text-[11px] text-slate-400">
            {kind === 'credit'
              ? '信用卡挂信用账户（每人一个，共享额度 ¥5,000），卡号独立；信用卡功能演示中，仅展示额度与状态。'
              : '储蓄卡开出来的账户余额为 ¥0.00，卡号立即可用于收款（对方按卡号转账即可入账）。'}
          </div>

          {(kindFull || applyMsg) && (
            <div className="space-y-1">
              {kindFull && (
                <div className="text-xs text-amber-600">
                  已达{kind === 'credit' ? '信用卡' : '储蓄卡'}上限（
                  {kind === 'credit' ? limits.credit.used : limits.debit.used}/
                  {kind === 'credit' ? limits.credit.max : limits.debit.max} 张），
                  如需新卡请先注销不用的卡。
                </div>
              )}
              {applyMsg && (
                <div className={`text-xs ${applyMsg.ok ? 'text-emerald-600' : 'text-rose-500'}`}>
                  {applyMsg.text}
                </div>
              )}
            </div>
          )}
        </form>

        {/* 申请成功：完整卡号仅展示这一次（4-4-4-4），请妥善保管 */}
        {newCard && (
          <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50/60 px-4 py-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-emerald-700">
                「{newCard.card_name}」申请成功 · 请妥善保管卡号
              </span>
              <button onClick={() => setNewCard(null)}
                className="text-[11px] text-slate-400 hover:text-slate-600 transition-colors">我已保存</button>
            </div>
            <div className="mt-2 font-mono text-[22px] tracking-[0.18em] text-slate-800">
              {newCard.card_no_grouped}
            </div>
            <div className="mt-2 text-[11px] text-slate-500 leading-relaxed">
              完整卡号仅在本次展示，之后列表只显示尾号 {newCard.tail}。
              {newCard.card_type === 'credit'
                ? `已挂信用账户「${newCard.account_name}」，可用额度 ¥${fmtMoney(newCard.available_limit)}。`
                : `已关联账户「${newCard.account_name}」（余额 ¥${fmtMoney(newCard.account_balance)}），
                   对方按此卡号转账即可入账，到账用「归集到主账户」搬到主账户使用。`}
            </div>
          </div>
        )}
      </Panel>

      {cs.loading ? <Loading /> : cs.error ? <ErrBox text={cs.error} />
        : cards.length === 0 ? <Empty text="暂无银行卡" icon="credit-card" />
        : (
          <div className="grid grid-cols-2 gap-4">
            {cards.map(c => {
              const frozen = c.status !== 'normal';
              const isCredit = c.card_type === 'credit';
              const used = isCredit ? c.used_limit : 0;
              const limit = isCredit ? c.credit_limit : 0;
              const usage = isCredit ? Math.min(Math.max(c.usage_ratio, 0), 1) : 0;

              return (
                <div key={c.id}
                  className={`relative overflow-hidden rounded-2xl p-5 text-white transition-all duration-300
                    ${frozen ? 'grayscale opacity-75' : 'shadow-[0_8px_24px_rgba(11,77,162,0.22)]'}`}
                  style={{
                    backgroundImage: isCredit
                      ? 'linear-gradient(135deg,#0A3D80 0%,#1D4ED8 55%,#0B4DA2 100%)'
                      : 'linear-gradient(135deg,#1D4ED8 0%,#3B82F6 55%,#6366F1 100%)',
                  }}>
                  {/* 信用卡金色描边 */}
                  {isCredit && <span className="absolute inset-0 rounded-2xl border border-gold-400/45 pointer-events-none" />}
                  {/* 装饰光斑 */}
                  <span className="pointer-events-none absolute -top-16 -right-10 w-44 h-44 rounded-full opacity-20 blur-2xl"
                        style={{ background: 'linear-gradient(135deg,#F0D590 0%,#FFFFFF 100%)' }} />
                  {/* 冻结角标 */}
                  {frozen && (
                    <span className="absolute top-3 right-3 inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                                     bg-white/20 text-[10px] font-medium backdrop-blur-sm">
                      已冻结
                    </span>
                  )}

                  <div className="relative">
                    <div className="flex items-center justify-between mb-5">
                      <span className="text-[11px] text-white/60">{c.bank}</span>
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium
                        ${frozen ? 'bg-orange-400/25 text-orange-100' : 'bg-emerald-400/25 text-emerald-100'}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${frozen ? 'bg-orange-300' : 'bg-emerald-300'}`} />
                        {frozen ? '已冻结' : '使用中'}
                      </span>
                    </div>

                    <div className="text-base font-semibold tracking-wide mb-0.5">{c.card_name || c.bank}</div>
                    <div className="font-mono text-sm tracking-[0.18em] text-white/85">
                      •••• •••• •••• {c.tail || (c.card_no || '').slice(-4)}
                    </div>

                    {/* 关联账户：卡背后的账户名 + 余额（click 级遮蔽） */}
                    <div className="mt-3 mb-1 flex items-center justify-between text-[11px]">
                      <span className="text-white/70 truncate">
                        关联账户：{c.account_name || '未关联'}
                        {c.account_is_primary ? '（主账户）' : ''}
                      </span>
                      <span className="text-white/70 shrink-0 ml-2">
                        余额 <Amount value={c.account_balance} level="click" className="!text-white" />
                      </span>
                    </div>

                    {/* 信用卡额度进度条 */}
                    {isCredit ? (
                      <div className="mt-3 mb-1">
                        <div className="flex items-center justify-between text-[10px] text-white/70 mb-1.5">
                          <span>可用额度{' '}
                            {limitShown
                              ? <span className="font-mono amount-fade">¥{fmtMoney(c.available_limit)}</span>
                              : <button onClick={() => setLimitShown(true)}
                                  className="font-mono hover:text-white transition-colors">{MASK_TEXT}</button>}
                          </span>
                          <span className="font-mono">{(usage * 100).toFixed(0)}% 已用</span>
                        </div>
                        <div className="h-1.5 rounded-full bg-white/20 overflow-hidden">
                          <div className="h-full rounded-full transition-all duration-700"
                               style={{
                                 width: `${Math.max(usage * 100, 4)}%`,
                                 background: usage > 0.9
                                   ? 'linear-gradient(90deg,#FB7185 0%,#F43F5E 100%)'
                                   : 'linear-gradient(90deg,#F0D590 0%,#E3C46A 100%)',
                               }} />
                        </div>
                        <div className="text-[10px] text-white/50 mt-1.5 font-mono">
                          {limitShown ? <>总额度 ¥{fmtMoney(limit)} · 已用 ¥{fmtMoney(used)}</> : '点击可用额度数字查看明细'}
                        </div>
                        <div className="text-[10px] text-white/45 mt-1">信用卡功能演示中（仅展示额度与状态，不支持转账收款）</div>
                      </div>
                    ) : (
                      <div className="text-[10px] text-white/50 mt-2">
                        储蓄卡 · 卡号尾号 {c.tail} · 可用于收款
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

      {/* 操作区：冻结/解冻走对话页预填确认；归集走本人账户间划转（支付密码核验） */}
      {!cs.loading && !cs.error && cards.length > 0 && (
        <Panel title="卡片操作" icon="shield-check"
               right={<span className="text-[11px] text-slate-400">冻结 / 解冻需经小微安全确认 · 归集仅需支付密码</span>}>
          <div className="space-y-2.5">
            {cards.map(c => {
              const frozen = c.status !== 'normal';
              const isCredit = c.card_type === 'credit';
              const onMain = mainAccount && String(c.account_id) === String(mainAccount.id);
              return (
                <div key={c.id} className="flex items-center gap-3 rounded-xl border border-slate-100 px-4 py-2.5
                                            hover:border-brand-200 transition-colors">
                  <span className="text-xs text-slate-700 flex-1 truncate">
                    {c.card_name || c.bank}
                    <span className="text-slate-400 font-mono ml-1.5">尾号 {c.tail || (c.card_no || '').slice(-4)}</span>
                    <span className="text-slate-400 ml-2">{c.account_name ? '账户 ' + c.account_name : ''}</span>
                  </span>
                  <AskBtn text={`我的${c.card_name || c.bank}（尾号${c.tail}）现在是什么状态`} compact />
                  {!isCredit && !onMain && (
                    <button onClick={() => openCollect(c)} disabled={frozen}
                      className="inline-flex items-center justify-center rounded-xl px-3 py-1.5 text-[11px] font-medium
                                 border border-brand-200 text-brand-700 hover:bg-brand-50
                                 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
                      归集到主账户
                    </button>
                  )}
                  {!isCredit && onMain && (
                    <span className="text-[11px] text-slate-300 px-1">已挂靠主账户</span>
                  )}
                  {isCredit && <span className="text-[11px] text-slate-300 px-1">信用卡演示中</span>}
                  <ActionBtn text={frozen ? `解冻我的${c.card_name || c.bank}（尾号${c.tail}）`
                                          : `冻结我的${c.card_name || c.bank}（尾号${c.tail}）`}
                             tone={frozen ? 'ghost' : 'brand'} className="!py-1.5 !text-[11px]">
                    {frozen ? '解冻' : '冻结'}
                  </ActionBtn>
                </div>
              );
            })}
          </div>
        </Panel>
      )}

      {/* 归集弹层：金额 → 支付密码 → 完成（本人账户间，免验证码） */}
      {collect && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center px-4"
             style={{ background: 'rgba(15,23,42,0.38)' }}
             onClick={() => { if (!collectBusy) setCollect(null); }}>
          <div className="w-full max-w-md" onClick={e => e.stopPropagation()}>
            {collect.step === 'amount' && (
              <div className="rounded-2xl bg-white px-5 py-4 shadow-xl">
                <div className="flex items-center justify-between">
                  <span className="text-[14px] font-semibold text-slate-700">归集到主账户</span>
                  <span className="text-[11px] text-slate-400">本人账户间划转 · 免验证码</span>
                </div>
                <div className="mt-3 space-y-3">
                  <div className="rounded-xl bg-slate-50 px-3 py-2.5 text-[12px] text-slate-600 space-y-1">
                    <div>转出账户：{fromAccount.name || '—'}
                      <span className="text-slate-400 font-mono ml-1.5">{fromAccount.card_no_masked || ''}</span></div>
                    <div>账户余额：<Amount value={fromAccount.balance} level="click" /></div>
                  </div>
                  <label className="block">
                    <span className="block text-[11px] text-slate-400 mb-1">转入账户</span>
                    <select value={String(collect.toId)} className={inputCls}
                            onChange={e => setCollect(c => ({ ...c, toId: e.target.value }))}>
                      {debitAccounts.filter(a => String(a.id) !== String(collect.fromId)).map(a => (
                        <option key={a.id} value={a.id}>
                          {a.name}{a.is_primary ? '（主账户）' : ''} · 尾号 {a.card_tail || '—'}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="block">
                    <span className="block text-[11px] text-slate-400 mb-1">归集金额</span>
                    <div className="flex items-center gap-2">
                      <input type="number" min="0" step="0.01" value={collect.amt} placeholder="0.00"
                             className={inputCls}
                             onChange={e => setCollect(c => ({ ...c, amt: e.target.value }))} />
                      <button type="button"
                        onClick={() => setCollect(c => ({ ...c, amt: String(fromAccount.balance || 0) }))}
                        className="shrink-0 rounded-xl border border-slate-200 px-3 py-2.5 text-[11px] text-slate-500
                                   hover:border-brand-200 hover:text-brand-600 transition-colors">全部</button>
                    </div>
                  </label>
                  <div className="text-[11px] text-slate-400 leading-relaxed">
                    归集后主账户余额增加、此卡归零；该卡仍可继续收款（收入只进不出，需归集后才能用于付款）。
                  </div>
                </div>
                <div className="mt-4 flex items-center justify-end gap-2">
                  <button onClick={() => setCollect(null)}
                    className="rounded-xl px-3 py-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors">
                    取消
                  </button>
                  <button
                    onClick={() => {
                      if (!(Number(collect.amt) > 0)) { setCollectErr('请输入正确的归集金额'); return; }
                      setCollectErr('');
                      setCollect(c => ({ ...c, step: 'pwd' }));
                    }}
                    className="rounded-xl bg-brand-600 hover:bg-brand-700 px-4 py-2 text-[12px] font-medium text-white
                               shadow-[0_4px_14px_rgba(59,130,246,0.28)] transition-all active:scale-[0.98]">
                    下一步
                  </button>
                </div>
                {collectErr && <div className="mt-2 text-[11px] text-rose-500">{collectErr}</div>}
              </div>
            )}

            {collect.step === 'pwd' && (
              <div className="space-y-2">
                <div className="rounded-xl bg-white px-4 py-2.5 text-[12px] text-slate-600 shadow">
                  归集 {fromAccount.name} → {toAccount.name} · 金额 ¥{fmtMoney(collect.amt)}
                </div>
                <PasswordCard busy={collectBusy} errorText={collectErr} needSet={collectNeedSet}
                              onFilled={submitPwd}
                              onCancel={() => { setCollectErr(''); setCollect(c => ({ ...c, step: 'amount' })); }} />
              </div>
            )}

            {collect.step === 'done' && (
              <div className="rounded-2xl bg-white px-5 py-5 shadow-xl">
                <div className="text-[14px] font-semibold text-emerald-700">归集完成</div>
                <div className="mt-2 text-[12px] text-slate-600 leading-relaxed">
                  {(collect.result && collect.result.message) || '资金归集已完成'}
                </div>
                <div className="mt-2 text-[11px] text-slate-400">
                  {toAccount.name} 余额 ¥{fmtMoney(collect.result && collect.result.to_balance_after)} ·
                  {' '}{fromAccount.name} 余额 ¥{fmtMoney(collect.result && collect.result.from_balance_after)}
                </div>
                <div className="mt-4 flex justify-end">
                  <button onClick={() => { setCollect(null); setCollectErr(''); }}
                    className="rounded-xl bg-brand-600 hover:bg-brand-700 px-4 py-2 text-[12px] font-medium text-white
                               transition-all active:scale-[0.98]">完成</button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </PageShell>
  );
}

'''

def main():
    with io.open(PATH, encoding="utf-8") as f:
        s = f.read()
    i = s.find(START)
    j = s.find(END)
    assert i != -1, "未找到 ④ 卡片管理页 起点"
    assert j != -1 and j > i, "未找到 ⑤ 订阅管理页 终点"
    out = s[:i] + NEW + s[j:]
    with io.open(PATH, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"替换完成：{j - i} 字符 → {len(NEW)} 字符；文件总长 {len(s)} → {len(out)}")


if __name__ == "__main__":
    main()
