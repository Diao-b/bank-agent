# -*- coding: utf-8 -*-
"""一次性前端改造脚本：A 体系 PrivacyContext + Amount 遮蔽组件"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

# ---------- A3 PrivacyContext（挂在路由工具之后、组件层之前） ----------
anchor = "/* ================= 小工具 ================= */"
privacy = '''/* ============================================================
   A3 隐私模式（展示安全）
   —— 顶栏"隐私 开/关"胶囊 + PrivacyContext；localStorage 记忆；
      登录 / 新会话强制重置为"开"（login.html 登录成功时清除记忆键）。
   —— 隐私开启时全站金额一律遮蔽；关闭后 click 级仍默认遮蔽、主动显示才 reveal。
   ============================================================ */
const PrivacyCtx = createContext({ privacy: true, setPrivacy: () => {} });
const PRIVACY_KEY = 'xb_privacy';   // 'on' | 'off'
const resetPrivacyOn = () => { localStorage.removeItem(PRIVACY_KEY); };

function PrivacyProvider({ children }) {
  const [privacy, setPrivacyState] = useState(() => localStorage.getItem(PRIVACY_KEY) !== 'off');
  const setPrivacy = (v) => {
    setPrivacyState(!!v);
    localStorage.setItem(PRIVACY_KEY, v ? 'on' : 'off');
  };
  return <PrivacyCtx.Provider value={{ privacy, setPrivacy }}>{children}</PrivacyCtx.Provider>;
}
const usePrivacy = () => useContext(PrivacyCtx);

/* ================= 小工具 ================= */'''
assert anchor in s
s = s.replace(anchor, privacy, 1)

# ---------- A2 Amount 遮蔽组件（替换旧实现） ----------
old_amount = '''/* 金色等宽金额：全站统一，杜绝各处手搓格式 */
const Amount = ({ value, className = '', prefix = '¥' }) => (
  <span className={`font-mono tabular-nums text-gold-600 ${className}`}>
    {prefix}{fmtMoney(value)}
  </span>
);'''
new_amount = '''/* ============================================================
   A2 金额遮蔽组件（金色等宽；全站统一，杜绝各处手搓格式）
   —— level='always'：直接显示（页面负责附"敏感提示行"）
   —— level='click' ：默认"¥ ··· ··"+"显示"文字按钮；点击 onReveal 由页面
                       实现（如拉 /api/overview?reveal=true）；autoHide 10s
                       （传 null 表示不自动回遮）；静默回遮 fade 0.3s；卸载清计时器
   —— 隐私模式开启：全站一律遮蔽（验收#15）
   —— 遮蔽态渲染"¥ ··· ··"固定格式，绝不显示"¥0.00"假值
   ============================================================ */
const MASK_TEXT = '¥ ··· ··';
function Amount({ value, level = 'always', className = '', prefix = '¥', onReveal, autoHide = 10000 }) {
  const { privacy } = usePrivacy();
  const [revealed, setRevealed] = useState(false);
  const timerRef = useRef(null);
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  if (value == null) {
    return <span className={'font-mono tabular-nums text-gold-600 ' + className}>—</span>;
  }
  const masked = privacy || (level === 'click' && !revealed);
  const doReveal = () => {
    setRevealed(true);
    if (onReveal) onReveal();
    if (autoHide != null) {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setRevealed(false), autoHide);
    }
  };
  if (masked) {
    return (
      <span className={'inline-flex items-baseline gap-2 ' + className}>
        <span className="font-mono tabular-nums text-gold-600">{MASK_TEXT}</span>
        {!privacy && (
          <button onClick={doReveal}
            className="text-[11px] leading-none px-1.5 py-0.5 rounded-md text-slate-400
                       border border-slate-200 hover:text-brand-600 hover:border-brand-200
                       hover:bg-brand-50 transition-colors font-sans">
            显示
          </button>
        )}
        {privacy && <span className="text-[11px] text-slate-400 font-sans">隐私模式</span>}
      </span>
    );
  }
  return (
    <span className={'font-mono tabular-nums text-gold-600 amount-fade ' + className}>
      {prefix}{fmtMoney(value)}
    </span>
  );
}

/* A4 敏感提示行：always 级金额页（账单/订阅/理财）顶部统一文案 */
const SensitiveHint = () => {
  const { privacy } = usePrivacy();
  return (
    <div className="text-[11px] text-slate-400 flex items-center gap-1.5">
      <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" />
      {privacy
        ? '隐私模式已开启：金额已脱敏，可在顶栏关闭隐私模式查看'
        : '本页含敏感金额数据，仅本人可见，请勿截图外传'}
    </div>
  );
};'''
assert old_amount in s, 'Amount 旧实现未找到'
s = s.replace(old_amount, new_amount, 1)

# ---------- fade CSS ----------
old_css = "  .type-caret{"
s = s.replace(old_css, """  /* A2：金额 reveal / 静默回遮 fade 0.3s */
  @keyframes amountFade{ from{opacity:0;} to{opacity:1;} }
  .amount-fade{ animation:amountFade .3s ease; }
  .type-caret{""", 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
