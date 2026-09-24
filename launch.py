"""Portable launcher owning the local server and background worker lifecycles."""
from __future__ import annotations
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))


def acquire_lock(path):
    handle=path.open('a+b')
    try:
        # Windows byte locks also deny reads; never read an already locked byte.
        handle.seek(0,os.SEEK_END)
        if handle.tell()==0: handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def available_port(preferred=8501):
    for port in range(preferred,preferred+30):
        with socket.socket() as connection:
            try: connection.bind(('127.0.0.1',port)); return port
            except OSError: continue
    raise RuntimeError('没有可用本地端口，请关闭不再使用的工具后重试')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--port',type=int,default=8501)
    parser.add_argument('--stop',action='store_true')
    args=parser.parse_args()
    try:
        import streamlit, pandas, openpyxl, requests, xlrd
        from modules.review_store import init_db
        from modules.bundled_snapshot import ensure_bundled_snapshot
        from modules.monitoring import start_worker
        from modules.agent_analysis import start_analysis_worker, stop_analysis_worker
    except ImportError as exc:
        print('运行环境不完整，请用当前 Python 执行 -m pip install -r requirements.lock.txt。缺少依赖：',exc)
        return 1
    data=Path(os.environ.get('APPSTORE_DATA_DIR',str(ROOT/'data')))
    data.mkdir(parents=True,exist_ok=True)
    stop_request=data/'stop.request'
    if args.stop:
        stop_request.write_text('requested',encoding='utf-8')
        print('已请求停止本项目工作台；将在几秒内退出。')
        return 0
    lock=acquire_lock(data/'application.lock')
    marker=data/'application.json'
    if lock is None:
        if marker.exists():
            try:
                state=json.loads(marker.read_text(encoding='utf-8'))
                port=int(state['port'])
                if 1024<=port<=65535:
                    print(f'工具已经运行：http://127.0.0.1:{port}')
                    if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{port}')
            except (ValueError,KeyError,OSError): print('工具正在启动，请稍候。')
        return 0
    process=stop=agent_stop=None
    path=None
    try:
        stop_request.unlink(missing_ok=True)
        logs=ROOT/'outputs'/'runtime_logs'; logs.mkdir(parents=True,exist_ok=True)
        path=logs/f"app_{datetime.now():%Y%m%d_%H%M%S}.log"
        logging.basicConfig(filename=path,encoding='utf-8',level=logging.INFO)
        port=available_port(args.port)
        init_db(data/'reviews.sqlite3')
        ensure_bundled_snapshot(data/'reviews.sqlite3',ROOT/'bundled_data')
        stop=start_worker(data/'reviews.sqlite3',ROOT/'outputs'/'collector_logs')
        agent_stop=start_analysis_worker(data/'reviews.sqlite3',ROOT/'outputs'/'agent_analysis')
        env=os.environ.copy(); env['APPSTORE_DISABLE_WORKER']='1'; env['PYTHONUTF8']='1'
        with path.open('a',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,'-B','-m','streamlit','run',str(ROOT/'app.py'),
                '--server.address','127.0.0.1','--server.port',str(port),'--server.headless','true',
                '--server.fileWatcherType','none','--browser.gatherUsageStats','false'],cwd=ROOT,env=env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            marker.write_text(json.dumps({'port':port,'pid':process.pid,'version':'2.3','root':str(ROOT)},ensure_ascii=False),encoding='utf-8')
            ready=False
            for _ in range(120):
                if stop_request.exists(): return 0
                if process.poll() is not None: break
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/_stcore/health',timeout=1) as response:
                        ready=response.status==200
                    if ready: break
                except OSError: pass
                time.sleep(.25)
            if not ready:
                print('启动未成功，请查看日志：',path)
                return 1
            print(f'App Store 舆情工作台：http://127.0.0.1:{port}\n关闭浏览器不影响巡检；在本窗口按 Ctrl+C 停止。\n日志：{path}')
            if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{port}')
            while process.poll() is None:
                if stop_request.exists():
                    stop_request.unlink(missing_ok=True)
                    print('已停止本项目工作台。')
                    return 0
                time.sleep(.5)
            return process.returncode
    except KeyboardInterrupt:
        return 0
    except OSError:
        logging.exception('Workbench startup or runtime failed')
        print('工作台运行未完成，请查看日志：',path or '无法创建日志文件')
        return 1
    finally:
        if stop is not None: stop.set()
        if agent_stop is not None: agent_stop.set()
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            # A daemon worker must finish its transport's terminate/kill cleanup
            # before the launcher exits and releases the single-instance lock.
            try:
                if agent_stop is not None and not stop_analysis_worker(data/'reviews.sqlite3',timeout=20):
                    logging.warning('Agent worker did not finish cleanup within 20 seconds')
            finally:
                lock.close()


if __name__=='__main__':
    raise SystemExit(main())
