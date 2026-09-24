"""Codex process-contract checks using fake streams; no CLI or network calls."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from modules import codex_runner


class FakeProcess:
    def __init__(self, *, result=None, returncode=0, stderr='', stop_requires_kill=False):
        events = [dict(type='turn.started')]
        if result is not None:
            events += [dict(type='item.completed',item=dict(type='agent_message',text=result)),
                       dict(type='turn.completed',usage={'input_tokens':10,'output_tokens':5})]
        self.stdin = io.StringIO()
        self.stdout = io.StringIO('\n'.join(json.dumps(event) for event in events)+'\n')
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.stop_requires_kill = stop_requires_kill
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stop_requires_kill: self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fixture-codex',timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class CodexRunnerTests(unittest.TestCase):
    schema = {'type':'object','properties':{'value':{'type':'integer'}},
              'required':['value'],'additionalProperties':False}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for name,value in [('find_codex','fixture-codex.exe'),('configured_model','fixture-model'),('_config',{})]:
            patched = patch.object(codex_runner,name,return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_validated_output_and_usage_are_returned_without_shell_execution(self):
        process = FakeProcess(result='{"value":7}')
        progress = []
        with patch.object(codex_runner.subprocess,'Popen',return_value=process) as popen:
            result = codex_runner.run_codex('只读测试快照',self.schema,self.folder,on_progress=progress.append)
        self.assertEqual(result['output'],{'value':7})
        self.assertEqual(result['model'],'fixture-model')
        self.assertEqual(result['usage']['output_tokens'],5)
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[0],'fixture-codex.exe')
        self.assertEqual(command[command.index('--sandbox')+1],'read-only')
        self.assertFalse(options.get('shell',False))
        self.assertNotIn('CODEX_THREAD_ID',options['env'])
        self.assertNotIn('CODEX_TURN_ID',options['env'])
        self.assertTrue(progress)
        self.assertEqual(len(list(self.folder.glob('*.usage.json'))),1)

    def test_timeout_terminates_and_kills_an_unresponsive_process(self):
        process = FakeProcess(returncode=None,stop_requires_kill=True)
        with patch.object(codex_runner.subprocess,'Popen',return_value=process), \
                patch.object(codex_runner.time,'monotonic',side_effect=[0,1000]):
            with self.assertRaisesRegex(codex_runner.CodexError,'超时'):
                codex_runner.run_codex('fixture',self.schema,self.folder,timeout=1)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(list(self.folder.glob('*.usage.json')))

    def test_cancellation_after_start_stops_the_child(self):
        process = FakeProcess(returncode=None)
        cancelled = Mock(side_effect=[False,True])
        with patch.object(codex_runner.subprocess,'Popen',return_value=process):
            with self.assertRaises(codex_runner.AnalysisCancelled):
                codex_runner.run_codex('fixture',self.schema,self.folder,cancelled=cancelled)
        self.assertTrue(process.terminated)

    def test_pre_cancelled_run_never_starts_a_process(self):
        with patch.object(codex_runner.subprocess,'Popen') as popen:
            with self.assertRaises(codex_runner.AnalysisCancelled):
                codex_runner.run_codex('fixture',self.schema,self.folder,cancelled=lambda:True)
        popen.assert_not_called()

    def test_bad_json_and_wrong_schema_are_not_exposed_or_saved_as_results(self):
        for output in ('not-json secret-provider-output', '{"value":"secret-provider-output"}'):
            with self.subTest(output=output):
                process = FakeProcess(result=output)
                with patch.object(codex_runner.subprocess,'Popen',return_value=process):
                    with self.assertRaises(codex_runner.CodexError) as failure:
                        codex_runner.run_codex('fixture',self.schema,self.folder)
                self.assertIn('格式未通过校验',str(failure.exception))
                self.assertNotIn('secret-provider-output',str(failure.exception))
        self.assertFalse(list(self.folder.glob('*.usage.json')))

    def test_auth_status_never_returns_provider_credential_previews(self):
        version = subprocess.CompletedProcess([],0,stdout='codex-cli 1.2.3\n',stderr='')
        status = subprocess.CompletedProcess([],0,stdout='Logged in api_key=private-preview',stderr='Bearer private-bearer')
        with patch.object(codex_runner.subprocess,'run',side_effect=[version,status]):
            result = codex_runner.check_runtime()
        self.assertTrue(result['authenticated'])
        serialized = json.dumps(result)
        self.assertNotIn('private-preview',serialized)
        self.assertNotIn('private-bearer',serialized)

    def test_provider_failures_keep_page_messages_safe_and_redact_diagnostics(self):
        process = FakeProcess(returncode=1,
            stderr='401 Unauthorized api_key=private-key Bearer private-bearer sk-private012345\n')
        with patch.object(codex_runner.subprocess,'Popen',return_value=process):
            with self.assertRaises(codex_runner.CodexError) as failure:
                codex_runner.run_codex('fixture',self.schema,self.folder)
        self.assertIn('认证',str(failure.exception))
        diagnostics = '\n'.join(path.read_text('utf-8') for path in self.folder.glob('*.error.txt'))
        self.assertTrue(diagnostics)
        for secret in ('private-key','private-bearer','sk-private012345'):
            self.assertNotIn(secret,str(failure.exception))
            self.assertNotIn(secret,diagnostics)

    def test_process_config_disables_extra_surfaces_without_copying_credentials(self):
        config = {'model':'fixture-model','model_provider':'configured-provider',
            'provider_api_key':'private-key',
            'mcp_servers':{'private_tool':{'url':'https://private.invalid','bearer_token':'private-token'}}}
        with patch.object(codex_runner,'_config',return_value=config):
            command = codex_runner._command('fixture.exe',self.folder,'schema.json','output.json','fixture-model')
        combined = '\n'.join(command)
        self.assertIn('mcp_servers.private_tool.enabled=false',command)
        self.assertIn('web_search="disabled"',command)
        self.assertIn('project_doc_max_bytes=0',command)
        self.assertIn('shell_tool',command)
        self.assertIn('plugins',command)
        for secret in ('private-key','private-token','https://private.invalid'):
            self.assertNotIn(secret,combined)
        with self.assertRaises(ValueError):
            codex_runner._command('fixture.exe',self.folder,'schema.json','output.json','model; unwanted')


if __name__ == '__main__':
    unittest.main()
