"""An updated launcher must not present an existing older process as the new build."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from io import StringIO
import importlib
import json
from pathlib import Path
import shutil
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

import launch
from modules.app_version import APP_VERSION, BUILD_LABEL


class LauncherVersionTests(unittest.TestCase):
    def setUp(self):
        # Some dependencies probe Windows with `ver` on first import. Load them
        # before mocking subprocess creation so only launcher processes are counted.
        for name in ['streamlit','pandas','openpyxl','requests','xlrd']:
            importlib.import_module(name)
        self.root=Path(__file__).resolve().parents[1]/'.test-tmp'/('launcher-version-'+uuid.uuid4().hex)
        self.data=self.root/'data'
        self.data.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.root)
        self.marker=self.data/'application.json'

    def run_existing(self,version):
        state={'port':8502,'pid':12345,'root':str(self.root)}
        if version is not None: state['version']=version
        self.marker.write_text(json.dumps(state),encoding='utf-8')
        output=StringIO()
        with ExitStack() as stack, redirect_stdout(output):
            stack.enter_context(patch.object(launch,'ROOT',self.root))
            stack.enter_context(patch.dict(launch.os.environ,{'APPSTORE_DATA_DIR':str(self.data)}))
            stack.enter_context(patch.object(launch.sys,'argv',['launch.py']))
            stack.enter_context(patch.object(launch,'acquire_lock',return_value=None))
            browser=stack.enter_context(patch.object(launch.webbrowser,'open'))
            worker=stack.enter_context(patch('modules.monitoring.start_worker'))
            agent=stack.enter_context(patch('modules.agent_analysis.start_analysis_worker'))
            created=stack.enter_context(patch.object(launch.subprocess,'Popen'))
            result=launch.main()
            created.assert_not_called()
            worker.assert_not_called()
            agent.assert_not_called()
            self.assertFalse((self.data/'stop.request').exists())
            self.assertEqual(json.loads(self.marker.read_text(encoding='utf-8')),state)
            return result,output.getvalue(),browser.call_args

    def test_old_version_prompts_manual_stop_without_opening_or_stopping_process(self):
        result,output,browser=self.run_existing('2.3')
        self.assertEqual(result,1)
        self.assertIn('检测到旧版进程（2.3）',output)
        self.assertIn(APP_VERSION,output)
        self.assertIn(BUILD_LABEL,output)
        self.assertIn('停止舆情工作台.bat',output)
        self.assertIn('再重新启动',output)
        self.assertIsNone(browser)

    def test_missing_version_is_not_silently_reopened(self):
        result,output,browser=self.run_existing(None)
        self.assertEqual(result,1)
        self.assertIn('版本未知',output)
        self.assertIsNone(browser)

    def test_matching_version_reopens_existing_address(self):
        result,output,browser=self.run_existing(APP_VERSION)
        self.assertEqual(result,0)
        self.assertIn('工具已经运行',output)
        self.assertNotIn('旧版',output)
        self.assertEqual(browser.args,('http://127.0.0.1:8502',))

    def test_new_process_marker_and_startup_message_use_current_version(self):
        process=MagicMock(pid=6789,returncode=0)
        process.poll.side_effect=[None,0,0]
        response=MagicMock()
        response.__enter__.return_value.status=200
        lock=MagicMock()
        output=StringIO()
        with ExitStack() as stack, redirect_stdout(output):
            stack.enter_context(patch.object(launch,'ROOT',self.root))
            stack.enter_context(patch.dict(launch.os.environ,{'APPSTORE_DATA_DIR':str(self.data)}))
            stack.enter_context(patch.object(launch.sys,'argv',['launch.py','--no-browser']))
            stack.enter_context(patch.object(launch,'acquire_lock',return_value=lock))
            stack.enter_context(patch.object(launch,'available_port',return_value=8503))
            stack.enter_context(patch.object(launch.logging,'basicConfig'))
            stack.enter_context(patch('modules.review_store.init_db'))
            stack.enter_context(patch('modules.bundled_snapshot.ensure_bundled_snapshot'))
            stack.enter_context(patch('modules.monitoring.start_worker',return_value=threading.Event()))
            stack.enter_context(patch('modules.agent_analysis.start_analysis_worker',return_value=threading.Event()))
            stack.enter_context(patch('modules.agent_analysis.stop_analysis_worker',return_value=True))
            stack.enter_context(patch.object(launch.subprocess,'Popen',return_value=process))
            stack.enter_context(patch.object(launch.urllib.request,'urlopen',return_value=response))
            self.assertEqual(launch.main(),0)
        state=json.loads(self.marker.read_text(encoding='utf-8'))
        self.assertEqual(state['version'],APP_VERSION)
        self.assertEqual(state['port'],8503)
        self.assertIn(f'{APP_VERSION} · {BUILD_LABEL}',output.getvalue())
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        lock.close.assert_called_once()


if __name__=='__main__': unittest.main()
