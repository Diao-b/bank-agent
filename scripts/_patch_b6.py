# -*- coding: utf-8 -*-
"""B6 安全中心适配：规则文案 / 身份核验统计 / 密码核验日志"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\index.html'
s = io.open(P, encoding='utf-8').read()

old = """const RISK_RULES = [
  { icon:'shield',   title:'单笔超 ¥5,000 需验证码', desc:'大额资金变动必须通过短信验证码二次确认，防止误操作与盗用' },
  { icon:'lock',     title:'非常用收款人需验证',     desc:'向非白名单收款人转账时自动触发身份验证，首次收款人必验' },
  { icon:'alert-triangle', title:'高危业务拒绝代办', desc:'冻结/解冻、取消订阅等高风险操作，仅本人确认后执行，AI 不得代签' },
];"""
new = """const RISK_RULES = [
  { icon:'shield',   title:'验证码（你有）', desc:'验证码一律发送至站内收件箱（不依赖短信通道），本人登录后才可查看；大额 / 非常用收款人必验' },
  { icon:'lock',     title:'支付密码（你知）', desc:'6 位支付密码，错 3 次锁定至当日 23:59:59；密码不进对话、不落任何记录，只做服务端核验' },
  { icon:'alert-triangle', title:'风险分级组合', desc:'小额且常用：仅支付密码；超 2000 或非常用收款人：验证码+支付密码双要素；超 20000 另加大额警示行' },
  { icon:'alert-triangle', title:'高危业务拒绝代办', desc:'冻结/解冻、取消订阅等高风险操作，仅本人确认后执行，AI 不得代签' },
];"""
assert old in s
s = s.replace(old, new, 1)

old = """  verify_send:'发送验证码', verify_check:'校验验证码',
};"""
new = """  verify_send:'发送验证码', verify_check:'校验验证码',
  pay_password_verify:'支付密码核验',
};"""
assert old in s
s = s.replace(old, new, 1)

old = """  const total = logs.length;
  const failed = logs.filter(l => l.result === 'failed').length;
  const verified = logs.filter(l => l.action === 'verify_send' || l.action === 'verify_check').length;"""
new = """  const total = logs.length;
  const failed = logs.filter(l => l.result === 'failed').length;
  const verified = logs.filter(l => l.action === 'verify_send' || l.action === 'verify_check').length;
  /* B6：身份核验次数 = 验证码校验成功 + 支付密码核验成功 */
  const pwdOk = logs.filter(l => l.action === 'pay_password_verify' && l.result === 'success').length;
  const codeOk = logs.filter(l => l.action === 'verify_check' && l.result === 'success').length;
  const pwdLogs = logs.filter(l => l.action === 'pay_password_verify').slice(0, 10);"""
assert old in s
s = s.replace(old, new, 1)

old = """        <StatCard label="验证次数" value={verified} icon="lock" tone="amber" raw
                  sub="含验证码发送与校验" />"""
new = """        <StatCard label="身份核验次数" value={codeOk + pwdOk} icon="lock" tone="amber" raw
                  sub="验证码校验 + 支付密码核验（成功）" />"""
assert old in s
s = s.replace(old, new, 1)

old = """      {/* 操作日志时间线 */}
      <Panel title="操作日志" icon="history\""""
new = """      {/* B6：支付密码核验日志（含失败留痕） */}
      <Panel title="支付密码核验日志" icon="lock"
             right={<span className="text-[11px] text-slate-400">含失败留痕 · 最近 {pwdLogs.length} 条</span>}>
        {ls.loading ? <Loading /> : pwdLogs.length === 0
          ? <Empty text="暂无支付密码核验记录（首次转账输入密码后生成）" icon="lock" />
          : (
            <ul className="divide-y divide-slate-100">
              {pwdLogs.map(l => (
                <li key={l.id} className="flex items-center gap-3 py-2.5">
                  <span className="text-[12px] text-slate-400 font-mono shrink-0">
                    {l.created_at ? String(l.created_at).slice(5, 16).replace('T', ' ') : ''}
                  </span>
                  <span className={'text-[12px] font-medium ' + (l.result === 'success' ? 'text-emerald-600' : 'text-rose-500')}>
                    {RESULT_CN[l.result] || l.result}
                  </span>
                  <span className="text-[12px] text-slate-500 truncate flex-1">
                    {(() => {
                      try {
                        const d = JSON.parse(l.detail || '{}');
                        if (d.locked) return '触发锁定';
                        if (d.blocked) return '锁定状态拒绝';
                        return '常规核验';
                      } catch (e) { return ''; }
                    })()}
                  </span>
                </li>
              ))}
            </ul>
          )}
      </Panel>

      {/* 操作日志时间线 */}
      <Panel title="操作日志" icon="history\""""
assert old in s
s = s.replace(old, new, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
