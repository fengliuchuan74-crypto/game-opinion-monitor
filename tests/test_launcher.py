"""Launcher lifecycle checks, without starting Streamlit, collectors or Codex."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import launch


class LauncherLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root/'data'
        self.collector_stop = threading.Event()
        self.agent_stop = threading.Event()
        self.events = []
        self.process = MagicMock(pid=12345,returncode=0)
        self.process.poll.return_value = None
        self.process.terminate.side_effect = lambda:self.events.append('terminate')
        self.process.kill.side_effect = lambda:self.events.append('kill')

    def run_main(self, *, popen=None, wait=None):
        def join_agent(db_path, timeout):
            self.assertEqual(db_path,self.data/'reviews.sqlite3')
            self.assertTrue(self.collector_stop.is_set())
            self.assertTrue(self.agent_stop.is_set())
            self.assertEqual(timeout,20)
            self.events.append('agent_joined')
            return True

        def release_lock():
            self.assertIn('agent_joined',self.events)
            self.events.append('lock_closed')

        response = MagicMock()
        response.__enter__.return_value.status = 200
        with ExitStack() as patches, redirect_stdout(StringIO()):
            patches.enter_context(patch.object(launch,'ROOT',self.root))
            patches.enter_context(patch.dict(launch.os.environ,{'APPSTORE_DATA_DIR':str(self.data)}))
            patches.enter_context(patch.object(launch.sys,'argv',['launch.py','--no-browser']))
            patches.enter_context(patch.object(launch,'acquire_lock',return_value=SimpleNamespace(close=release_lock)))
            patches.enter_context(patch.object(launch,'available_port',return_value=8501))
            patches.enter_context(patch.object(launch.logging,'basicConfig'))
            patches.enter_context(patch.object(launch.logging,'exception'))
            patches.enter_context(patch('modules.review_store.init_db'))
            patches.enter_context(patch('modules.monitoring.start_worker',return_value=self.collector_stop))
            patches.enter_context(patch('modules.agent_analysis.start_analysis_worker',return_value=self.agent_stop))
            joined = patches.enter_context(patch('modules.agent_analysis.stop_analysis_worker',side_effect=join_agent))
            created = patches.enter_context(patch.object(launch.subprocess,'Popen',
                side_effect=popen,return_value=self.process))
            patches.enter_context(patch.object(launch.urllib.request,'urlopen',return_value=response))
            patches.enter_context(patch.object(launch.time,'sleep',side_effect=KeyboardInterrupt))
            if wait is not None: self.process.wait.side_effect = wait
            result = launch.main()
            joined.assert_called_once()
            self.assertEqual(created.call_args.kwargs['env']['APPSTORE_DISABLE_WORKER'],'1')
            self.assertEqual(self.events[-2:],['agent_joined','lock_closed'])
            return result

    def test_failed_server_spawn_cleans_up_both_started_workers(self):
        self.assertEqual(self.run_main(popen=OSError('Synthetic spawn failure')),1)
        self.process.terminate.assert_not_called()

    def test_stop_request_during_server_startup_runs_cleanup(self):
        def start(*args, **kwargs):
            (self.data/'stop.request').write_text('requested',encoding='utf-8')
            return self.process
        self.assertEqual(self.run_main(popen=start),0)
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=10)

    def test_keyboard_interrupt_waits_for_agent_before_unlock(self):
        self.assertEqual(self.run_main(),0)
        self.assertEqual(self.events,['terminate','agent_joined','lock_closed'])

    def test_unresponsive_server_is_killed_and_reaped_before_unlock(self):
        self.assertEqual(self.run_main(wait=[subprocess.TimeoutExpired('streamlit',10),0]),0)
        self.assertEqual(self.events,['terminate','kill','agent_joined','lock_closed'])
        self.assertEqual(self.process.wait.call_count,2)
        self.assertEqual(self.process.wait.call_args.kwargs['timeout'],5)


if __name__=='__main__':
    unittest.main()
