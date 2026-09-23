# -*- coding: utf-8 -*-
"""login.html：删除装饰假统计（C5-验收#8 选择"删除"）、登录重置隐私、admin 跳转"""
import io

P = r'D:\Users\Dwm\Desktop\agaent\bank-agent\web\login.html'
s = io.open(P, encoding='utf-8').read()

# 1) 删除 Counters / RollingNumber 装饰统计（假数据终结：直接删除而非接真数据，
#    登录页不承担统计职能，真实聚合在 /admin 概览页）
i0 = s.index('function RollingNumber({ value, className }) {')
i1 = s.index('/* ===================== 登录 / 注册面板 ===================== */')
s = s[:i0] + '''/* 旧右下角 MESSAGES / TOOL CALLS / MCP SYNC 装饰统计已删除：
   它们是随机生成的演示数字（假数据），与真实业务无关；
   真实聚合统计统一在 /admin 管理后台概览页查看。 */

''' + s[i1:]

# 渲染处
old = "          <Counters counters={counters} />"
assert old in s
s = s.replace(old, '', 1)

# state 与轮询
old = """  const [counters, setCounters] = useState(() => ({"""
i0 = s.index(old)
# 找到该 useState 结束（以 "));" 结尾的三行块）
i1 = s.index('));', i0) + 3
s = s[:i0] + '/* 装饰统计已删除 */' + s[i1:]

old = """        setCounters((c) => ({ ...c, messages: c.messages + 1, toolCalls: c.toolCalls + 1, mcpSync: c.mcpSync + 1 }));
"""
assert old in s
s = s.replace(old, '', 1)

# 2) 登录/注册/游客成功：A3 隐私模式强制重置为"开" + admin 默认跳 /admin
old = """  const go = () => {
    // 无论通过 / 还是 /login 进入，登录/游客成功后统一进入 /chat（由服务端路由返回聊天页）
    window.location.href = '/chat';
  };"""
new = """  const go = (role) => {
    // A3：登录/新会话强制重置隐私模式为"开"
    localStorage.removeItem('xb_privacy');
    // C5：管理员登录默认进管理后台；普通用户进用户端
    window.location.href = role === 'admin' ? '/admin' : '/chat';
  };"""
assert old in s
s = s.replace(old, new, 1)

old = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', (res.user && res.user.username) || login.username);
      go();"""
new = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', (res.user && res.user.username) || login.username);
      go((res.user && res.user.role) || 'user');"""
assert old in s
s = s.replace(old, new, 1)

old = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', 'guest');
      go();"""
new = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', 'guest');
      go('user');"""
assert old in s
s = s.replace(old, new, 1)

old = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', reg.username);
      go();"""
new = """      localStorage.setItem('xb_token', res.token);
      localStorage.setItem('xb_user', reg.username);
      go('user');"""
assert old in s
s = s.replace(old, new, 1)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('OK')
