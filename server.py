#!/usr/bin/env python3
"""
揽星阁 · 班级积分 — 后端服务器
提供静态文件服务 + REST API 实现多人数据共享
所有数据存储在 data.json 文件中（原子写入 + 自动备份）
"""
import json
import os
import time
import shutil
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')
BACKUP_FILE = DATA_FILE + '.bak'
HOST = '0.0.0.0'
PORT = 8080


class Handler(SimpleHTTPRequestHandler):
    """自定义请求处理器：GET 静态文件 + API 路由"""

    # ── 读取数据（多级容错）──
    def _load_data_from_disk(self):
        """尝试加载数据：主文件 → 备份文件 → None"""
        for path in (DATA_FILE, BACKUP_FILE):
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                if not raw.strip():
                    continue
                data = json.loads(raw)
                # 有效性校验
                if isinstance(data, dict) and 'students' in data and 'points' in data:
                    data.setdefault('dailyLogs', {})
                    data.setdefault('_version', 2)
                    return data, path
            except (json.JSONDecodeError, IOError, ValueError):
                print(f'[WARN] 数据文件损坏: {path}，尝试备选...')
                continue
        return None, None

    # ── API: 读取数据 ──
    def _api_get_data(self):
        data, source = self._load_data_from_disk()

        if data is None:
            # 完全无数据：返回空信号，由前端决定初始化
            # 绝不在此创建空文件！
            data = {'students': [], 'points': {}, 'dailyLogs': {}, '_version': 2, '_empty': True}
        else:
            data['_empty'] = False
            if source == BACKUP_FILE:
                print(f'[INFO] 主数据文件损坏，已从备份恢复')

        data['_serverTime'] = int(time.time() * 1000)
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    # ── API: 写入数据（原子操作）──
    def _api_post_data(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                raise ValueError('Empty body')
            raw = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(raw)

            # 基本校验
            if not isinstance(data, dict) or 'students' not in data:
                raise ValueError('Invalid data structure')
            if '_empty' in data:
                del data['_empty']

            # 防止误写空数据覆盖已有数据
            existing, _ = self._load_data_from_disk()
            if existing and existing.get('students') and (not data.get('students')):
                print(f'[WARN] 拒绝写入空学生列表（保护已有数据）')
                resp = {'ok': False, 'error': 'Refusing to overwrite with empty student list'}
                self.send_response(409)
                body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
                return

            # 原子写入
            self._atomic_write(data)
            resp = {'ok': True, 'time': int(time.time() * 1000)}
            self.send_response(200)

        except Exception as e:
            resp = {'ok': False, 'error': str(e)}
            self.send_response(400)
            print(f'[ERROR] POST 写入失败: {e}')

        body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── API: 检查数据版本（用于轮询）──
    def _api_check(self):
        mtime = int(os.path.getmtime(DATA_FILE) * 1000) if os.path.exists(DATA_FILE) else 0
        if mtime == 0 and os.path.exists(BACKUP_FILE):
            mtime = int(os.path.getmtime(BACKUP_FILE) * 1000)
        resp = {'mtime': mtime}
        body = json.dumps(resp).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── 路由分发 ──
    def do_GET(self):
        if self.path == '/api/data':
            return self._api_get_data()
        if self.path == '/api/check':
            return self._api_check()
        if self.path == '/' or self.path == '':
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        if self.path == '/api/data':
            return self._api_post_data()
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not Found')

    # ── 原子写入 ──
    def _atomic_write(self, data):
        """写临时文件 → 备份旧文件 → 原子重命名"""
        data.pop('_serverTime', None)
        data.pop('_empty', None)

        # 1. 写入临时文件
        dirname = os.path.dirname(DATA_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix='.data_tmp_', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())  # 确保刷盘

            # 2. 备份当前有效数据
            if os.path.exists(DATA_FILE):
                try:
                    shutil.copy2(DATA_FILE, BACKUP_FILE)
                except IOError:
                    pass  # 备份失败不阻塞

            # 3. 原子替换
            shutil.move(tmp_path, DATA_FILE)
            print(f'[{time.strftime("%H:%M:%S")}] 数据已保存 ({len(data.get("students",[]))} 名学生)')

        except Exception:
            # 清理临时文件
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def log_message(self, format, *args):
        if '/api/' in str(args[0]):
            pass  # 已在 _atomic_write 和错误处理中记录
        else:
            pass


if __name__ == '__main__':
    print(f'  揽星阁服务器 v2')
    print(f'  本地访问: http://localhost:{PORT}')
    print(f'  手机访问: http://<本机IP>:{PORT}')
    print(f'  数据文件: {DATA_FILE}')
    print(f'  备份文件: {BACKUP_FILE}')
    print(f'  按 Ctrl+C 停止')
    print()

    # 启动时检查数据完整性
    data, source = None, None
    try:
        data, source = Handler._load_data_from_disk(Handler)
    except:
        pass
    if data:
        n = len(data.get('students', []))
        print(f'  已加载数据: {n} 名学生 (来源: {os.path.basename(source)})')
    else:
        print(f'  未找到数据文件，等待前端首次初始化...')

    server = HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务器已停止')
        server.shutdown()
