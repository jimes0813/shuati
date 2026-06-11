#!/usr/bin/env python3
"""刷题小程序本地服务器(零第三方依赖, docx解析用python-docx)
启动: python3 server.py [端口]   默认 8787
"""
import json, os, re, sys, tempfile, threading, webbrowser
import urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import import_docx

FROZEN = getattr(sys, 'frozen', False)          # PyInstaller 打包标志
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
# 静态文件: 优先用 exe/脚本同目录下的 static 文件夹(方便只更新前端), 否则用打进exe的
_EXT_STATIC = os.path.join(ROOT, 'static')
STATIC = _EXT_STATIC if os.path.isdir(_EXT_STATIC) else os.path.join(getattr(sys, '_MEIPASS', ROOT), 'static')
BANK = os.path.join(ROOT, 'data', 'bank.json')
PROG = os.path.join(ROOT, 'data', 'progress.json')
os.makedirs(os.path.join(ROOT, 'data'), exist_ok=True)
LOCK = threading.Lock()

# ---- AI 判卷: searxng 联网搜索 + 可切换的本地/云端模型 ----
CONF = os.path.join(ROOT, 'data', 'config.json')
DEFAULT_CONF = {
    'provider': 'cloud',   # cloud=DeepSeek(经codex-bridge) | local=llama.cpp
    'cloud': {'base': 'http://127.0.0.1:4000/v1', 'model': 'deepseek-v4-flash',
              'auth': 'bridge', 'label': '云端 DeepSeek', 'api_key': ''},
    'local': {'base': 'http://127.0.0.1:8080/v1', 'model': 'local',
              'auth': 'none', 'label': '本地 Qwen', 'api_key': ''},
    'searx': 'http://127.0.0.1:9999/search',   # 置空则跳过联网搜索
}
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机调用不走代理


def get_conf():
    c = json.loads(json.dumps(DEFAULT_CONF))
    s = load(CONF, {})
    if s.get('provider') in ('cloud', 'local'):
        c['provider'] = s['provider']
    for k in ('cloud', 'local'):
        if isinstance(s.get(k), dict):
            c[k].update(s[k])
    return c


def _provider_key(p):
    """key 优先级: 用户配置 > codex-bridge 的 .env(云端兜底)"""
    return p.get('api_key') or (_bridge_key() if p.get('auth') == 'bridge' else '') or 'none'


def _bridge_key():
    try:
        env = open(os.path.expanduser('~/.codex/codex-bridge/.env'), encoding='utf-8').read()
        m = re.search(r'^PROXY_AUTH_KEY=(.+)$', env, re.M)
        return m.group(1).strip() if m else ''
    except Exception:
        return ''


def llm_chat(prompt, timeout=180):
    """按当前配置的 provider 调 OpenAI 兼容接口, 返回 content"""
    conf = get_conf()
    p = conf[conf['provider']]
    body = json.dumps({'model': p['model'], 'temperature': 0.2,
                       'messages': [{'role': 'user', 'content': prompt}]}).encode()
    req = urllib.request.Request(p['base'] + '/chat/completions', data=body, headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _provider_key(p)})
    with _NOPROXY.open(req, timeout=timeout) as r:
        return json.load(r)['choices'][0]['message']['content']


def provider_alive(p):
    key = _provider_key(p)
    # 先试 /models(便宜); 鉴权失败直接判离线; 不支持/models的服务再退到最小chat测试
    try:
        req = urllib.request.Request(p['base'] + '/models',
                                     headers={'Authorization': 'Bearer ' + key})
        return 200 <= _NOPROXY.open(req, timeout=6).getcode() < 300
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
    except Exception:
        pass
    try:
        body = json.dumps({'model': p['model'], 'max_tokens': 1,
                           'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
        req = urllib.request.Request(p['base'] + '/chat/completions', data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        return 200 <= _NOPROXY.open(req, timeout=8).getcode() < 300
    except Exception:
        return False


def test_provider(which):
    """真实连接测试: 发一条极小请求, 返回成功回显或详细错误(供用户诊断'连不通')"""
    conf = get_conf()
    p = conf.get(which)
    if not p:
        return {'ok': False, 'error': '未知的模型类型'}
    key = _provider_key(p)
    info = f"接口 {p['base']}  模型 {p['model']}  鉴权 {'有Key' if key != 'none' else '无Key'}"
    try:
        body = json.dumps({'model': p['model'], 'max_tokens': 5,
                           'messages': [{'role': 'user', 'content': '回复两个字：正常'}]}).encode()
        req = urllib.request.Request(p['base'] + '/chat/completions', data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with _NOPROXY.open(req, timeout=20) as r:
            reply = json.load(r)['choices'][0]['message'].get('content', '')
        return {'ok': True, 'info': info, 'reply': (reply or '').strip()[:50]}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:300]
        except Exception:
            pass
        hint = '（API Key 不对或未填）' if e.code in (401, 403) else \
               '（模型名不对）' if e.code == 404 else ''
        return {'ok': False, 'info': info, 'error': f'HTTP {e.code} {hint} {detail}'}
    except Exception as e:
        return {'ok': False, 'info': info,
                'error': f'{type(e).__name__}: {e}（接口地址不通或网络问题）'}


def web_search(query, n=4):
    searx = get_conf().get('searx', '')
    if not searx:
        return []
    try:
        url = searx + '?' + urllib.parse.urlencode({'q': query[:90], 'format': 'json'})
        with _NOPROXY.open(url, timeout=10) as r:
            results = json.load(r).get('results', [])[:n]
        return [{'title': x.get('title', ''), 'content': x.get('content', '')[:300]}
                for x in results]
    except Exception:
        return []


def _search_ctx(stem, parts):
    snippets = web_search('一级建造师机电 ' + stem + ' ' + ' '.join(parts)[:50])
    ctx = '\n'.join(f"[资料{i+1}] {s['title']}: {s['content']}"
                    for i, s in enumerate(snippets))
    return snippets, ctx


def gen_answer(stem, parts):
    """联网搜索+LLM 生成参考答案(逐小问要点)"""
    snippets, ctx = _search_ctx(stem, parts)
    q_text = stem + ('\n' + '\n'.join(parts) if parts else '')
    prompt = f"""你是一级建造师机电实务的资深讲师。给出下面问答题的标准参考答案。

【题目】
{q_text}

【联网检索资料】
{ctx or '(联网搜索不可用,凭你的专业知识作答)'}

要求：逐小问给出答案要点，含规范要求的具体数值；语言精炼像教材划重点；纯文本输出（不要markdown标记），每个小问一行，形如"（1）…"。"""
    return llm_chat(prompt), len(snippets)


def gen_explain(q):
    """针对一道题生成"标准知识点讲解": 联网搜索 + LLM, 讲清考点而非只给答案"""
    stem = q.get('stem', '')
    parts = q.get('parts', [])
    snippets, ctx = _search_ctx(stem, parts)
    if q.get('type') == 'qa':
        qline = stem + ('\n' + '\n'.join(parts) if parts else '')
        ansline = ('参考答案：' + q['ref']) if q.get('ref') else ''
    else:
        opts = '\n'.join(f"{k}. {v}" for k, v in (q.get('options') or {}).items())
        qline = stem + '\n' + opts
        ansline = '正确答案：' + ''.join(q.get('answer') or []) + \
                  (('\n原解析：' + q['explanation']) if q.get('explanation') else '')
    prompt = f"""你是一级建造师机电实务的资深讲师。下面这道题考生反复做错，请围绕它做"知识点精讲"，帮考生彻底吃透这个考点。

【题目】
{qline}
{ansline}

【联网检索资料】
{ctx or '(联网搜索不可用,凭你的专业知识讲解)'}

请按以下结构输出纯文本（不要markdown符号），简明扼要：
一、核心考点：这道题考的是什么知识点。
二、必记要点：相关规范的关键数值/条件/分类（分条列出）。
三、易错提醒：常见混淆点和这道题的陷阱。
四、记忆口诀或方法：帮助记住的小技巧。"""
    return llm_chat(prompt), len(snippets)


def ai_judge(stem, parts, user_answer, ref=''):
    snippets, ctx = _search_ctx(stem, parts)
    q_text = stem + ('\n' + '\n'.join(parts) if parts else '')
    prompt = f"""你是一级建造师机电实务的判卷老师。根据规范知识和下面的联网检索资料，批改考生对问答题的回答。

【题目】
{q_text}
{'【参考答案】' + ref if ref else ''}
【考生回答】
{user_answer}

【联网检索资料】
{ctx or '(联网搜索不可用,凭你的专业知识判卷)'}

要求：逐小问对照，指出答对的点、答错/漏答的点并给出正确说法（含具体数值/规范要求）。
只输出 JSON（不要代码块）：{{"score": 0到100整数, "verdict": "正确"或"部分正确"或"错误", "comment": "简洁的逐点批改，200字内"}}"""
    content = llm_chat(prompt)
    m = re.search(r'\{.*\}', content, re.S)
    out = json.loads(m.group(0))
    out['searched'] = len(snippets)
    return out


def load(path, default):
    try:
        with open(path, encoding='utf-8') as f:   # 必须指定utf-8: Windows中文默认GBK会读崩UTF-8文件
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:    # 同上, 否则Windows下写GBK导致跨机/再读出错
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/bank':
            return self._json(load(BANK, []))
        if self.path == '/api/progress':
            return self._json(load(PROG, {}))
        if self.path == '/api/config':
            c = get_conf()
            out = {'provider': c['provider']}
            for k in ('cloud', 'local'):
                p, key = c[k], c[k].get('api_key', '')
                out[k] = {'label': p['label'], 'model': p['model'], 'base': p['base'],
                          'has_key': bool(key),
                          'key_hint': ('••••' + key[-4:]) if len(key) >= 8 else ('已设置' if key else ''),
                          'fallback': '自动用本机 codex-bridge 的 key' if (p.get('auth') == 'bridge' and not key and _bridge_key()) else ''}
            return self._json(out)
        if self.path == '/api/aistatus':
            c = get_conf()
            return self._json({'cloud': provider_alive(c['cloud']),
                               'local': provider_alive(c['local'])})
        if self.path.startswith('/api/testai'):
            q = urllib.parse.urlparse(self.path).query
            which = urllib.parse.parse_qs(q).get('provider', ['cloud'])[0]
            return self._json(test_provider(which))
        super().do_GET()

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n)
        if self.path == '/api/progress':
            with LOCK:
                save(PROG, json.loads(body))
            return self._json({'ok': True})
        if self.path == '/api/config':
            d = json.loads(body)
            with LOCK:
                c = get_conf()
                if d.get('provider') in ('cloud', 'local'):
                    c['provider'] = d['provider']
                for k in ('cloud', 'local'):
                    upd = d.get(k)
                    if not isinstance(upd, dict):
                        continue
                    for f in ('base', 'model', 'label'):
                        if isinstance(upd.get(f), str) and upd[f].strip():
                            c[k][f] = upd[f].strip()
                    ak = upd.get('api_key')
                    if isinstance(ak, str) and ak.strip():   # 空串=保持不变
                        c[k]['api_key'] = '' if ak.strip() == '__clear__' else ak.strip()
                save(CONF, c)
            return self._json({'ok': True, 'provider': c['provider']})
        if self.path == '/api/answer':
            d = json.loads(body)
            with LOCK:
                bank = load(BANK, [])
                q = next((x for x in bank if x['id'] == d.get('id')), None)
            if not q:
                return self._json({'ok': False, 'error': '题目不存在'})
            if q.get('ref'):
                return self._json({'ok': True, 'ref': q['ref'], 'cached': True,
                                   'ai_generated': q.get('ref_ai', False)})
            try:
                ref, searched = gen_answer(q['stem'], q.get('parts', []))
            except Exception as e:
                return self._json({'ok': False, 'error': f'{type(e).__name__}: {e}'})
            with LOCK:   # 缓存进题库, 下次秒出
                bank = load(BANK, [])
                for x in bank:
                    if x['id'] == q['id']:
                        x['ref'] = ref
                        x['ref_ai'] = True
                save(BANK, bank)
            return self._json({'ok': True, 'ref': ref, 'cached': False,
                               'ai_generated': True, 'searched': searched})
        if self.path == '/api/judge':
            d = json.loads(body)
            try:
                out = ai_judge(d.get('stem', ''), d.get('parts', []),
                               d.get('user_answer', ''), d.get('ref', ''))
                out['ok'] = True
                return self._json(out)
            except Exception as e:
                return self._json({'ok': False, 'error': f'{type(e).__name__}: {e}'})
        if self.path == '/api/explain':
            # 知识点精讲: body = {"id": "xxx"}; 结果缓存进 bank 的 q.kp
            d = json.loads(body)
            with LOCK:
                bank = load(BANK, [])
                q = next((x for x in bank if x['id'] == d.get('id')), None)
            if not q:
                return self._json({'ok': False, 'error': '题目不存在'})
            if q.get('kp'):
                return self._json({'ok': True, 'kp': q['kp'], 'cached': True})
            try:
                kp, searched = gen_explain(q)
            except Exception as e:
                return self._json({'ok': False, 'error': f'{type(e).__name__}: {e}'})
            with LOCK:
                bank = load(BANK, [])
                for x in bank:
                    if x['id'] == q['id']:
                        x['kp'] = kp
                save(BANK, bank)
            return self._json({'ok': True, 'kp': kp, 'cached': False, 'searched': searched})
        if self.path == '/api/delete':
            # 批量删除题目: body = {"ids": ["xxx", ...]}
            d = json.loads(body)
            ids = set(d.get('ids') or [])
            if not ids:
                return self._json({'ok': False, 'error': '未指定要删除的题目'}, 400)
            with LOCK:
                bank = load(BANK, [])
                before = len(bank)
                bank = [q for q in bank if q.get('id') not in ids]
                save(BANK, bank)
            return self._json({'ok': True, 'deleted': before - len(bank),
                               'total': len(bank)})
        if self.path == '/api/import':
            # 前端逐个文件以原始字节上传, 文件名在 X-Filename(URL编码)
            from urllib.parse import unquote
            name = unquote(self.headers.get('X-Filename', 'upload.docx'))
            with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
                f.write(body)
                tmp = f.name
            try:
                qs = import_docx.parse_docx(tmp)
                for q in qs:
                    q['source'] = name
                with LOCK:
                    bank = load(BANK, [])
                    added, updated = import_docx.merge_into_bank(bank, qs)
                    save(BANK, bank)
                return self._json({'ok': True, 'parsed': len(qs), 'added': added,
                                   'updated': updated, 'total': len(bank)})
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            finally:
                os.unlink(tmp)
        self._json({'ok': False, 'error': 'not found'}, 404)


if __name__ == '__main__':
    import socket
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    host = '0.0.0.0'   # 监听所有网卡，手机可访问
    # 获取本机局域网 IP
    lan_ip = '127.0.0.1'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    local_url = f'http://127.0.0.1:{port}'
    lan_url = f'http://{lan_ip}:{port}'
    print(f'刷题营已启动:')
    print(f'  本机访问: {local_url}')
    print(f'  手机访问: {lan_url}  (同一 WiFi 下)')
    print(f'  关闭本窗口即退出')
    if FROZEN:
        threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
