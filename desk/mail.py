"""Naver POP3 headers only. Credentials come from existing ~/.env or macOS Keychain; no RETR/DELE."""
import json
import poplib
import re
import ssl
import subprocess
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path
from desk.paths import DATA, ensure_dirs
from desk import state

HOST = 'pop.naver.com'
PORT = 995
_compile_lock = threading.Lock()


def _keychain(action, account, password=None):
    ensure_dirs()
    source = Path(__file__).with_name('mail_keychain.swift')
    binary = DATA / 'mail-keychain'
    with _compile_lock:
        if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
            p = subprocess.run(['/usr/bin/swiftc', str(source), '-o', str(binary)], capture_output=True, timeout=90)
            if p.returncode: raise RuntimeError('키체인 도구를 빌드하지 못했습니다. macOS Command Line Tools를 확인하세요.')
    payload = {'action': action, 'account': account}
    if password is not None: payload['password'] = password
    p = subprocess.run([str(binary)], input=json.dumps(payload).encode(), capture_output=True, timeout=30)
    if p.returncode: raise RuntimeError('저장된 앱 비밀번호가 없거나 키체인 접근을 허용하지 않았습니다.')
    return p.stdout.decode()


def config():
    return state.read_json(DATA / 'mail.json', {})


def _existing_credentials(path=None):
    """Reuse the existing AGY Naver skill's two keys, without sourcing shell code."""
    path = Path(path) if path is not None else Path.home() / '.env'
    values = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.lstrip().startswith('#') or '=' not in line: continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key not in ('NAVER_MAIL_USERNAME', 'NAVER_MAIL_PASSWORD'): continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            values[key] = value
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError):
        raise RuntimeError('기존 네이버 메일 설정 파일을 읽지 못했습니다.') from None
    return values.get('NAVER_MAIL_USERNAME'), values.get('NAVER_MAIL_PASSWORD')


def save(account, password):
    account = str(account).strip().lower()
    if not re.fullmatch(r'[a-z0-9_.-]+@naver\.com', account):
        raise ValueError('네이버 메일 주소를 입력하세요.')
    if not password or any(c in password for c in '\r\n\x00'):
        raise ValueError('네이버 애플리케이션 비밀번호를 입력하세요.')
    _keychain('save', account, password)
    state.write_json(DATA / 'mail.json', {'account': account})
    return {'ok': True, 'account': account, 'message': '키체인에 저장했습니다. 연결 테스트로 인증을 확인하세요.'}


def network_check():
    client = poplib.POP3_SSL(HOST, PORT, timeout=10, context=ssl.create_default_context())
    try:
        return {'ok': True, 'stage': 'tls', 'message': '네이버 POP3 TLS 연결 정상. 계정 인증은 별도입니다.'}
    finally: client.quit()


def headers(limit=5):
    account = config().get('account')
    if account:
        password = _keychain('read', account)
    else:
        account, password = _existing_credentials()
        if not account or not password:
            raise RuntimeError('기존 네이버 메일 설정이 없습니다. 연동 설정에서 계정을 먼저 등록하세요.')
    client = None
    try:
        client = poplib.POP3_SSL(HOST, PORT, timeout=15, context=ssl.create_default_context())
        client.user(account)
        client.pass_(password)
        count, _ = client.stat()
        result = []
        for number in range(count, max(0, count - max(1, min(int(limit), 10))), -1):
            _, lines, _ = client.top(number, 0)
            msg = BytesParser(policy=policy.default).parsebytes(b'\r\n'.join(lines))
            result.append({k: str(msg.get(k, ''))[:500] for k in ('Subject', 'From', 'Date')})
        return {'ok': True, 'stage': 'headers', 'total': count, 'headers': result,
                'note': 'POP3에 노출된 메일의 헤더만 조회했습니다. 발송·삭제·본문 다운로드는 하지 않습니다.'}
    except poplib.error_proto:
        raise RuntimeError('POP3 인증 또는 헤더 조회 실패. POP3 사용 설정과 앱 비밀번호를 확인하세요.') from None
    except (OSError, TimeoutError):
        raise RuntimeError('네이버 POP3 연결 실패 또는 시간 초과') from None
    finally:
        password = None
        if client:
            try: client.quit()
            except Exception: client.close()
