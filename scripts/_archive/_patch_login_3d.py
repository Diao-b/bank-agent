# -*- coding: utf-8 -*-
# login.html → 黑金 3D 舞台 改造脚本（状态机重写 + 渲染层换肤）
import io

p = 'login.html'
s = io.open(p, encoding='utf-8').read()

old_state_start = s.index("function LoginPage() {")
old_state_end = s.index("  const split = view !== 'full';")
new_state = '''function LoginPage() {
  const [view, setView] = useState('full'); // full | login | register
  const [stage, setStage] = useState(1);
  const [logLines, setLogLines] = useState([]);
  const [latency, setLatency] = useState(12);

  const loopStart = useRef(0);
  const logId = useRef(0);

  // 已登录 → 直接进 /chat
  useEffect(() => {
    if (localStorage.getItem('xb_token')) {
      const t = location.pathname;
      window.location.href = t.endsWith('.html') ? 'index.html' : '/chat';
    }
  }, []);

  // LATENCY 每 3 秒微变
  useEffect(() => {
    const id = setInterval(() => setLatency(8 + Math.floor(Math.random() * 13)), 3000);
    return () => clearInterval(id);
  }, []);

  const pushLog = useCallback((msg) => {
    const t = performance.now() - loopStart.current;
    logId.current += 1;
    setLogLines((prev) => [...prev, { id: logId.current, text: '[' + fmtTime(t) + '] ' + msg }]);
  }, []);

  // 叙事状态机：沿用原 flow-steps 的 16s 时序与文案（卸载时清理）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      while (!cancelled) {
        loopStart.current = performance.now();
        if (cancelled) break;
        setStage(1); pushLog('user → agent · message received');
        await sleep(1500);
        if (cancelled) break;
        setStage(2); pushLog('agent · parsing intent');
        await sleep(2000);
        if (cancelled) break;
        setStage(3); pushLog('agent → tool · call get_balance');
        await sleep(2200);
        if (cancelled) break;
        setStage(4); pushLog('agent → mcp · context sync');
        await sleep(2200);
        if (cancelled) break;
        setStage(5); pushLog('agent · merging results');
        await sleep(1500);
        if (cancelled) break;
        setStage(6); pushLog('agent → user · response sent');
        await sleep(1500);
        if (cancelled) break;
        setStage(7); pushLog('user → agent · confirm received');
        await sleep(1500);
        if (cancelled) break;
        setStage(8); pushLog('agent → user · action executed');
        await sleep(2500);
        if (cancelled) break;
        setStage(9); pushLog('loop · done, restarting');
        await sleep(800);
      }
    };
    run();
    return () => { cancelled = true; };
  }, []);

'''
s = s[:old_state_start] + new_state + s[old_state_end:]

# --- render patch: root bg + stars ---
s = s.replace(
  "style={{ background: 'linear-gradient(180deg, #050810 0%, #0A1128 45%, #0B1B3A 100%)', minWidth: 1024 }}>",
  "style={{ background: 'linear-gradient(180deg, #06090F 0%, #0A0F18 55%, #0D131D 100%)', minWidth: 1024 }}>")
s = s.replace('<div className="aurora aurora-1" />',
              '<div className="absolute inset-0 stars pointer-events-none" />\n      <div className="aurora aurora-1" />', 1)

# login button gold
s = s.replace(
  'className="rounded-lg text-[14px] font-semibold text-white bg-gradient-to-r from-blue-500 to-indigo-500 hover:brightness-110 hover:scale-[1.02] active:scale-[0.98] transition"',
  'className="rounded-lg text-[14px] font-semibold hover:brightness-110 hover:scale-[1.02] active:scale-[0.98] transition"')
s = s.replace(
  "style={{ width: 120, height: 40, boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.18)' }}>登 录</button>",
  "style={{ width: 120, height: 40, color: '#1A1408',\n                         backgroundImage: 'linear-gradient(135deg,#E8C460 0%,#D4A017 55%,#B8860B 100%)',\n                         boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.25), 0 4px 14px rgba(212,160,23,0.25)' }}>登 录</button>")

# left pane: padding + scale 0.7
s = s.replace("paddingTop: 96 }}", "paddingTop: 72 }}")
s = s.replace("transform: split ? 'scale(0.5)' : 'scale(1)' }}", "transform: split ? 'scale(0.7)' : 'scale(1)' }}")

# title gradient gold
s = s.replace(
  '<span className="bg-gradient-to-r from-blue-500 to-indigo-500 bg-clip-text text-transparent">小微</span>',
  '<span className="bg-clip-text text-transparent" style={{ backgroundImage: \'linear-gradient(135deg,#E8C460 0%,#D4A017 55%,#B8860B 100%)\' }}>小微</span>')
s = s.replace("color: '#9DB2D6' }}", "color: '#B9AE8E' }}")
s = s.replace('<div className="text-[32px] font-bold leading-tight" style={{ marginTop: 8 }}>',
              '<div className="text-[32px] font-semibold leading-tight" style={{ marginTop: 8 }}>')

# central animation → 3D stage
old_center = '''            {/* 中央架构动画：外层容器放大 1.25，内部 900×460 坐标不动 */}
            <div style={{ marginTop: 40, width: 1125, height: 575 }} className="flex justify-center">
              <div style={{ width: 900, height: 460, transform: 'scale(1.25)', transformOrigin: 'center top' }}>
                <ArchitectureLoop stage={stage} flowColor={flowColor} activeNodes={activeNodes}
                                  processing={processing} flight={flight} pulse={pulse} />
              </div>
            </div>

            {/* 阶段描述器：架构图正下方 32px */}
            <StageIndicator stage={stage} />

            <div className="text-[11px]" style={{ marginTop: 32, color: 'rgba(255,255,255,0.3)' }}>演示环境 · 所有数据均为模拟</div>'''
new_center = '''            {/* 3D 立体旋转流程舞台 */}
            <div style={{ marginTop: 8 }} className="flex justify-center">
              <FlowStage3D activeCard={STAGE_TO_CARD[stage]} />
            </div>

            {/* 阶段描述器：舞台正下方（与卡片点亮同步） */}
            <StageIndicator stage={stage} />

            <div className="text-[11px]" style={{ marginTop: 20, color: 'rgba(255,255,255,0.3)' }}>演示环境 · 所有数据均为模拟 · 拖拽可旋转</div>'''
assert old_center in s, 'center block not found'
s = s.replace(old_center, new_center)

# right pane gold accents
s = s.replace("background: split ? 'rgba(10,17,40,0.75)' : 'transparent',",
              "background: split ? 'rgba(10,15,24,0.78)' : 'transparent',")
s = s.replace("borderLeft: split ? '1px solid rgba(255,255,255,0.10)' : 'none',",
              "borderLeft: split ? '1px solid rgba(212,160,23,0.18)' : 'none',")

# --- AuthPanel gold accents ---
s = s.replace(
  "focus:border-blue-500 focus:ring-2 focus:ring-blue-500/40 transition';",
  "focus:border-[#D4A017] focus:ring-2 focus:ring-[rgba(212,160,23,0.35)] transition';")
s = s.replace(
  "const submitCls = 'w-full h-12 rounded-lg text-[15px] font-semibold text-white bg-gradient-to-r from-blue-500 to-indigo-500 hover:brightness-110 transition disabled:opacity-60';",
  "const submitCls = 'w-full h-12 rounded-lg text-[15px] font-semibold text-[#1A1408] hover:brightness-110 transition disabled:opacity-60';")
s = s.replace(
  "const submitStyle = { boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.18)' };",
  "const submitStyle = { backgroundImage: 'linear-gradient(135deg,#E8C460 0%,#D4A017 55%,#B8860B 100%)', boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.25), 0 4px 14px rgba(212,160,23,0.25)' };")
s = s.replace(
  "style={{ width: 8, height: 8, background: 'linear-gradient(135deg, #3B82F6, #6366F1)', boxShadow: '0 0 8px rgba(99,102,241,0.7)' }} />",
  "style={{ width: 8, height: 8, background: 'linear-gradient(135deg,#E8C460,#D4A017)', boxShadow: '0 0 8px rgba(212,160,23,0.7)' }} />")
s = s.replace(
  'className="rounded-lg text-[14px] font-bold text-white">小微</span>',
  'className="rounded-lg text-[14px] font-semibold text-[#F0D590]">小微</span>')

io.open(p, 'w', encoding='utf-8', newline='').write(s)
print('PATCH_OK')
