"""Host-only session grants. Private values never enter plans, argv or receipts."""

WEB_SESSION_PROGRAM = r'''
import json, math, os, pathlib, re, stat, time
class WebSessionDenied(RuntimeError):
    pass
def web_session_root(p):
    if not re.fullmatch(r'[a-f0-9]{48}',p['owner']):
        raise RuntimeError('Invalid web session owner')
    return pathlib.Path('/tmp') / ('security-lab-web-session-'+p['owner'])

def release_web_session(p):
    if p.get('kind') not in {'websocket','socketio','writes'} or not p.get('session_ref'):
        return True
    root = web_session_root(p)
    if not root.exists():
        return True
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError('Private web session ownership mismatch')
    marker = root / 'owner.json'
    if marker.is_symlink() or json.loads(marker.read_text()) != {'owner':p['owner'],'run_id':p['run_id']}:
        raise RuntimeError('Private web session binding mismatch')
    for name in ('session.json','owner.json'):
        path = root / name
        if path.is_symlink():
            raise RuntimeError('Unsafe private session link')
        path.unlink(missing_ok=True)
    root.rmdir()
    return True

def prepare_web_session(p):
    reference = p.get('session_ref')
    if reference is None:
        return []
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',reference):
        raise WebSessionDenied('web_session_invalid')
    # runs root is host configuration, not a caller-selected secret path.
    source = pathlib.Path(p['runs_directory']).parent / 'web-sessions'
    if source.is_symlink():
        raise WebSessionDenied('web_session_permissions')
    flags = os.O_RDONLY | getattr(os,'O_NOFOLLOW',0)
    try:
        directory = os.open(source, flags | os.O_DIRECTORY)
    except OSError:
        raise WebSessionDenied('web_session_not_found') from None
    try:
        try:
            descriptor = os.open(reference+'.json',flags,dir_fd=directory)
        except FileNotFoundError:
            raise WebSessionDenied('web_session_not_found') from None
        except OSError:
            raise WebSessionDenied('web_session_permissions') from None
        with os.fdopen(descriptor,'r',encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size>16384
                    or info.st_uid not in {0,os.getuid()} or info.st_mode & 0o077):
                raise WebSessionDenied('web_session_permissions')
            try:
                grant = json.load(stream)
            except (ValueError,UnicodeError):
                raise WebSessionDenied('web_session_invalid') from None
    finally:
        os.close(directory)
    if not isinstance(grant,dict) or not isinstance(grant.get('expires_at'),(int,float)) or not math.isfinite(grant['expires_at']):
        raise WebSessionDenied('web_session_invalid')
    if not isinstance(grant.get('values',{}),dict):
        raise WebSessionDenied('web_session_invalid')
    if (set(grant)-{'schema_version','case_ids','origins','expires_at','headers','cookies','socketio_auth','values'}
            or grant.get('schema_version')!=1 or p['web_case_id'] not in grant.get('case_ids',[])
            or p['web_origin'] not in grant.get('origins',[]) or grant.get('expires_at',0)<=time.time()):
        raise WebSessionDenied('web_session_not_authorized')
    for collection in ('headers','cookies'):
        values = grant.get(collection,{})
        if not isinstance(values,dict) or len(values)>8:
            raise WebSessionDenied('web_session_invalid')
        for key,value in values.items():
            if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key)
                    or not isinstance(value,str) or len(value)>4096
                    or any(ord(c)<32 or ord(c)==127 for c in value)):
                raise WebSessionDenied('web_session_invalid')
            if collection=='headers' and key.lower() not in {'authorization','x-api-key','x-auth-token'}:
                raise WebSessionDenied('web_session_invalid')
    root = web_session_root(p)
    root.mkdir(mode=0o700,exist_ok=False)
    # Marker is created first so cancellation can clean up a partial write.
    marker = root / 'owner.json'
    with marker.open('x',encoding='utf-8') as stream:
        json.dump({'owner':p['owner'],'run_id':p['run_id']},stream)
    marker.chmod(0o600)
    path = root / 'session.json'
    with path.open('x',encoding='utf-8') as stream:
        json.dump(grant,stream)
    path.chmod(0o600)
    return ['--volume',str(path)+':/run/web-session.json:ro']
'''
