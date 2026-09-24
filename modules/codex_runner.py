"""Local Codex transport for bounded, schema-validated sentiment agents.

Authentication stays with the installed Codex CLI. The application never copies
credentials into its database or exports. Tool requests are resolved by the
analysis controller against the selected review snapshot, not by a shell.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import tomllib
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class CodexError(RuntimeError):
    pass


class AnalysisCancelled(CodexError):
    pass


def _config():
    location=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'
    try:
        return tomllib.loads(location.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}


# The desktop model picker currently exposes these model IDs.  The CLI does
# not provide a model-list endpoint, so these are displayed as *candidates*;
# ``configured_models`` is kept separately and is used to indicate whether a
# candidate has already been seen in the local Codex configuration.
KNOWN_MODELS = ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra',
                'gpt-5.6-luna', 'gpt-5.5')
REASONING_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
MODEL_REASONING_EFFORTS = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-terra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.5': ('low', 'medium', 'high', 'xhigh'),
}


def configured_model():
    return str(_config().get('model') or '')


def configured_reasoning_effort():
    value = str(_config().get('model_reasoning_effort') or '').strip().lower()
    return value if value in REASONING_EFFORTS else ''


def configured_models():
    """Return models explicitly present in this machine's Codex config.

    This is deliberately separate from :func:`available_models`: the latter
    also returns the desktop picker candidates, while this function is the
    source of the UI's ``已配置``/``启动时验证`` status.
    """
    config = _config()
    values = []
    for key in ('models', 'available_models'):
        candidate = config.get(key)
        if isinstance(candidate, (list, tuple)):
            values.extend(str(value).strip() for value in candidate if str(value).strip())
        elif isinstance(candidate, dict):
            values.extend(str(value).strip() for value in candidate if str(value).strip())
    current = configured_model()
    if current:
        values.insert(0, current)
    return list(dict.fromkeys(values))


def available_reasoning_efforts():
    configured = _config().get('desktop', {}).get('enabled-reasoning-efforts')
    if isinstance(configured, list):
        values = tuple(str(value).strip().lower() for value in configured
                       if str(value).strip().lower() in REASONING_EFFORTS)
        if values:
            return values
    return REASONING_EFFORTS


def reasoning_efforts_for_model(model=''):
    """Return strengths valid for ``model`` and enabled by local config.

    An empty model means "follow the local default", therefore the local
    Codex capability set is used.  Unknown models are left to the CLI's
    configured set and are still validated when the process starts.
    """
    enabled = set(available_reasoning_efforts())
    model_name = str(model or '').strip() or configured_model()
    allowed = MODEL_REASONING_EFFORTS.get(model_name, REASONING_EFFORTS)
    return tuple(value for value in allowed if value in enabled)


def model_is_locally_configured(model):
    return str(model or '').strip() in set(configured_models())


def available_models():
    """Return the desktop picker candidates plus locally configured IDs.

    Codex CLI has no model-list command.  The known IDs are therefore shown as
    candidates and the selected ID is still validated by the real ``exec``
    call; ``model_is_locally_configured`` exposes the distinction to the UI.
    """
    values = list(configured_models())
    # Include the same choices shown by the Codex desktop picker.  They are
    # candidates only; arbitrary model IDs are never accepted by the UI.
    values.extend(KNOWN_MODELS)
    return list(dict.fromkeys(values))


def find_codex():
    override=os.environ.get('APPSTORE_CODEX_BIN','')
    if override:
        path=Path(override)
        if path.is_file() and (os.name!='nt' or path.suffix.lower()=='.exe'):
            return str(path.resolve())
        return None
    for name in ('codex.exe','codex'):
        path=shutil.which(name)
        if path and (os.name!='nt' or Path(path).suffix.lower()=='.exe'):
            return path
    if os.name=='nt':
        base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'OpenAI/Codex/bin'
        candidates=list(base.glob('*/codex.exe')) if base.exists() else []
        if candidates:
            return str(max(candidates,key=lambda p:p.stat().st_mtime))
    return None


def _process_options():
    return {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}


def check_runtime():
    executable=find_codex()
    result={'available':bool(executable),'authenticated':False,'path':executable or '',
            'model':configured_model(),'reasoning_effort':configured_reasoning_effort(),'message':''}
    if not executable:
        result['message']='未找到 Codex。请先安装并登录 Codex，或配置 APPSTORE_CODEX_BIN。'
        return result
    try:
        version=subprocess.run([executable,'--version'],capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=15,**_process_options())
        if version.returncode:
            raise CodexError('Codex 无法启动。请在终端运行 codex --version 检查安装。')
        result['version']=next((line for line in version.stdout.splitlines() if line.startswith('codex-cli')),'Codex')
        auth=subprocess.run([executable,'login','status'],capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=20,**_process_options())
        # Never expose raw login status: some providers include a key preview.
        result['authenticated']=auth.returncode==0
        result['message']='Codex 已就绪，将使用本机登录与模型配置。' if result['authenticated'] else 'Codex 尚未登录，请在本机终端运行 codex login 后重新检查。'
    except (OSError,subprocess.TimeoutExpired,CodexError):
        result['message']='Codex 检查未完成，请确认本机 Codex 可以启动并已登录，再重试。'
    return result


def _command(executable,work_dir,schema_file,output_file,model,reasoning_effort=''):
    command=[executable,'exec','--json','--ephemeral','--skip-git-repo-check',
             '--sandbox','read-only','--color','never','-C',str(work_dir),
             '--output-schema',str(schema_file),'-o',str(output_file)]
    if model:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}',model):
            raise ValueError('模型名称格式不正确')
        command.extend(['--model',model])
    if reasoning_effort:
        effort = str(reasoning_effort).strip().lower()
        if effort not in reasoning_efforts_for_model(model):
            raise ValueError('所选模型不支持该推理强度，或该强度未在当前 Codex 配置启用')
        command.extend(['-c',f'model_reasoning_effort="{effort}"'])
    # Keep the configured provider/auth, but disable unrelated agent surfaces.
    # These overrides affect this child process only, never the user's settings.
    for feature in ['shell_tool','apps','plugins','hooks','browser_use','computer_use',
                    'multi_agent','memories','image_generation','skill_search']:
        command.extend(['--disable',feature])
    command.extend(['-c','web_search="disabled"','-c','approval_policy="never"',
                    '-c','project_doc_max_bytes=0'])
    for name in _config().get('mcp_servers',{}):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',str(name)):
            raise CodexError('本机 MCP 配置名称暂不受分析隔离器支持，请使用字母、数字、下划线或连字符命名。')
        command.extend(['-c',f'mcp_servers.{name}.enabled=false'])
    command.append('-')
    return command


def _failure_message(text,returncode=None):
    message=text.lower()
    if any(word in message for word in ['401','unauthorized','authentication','not logged','invalid api key']):
        return 'Codex 登录已失效或服务拒绝认证，请在本机重新登录后重试；已采集数据和已完成分析保留。'
    if any(word in message for word in ['429','rate limit','quota','usage limit','credits']):
        return '模型服务达到额度或频率限制，请稍后重试或检查本机 Codex 额度；已完成分析保留。'
    if any(word in message for word in ['model_not_found','model not found','unsupported model']):
        return '当前登录配置无法使用所选模型，请检查 Codex 的模型设置后重试。'
    if any(word in message for word in ['connection','connect','network','timed out','dns','stream disconnected']):
        return '模型服务连接未完成，请检查网络与本机 Codex 服务配置后重试。'
    return f'Codex 未返回可用分析结果（退出码 {returncode}），请检查本机 Codex 是否能正常运行后重试。'


def _redacted_error(text):
    text=re.sub(r'(?i)(bearer\s+|(?:api[_ -]?key|token|authorization)\s*[=:]\s*)[^\s,;]+',r'\1[redacted]',text)
    text=re.sub(r'\b(?:sk-|6A-)[A-Za-z0-9_\-]+','[redacted]',text)
    return text[-6000:]


def run_codex(prompt,schema,work_dir,*,model='',reasoning_effort='',on_progress=None,cancelled=None,timeout=240):
    executable=find_codex()
    if not executable:
        raise CodexError('未找到本机 Codex，请先安装并登录后重试。')
    if cancelled and cancelled():
        raise AnalysisCancelled('已取消分析')
    Draft202012Validator.check_schema(schema)
    work_dir=Path(work_dir).resolve()
    work_dir.mkdir(parents=True,exist_ok=True)
    call_id=uuid.uuid4().hex
    schema_file=work_dir/(call_id+'.schema.json')
    output_file=work_dir/(call_id+'.result.json')
    schema_file.write_text(json.dumps(schema,ensure_ascii=False),encoding='utf-8')
    effective_model=model or configured_model()
    # Empty means follow the Codex config without forcing an effort that may not
    # be supported by a user-selected model.
    effective_effort=str(reasoning_effort or '').strip().lower()
    command=_command(executable,work_dir,schema_file,output_file,effective_model,effective_effort)
    env=os.environ.copy()
    env.pop('CODEX_THREAD_ID',None)
    env.pop('CODEX_TURN_ID',None)
    events=queue.Queue()
    errors=[]
    usage={}
    output_text=''
    started=time.monotonic()
    if on_progress:
        on_progress('正在连接本机 Codex 分析服务')
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        cwd=work_dir,env=env,text=True,encoding='utf-8',errors='replace',bufsize=1,**_process_options())

    def read_stdout():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    def read_stderr():
        for line in process.stderr:
            errors.append(line)
            if len(errors)>40:
                del errors[:-40]

    def write_prompt():
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError,OSError):
            pass

    readers=[threading.Thread(target=read_stdout,daemon=True),threading.Thread(target=read_stderr,daemon=True),
             threading.Thread(target=write_prompt,daemon=True)]
    for reader in readers:
        reader.start()
    stream_done=False
    try:
        while not stream_done or process.poll() is None:
            if cancelled and cancelled():
                raise AnalysisCancelled('已取消分析；已经完成的评论结果保留')
            if time.monotonic()-started>timeout:
                raise CodexError('本批 Agent 分析超时；已完成结果保留，可以重试继续。')
            try:
                line=events.get(timeout=.25)
            except queue.Empty:
                continue
            if line is None:
                stream_done=True
                continue
            try:
                event=json.loads(line)
            except ValueError:
                continue
            event_type=event.get('type')
            if event_type=='turn.completed':
                usage=event.get('usage') or {}
            elif event_type in ('error','turn.failed'):
                errors.append(json.dumps(event,ensure_ascii=False))
            elif event_type=='turn.started' and on_progress:
                on_progress('Agent 正在阅读评论与证据')
            elif event_type=='item.completed' and event.get('item',{}).get('type')=='agent_message':
                output_text=event['item'].get('text','')
        if process.returncode:
            (work_dir/(call_id+'.error.txt')).write_text(_redacted_error(''.join(errors)),encoding='utf-8')
            raise CodexError(_failure_message(''.join(errors),process.returncode))
        if output_file.is_file():
            output_text=output_file.read_text(encoding='utf-8')
        try:
            output=json.loads(output_text)
            Draft202012Validator(schema).validate(output)
        except (ValueError,ValidationError) as exc:
            # Do not publish raw model/provider output in the page error panel.
            raise CodexError('Agent 返回格式未通过校验，本批未入库。请重试。') from exc
        metadata={'model':effective_model or 'Codex 当前默认模型','reasoning_effort':effective_effort or 'Codex 当前默认配置','usage':usage,
                  'elapsed_seconds':round(time.monotonic()-started,2)}
        (work_dir/(call_id+'.usage.json')).write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
        return {'output':output,**metadata}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream and not stream.closed:
                stream.close()
