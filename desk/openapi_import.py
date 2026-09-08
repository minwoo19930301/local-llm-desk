"""Conservative OpenAPI 3.0/3.1 JSON -> HTTP connector drafts; never saves/runs them.

Reference: https://spec.openapis.org/oas/v3.1.1.html (servers, parameters,
request bodies, Security Requirement Objects, and local JSON Pointer references).
YAML is deliberately unsupported until a maintained parser is a dependency.
"""
from __future__ import annotations

import copy
import http.client
import ipaddress
import json
import re
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

from desk import tools

MAX_SPEC_BYTES = 1024 * 1024
MAX_OPERATIONS = 200
MAX_NODES = 30000
MAX_DEPTH = 40
METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
FORMAT_NOTE = "OpenAPI 3.0/3.1 JSON만 지원합니다. YAML은 JSON으로 변환해 붙여 넣으세요."


class SpecError(ValueError):
    pass


def fetch_spec(source_url: str, timeout: float = 20) -> dict:
    """GET only the explicitly supplied public spec URL; validate every redirect.

    Reuses the runtime's deadline-aware DNS validation and pinned HTTP connection.
    No operation servers or external references are fetched.
    """
    if not isinstance(source_url, str) or not source_url.strip():
        raise SpecError("명세 URL을 입력하세요.")
    url = source_url.strip()
    deadline = time.monotonic() + min(20.0, max(0.1, float(timeout)))
    for _ in range(6):
        parsed, addresses = tools._destinations(url, allow_local=False, deadline=deadline)
        connection = tools._pinned_connection(parsed, addresses, deadline)
        sockets: list = []
        timer = threading.Timer(tools._remaining_http(deadline), tools._interrupt_http, args=(connection, sockets))
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            connection.sock.settimeout(tools._remaining_http(deadline))
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={"Accept": "application/json", "User-Agent": "Free-AI-Scheduler-OpenAPI"})
            connection.sock.settimeout(tools._remaining_http(deadline))
            response = connection.getresponse()
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sockets.append(sock)
                sock.settimeout(tools._remaining_http(deadline))
            if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status != 200:
                raise SpecError(f"명세 조회 실패: HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise SpecError("압축된 명세 응답은 지원하지 않습니다. JSON 텍스트를 붙여 넣으세요.")
            length = response.getheader("Content-Length")
            if length and int(length) > MAX_SPEC_BYTES:
                raise SpecError("명세는 1 MiB 이하여야 합니다.")
            raw = bytearray()
            while len(raw) <= MAX_SPEC_BYTES:
                tools._remaining_http(deadline)
                chunk = response.read1(min(65536, MAX_SPEC_BYTES + 1 - len(raw)))
                tools._remaining_http(deadline)
                if not chunk:
                    if isinstance(response.length, int) and response.length > 0:
                        raise http.client.IncompleteRead(bytes(raw), response.length)
                    break
                raw.extend(chunk)
            if len(raw) > MAX_SPEC_BYTES:
                raise SpecError("명세는 1 MiB 이하여야 합니다.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise SpecError("명세는 UTF-8 JSON이어야 합니다.") from exc
            return {"text": text, "source_url": url}
        finally:
            timer.cancel()
            connection.close()
    raise SpecError("명세 리디렉션 횟수를 초과했습니다.")


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise SpecError("중복 JSON 키가 있는 명세는 지원하지 않습니다.")
        out[key] = value
    return out


def _invalid_constant(value):
    raise SpecError("JSON에 NaN/Infinity를 쓸 수 없습니다.")


def _parse(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError("명세는 1 MiB 이하의 JSON 텍스트여야 합니다.")
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SpecError(FORMAT_NOTE) from exc
    if not isinstance(doc, dict) or not re.fullmatch(r"3\.[01]\.\d+", str(doc.get("openapi", ""))):
        raise SpecError(FORMAT_NOTE)
    pending, count = [(doc, 0)], 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise SpecError("명세의 크기 또는 중첩 깊이 제한을 초과했습니다.")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return doc


class _Resolver:
    def __init__(self, document):
        self.document = document
        self.visits = 0

    def object(self, value, seen=()):
        self.visits += 1
        if self.visits > MAX_NODES or len(seen) > MAX_DEPTH:
            raise SpecError("로컬 참조 해석 제한을 초과했습니다.")
        if not isinstance(value, dict):
            raise SpecError("객체 형식이 필요한 명세 항목입니다.")
        if "$ref" not in value:
            return value
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SpecError("외부 $ref는 가져오지 않습니다. 명세를 단일 JSON 파일로 합쳐 주세요.")
        if ref in seen:
            raise SpecError("순환 $ref는 지원하지 않습니다.")
        if set(value) - {"$ref", "summary", "description"}:
            raise SpecError("$ref와 함께 추가 제약을 지정한 스키마는 지원하지 않습니다.")
        current = self.document
        try:
            for token in unquote(ref[2:]).split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(current, list) and not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("invalid array reference")
                current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise SpecError("존재하지 않는 로컬 $ref입니다.") from exc
        target = self.object(current, (*seen, ref))
        return {**target, **{key: value[key] for key in ("summary", "description") if key in value}}


def _public_server(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise SpecError("인증정보 없는 http(s) 서버 URL이 필요합니다.")
    if parsed.query or parsed.fragment or any(char in url for char in "\r\n\x00{}"):
        raise SpecError("쿼리·fragment·미해결 변수 없는 서버 URL이 필요합니다.")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise SpecError("로컬 서버 주소는 OpenAPI 초안 가져오기에서 지원하지 않습니다.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # Parsing never resolves operation server DNS or accesses it.
    if not address.is_global:
        raise SpecError("공개 서버 주소가 필요합니다.")


def _server(doc, path_item, operation, source_url, warnings):
    servers = operation.get("servers", path_item.get("servers", doc.get("servers", [{"url": "/"}])))
    if not isinstance(servers, list) or not servers:
        raise SpecError("서버 URL을 하나 이상 지정해야 합니다.")
    if len(servers) > 1:
        warnings.append("여러 서버 중 첫 번째 servers URL을 사용했습니다. 저장 전에 확인하세요.")
    server = servers[0]
    if not isinstance(server, dict) or not isinstance(server.get("url"), str):
        raise SpecError("올바른 서버 URL이 필요합니다.")
    url = server["url"]
    variables = server.get("variables") or {}
    for name in re.findall(r"\{([^{}]+)\}", url):
        variable = variables.get(name)
        if not isinstance(variable, dict) or not isinstance(variable.get("default"), str):
            raise SpecError("서버 변수의 문자열 default가 필요합니다.")
        if "enum" in variable and variable["default"] not in variable["enum"]:
            raise SpecError("서버 변수 default가 enum에 없습니다.")
        url = url.replace("{" + name + "}", variable["default"])
    if not urlsplit(url).scheme:
        if not source_url:
            raise SpecError("상대 서버 URL을 해석할 명세 URL이 필요합니다.")
        url = urljoin(source_url, url)
    _public_server(url)
    return url.rstrip("/")


def _primitive(resolver, raw, label):
    schema = resolver.object(raw)
    unknown = set(schema) - {"type", "description", "title", "default", "example", "examples", "deprecated"}
    if unknown:
        raise SpecError(f"{label}: 지원하지 않는 스키마 제약 ({', '.join(sorted(unknown))}).")
    if schema.get("type") not in ("string", "integer"):
        raise SpecError(f"{label}: 현재 HTTP 템플릿은 string/integer만 정확하게 지원합니다 (boolean/number/배열/객체 미지원).")
    default = schema.get("default")
    if default is not None and ((schema["type"] == "string" and not isinstance(default, str)) or (schema["type"] == "integer" and (not isinstance(default, int) or isinstance(default, bool)))):
        raise SpecError(f"{label}: default의 타입이 스키마와 다릅니다.")
    return schema


def _arg_name(prefix, name, used):
    stem = prefix + "_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
    key, suffix = stem, 2
    while key in used:
        key, suffix = f"{stem}_{suffix}", suffix + 1
    used.add(key)
    return key


def _parameter_lists(resolver, path_item, operation):
    merged = {}
    for owner in (path_item, operation):
        parameters = owner.get("parameters", [])
        if not isinstance(parameters, list):
            raise SpecError("parameters는 배열이어야 합니다.")
        seen = set()
        for raw in parameters:
            param = resolver.object(raw)
            name, location = param.get("name"), param.get("in")
            if not isinstance(name, str) or not name or not isinstance(location, str):
                raise SpecError("파라미터 name/in이 필요합니다.")
            key = (name, location)
            if key in seen:
                raise SpecError("같은 위치와 이름의 파라미터가 중복되었습니다.")
            seen.add(key)
            merged[key] = param
    return list(merged.values())


def _auth(doc, operation, resolver, warnings):
    requirements = operation.get("security", doc.get("security", []))
    if not isinstance(requirements, list):
        raise SpecError("security는 배열이어야 합니다.")
    if not requirements:
        return {}
    if len(requirements) != 1 or not isinstance(requirements[0], dict):
        raise SpecError("여러 인증 대안은 자동 선택하지 않습니다. 사용할 security 항목 하나를 지정하세요.")
    schemes = (doc.get("components") or {}).get("securitySchemes") or {}
    headers, used_env = {}, set()
    for name, scopes in requirements[0].items():
        if scopes:
            raise SpecError("OAuth scope/role 요구사항은 자동 가져오기를 지원하지 않습니다.")
        scheme = resolver.object(schemes.get(name))
        kind = scheme.get("type")
        if kind == "http" and str(scheme.get("scheme", "")).lower() == "bearer":
            header, prefix = "Authorization", "Bearer "
        elif kind == "apiKey" and scheme.get("in") == "header":
            header, prefix = scheme.get("name"), ""
        else:
            raise SpecError("bearer 또는 header apiKey만 지원합니다. OAuth/basic/query·cookie apiKey는 직접 설정해야 합니다.")
        if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", header) or header.lower() in {"host", "cookie", "content-length", "content-type"}:
            raise SpecError("지원하지 않는 인증 헤더 이름입니다.")
        if header.lower() in {key.lower() for key in headers}:
            raise SpecError("인증 헤더 이름이 충돌합니다.")
        env = _arg_name("OPENAPI", name.upper(), used_env)
        headers[header] = prefix + "${" + env + "}"
        warnings.append(f"인증정보는 포함하지 않았습니다. 실행 환경의 {env}를 설정해야 합니다.")
    return headers


def _connector(doc, path, item, method, operation, resolver, source_url, warnings):
    if method not in ("get", "post"):
        raise SpecError(f"{method.upper()} 메서드는 지원하지 않습니다. GET/POST만 가능합니다.")
    if operation.get("callbacks"):
        raise SpecError("callback이 필요한 operation은 지원하지 않습니다.")
    base = _server(doc, item, operation, source_url, warnings)
    if not path.startswith("/") or any(char in path for char in "?#\r\n\x00") or any(char in re.sub(r"\{[^{}]+\}", "", path) for char in "{}"):
        raise SpecError("올바른 OpenAPI path가 필요합니다.")
    params, used, query, path_names = {}, set(), [], set()
    path_aliases = {}
    for parameter in _parameter_lists(resolver, item, operation):
        location, name = parameter["in"], parameter["name"]
        if location not in ("path", "query"):
            raise SpecError("일반 header/cookie 파라미터는 지원하지 않습니다.")
        if "content" in parameter or parameter.get("allowReserved") or parameter.get("allowEmptyValue"):
            raise SpecError(f"{name}: content/allowReserved/allowEmptyValue 직렬화는 지원하지 않습니다.")
        if parameter.get("style", "simple" if location == "path" else "form") != ("simple" if location == "path" else "form"):
            raise SpecError(f"{name}: 기본 simple/form 직렬화만 지원합니다.")
        schema = _primitive(resolver, parameter.get("schema"), name)
        required = parameter.get("required") is True
        if location == "path" and not required:
            raise SpecError(f"{name}: path 파라미터는 required=true여야 합니다.")
        if not required:
            raise SpecError(f"{name}: 선택적 파라미터 생략을 현재 HTTP 템플릿이 지원하지 않습니다.")
        arg = _arg_name(location, name, used)
        params[arg] = {"type": schema["type"], "required": True, "description": str(parameter.get("description") or schema.get("description") or name), "default": schema.get("default")}
        if location == "path":
            if "{" + name + "}" not in path:
                raise SpecError(f"{name}: path에 대응하는 변수가 없습니다.")
            path_aliases[name] = arg
            path_names.add(name)
        else:
            query.append(quote(name, safe="") + "={" + arg + "}")
    if set(re.findall(r"\{([^{}]+)\}", path)) != path_names:
        raise SpecError("path 변수와 path 파라미터 정의가 일치하지 않습니다.")
    url_path = re.sub(r"\{([^{}]+)\}", lambda match: "{" + path_aliases[match[1]] + "}", path)
    body_template = ""
    if "requestBody" in operation:
        if method != "post":
            raise SpecError("GET requestBody는 지원하지 않습니다.")
        body = resolver.object(operation["requestBody"])
        if body.get("required") is not True:
            raise SpecError("선택적 requestBody 생략은 지원하지 않습니다.")
        content = body.get("content")
        if not isinstance(content, dict) or set(content) != {"application/json"}:
            raise SpecError("requestBody는 application/json 하나만 지원합니다.")
        media = resolver.object(content["application/json"])
        if media.get("encoding"):
            raise SpecError("별도 requestBody encoding은 지원하지 않습니다.")
        schema = resolver.object(media.get("schema"))
        if schema.get("type") != "object" or set(schema) - {"type", "properties", "required", "additionalProperties", "description", "title"}:
            raise SpecError("JSON body는 string/integer 속성의 평면 object만 지원합니다.")
        if schema.get("additionalProperties", False) is not False:
            raise SpecError("추가 JSON 속성 스키마는 지원하지 않습니다.")
        properties, required = schema.get("properties", {}), schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or set(required) != set(properties):
            raise SpecError("JSON body의 모든 속성은 required여야 합니다. 선택적 속성 생략은 지원하지 않습니다.")
        members = []
        for name, raw in properties.items():
            field = _primitive(resolver, raw, name)
            arg = _arg_name("body", name, used)
            params[arg] = {"type": field["type"], "required": True, "description": str(field.get("description") or name), "default": field.get("default")}
            slot = "{" + arg + "}"
            key_json = json.dumps(name, ensure_ascii=False).replace("{", r"\u007b").replace("}", r"\u007d")
            members.append(key_json + ": " + (json.dumps(slot) if field["type"] == "string" else slot))
        body_template = "{" + ", ".join(members) + "}"
    headers = _auth(doc, operation, resolver, warnings)
    if body_template:
        headers["Content-Type"] = "application/json"
    ident = str(operation.get("operationId") or method + "_" + path)
    return {"kind": "http", "name": re.sub(r"[^A-Za-z0-9_-]", "_", ident)[:40] or "openapi_operation", "enabled": False,
            "description": str(operation.get("summary") or operation.get("description") or ident)[:2000],
            "method": method.upper(), "url_template": base + url_path + ("?" + "&".join(query) if query else ""),
            "headers": headers, "body_template": body_template, "params": params}


def inspect_spec(spec_text: str, source_url: str | None = None) -> dict:
    doc = _parse(spec_text)
    resolver = _Resolver(doc)
    paths = doc.get("paths", {})
    if not isinstance(paths, dict):
        raise SpecError("paths는 객체여야 합니다.")
    rows, ids = [], {}
    for path, raw_item in paths.items():
        if path.startswith("x-"):
            continue
        if len(rows) >= MAX_OPERATIONS:
            raise SpecError("operation은 최대 200개까지 지원합니다. 필요한 명세만 나누어 주세요.")
        try:
            item = resolver.object(raw_item)
        except SpecError as exc:
            rows.append({"id": "PATH " + path, "operation_id": None, "method": "", "path": path, "summary": "", "supported": False, "issues": [str(exc)], "warnings": []})
            continue
        for method in METHODS:
            if method not in item:
                continue
            if len(rows) >= MAX_OPERATIONS:
                raise SpecError("operation은 최대 200개까지 지원합니다. 필요한 명세만 나누어 주세요.")
            operation = item[method]
            operation_id = operation.get("operationId") if isinstance(operation, dict) else None
            ident = operation_id if isinstance(operation_id, str) and operation_id else method.upper() + " " + path
            row = {"id": ident, "operation_id": operation_id, "method": method.upper(), "path": path,
                   "summary": str(operation.get("summary") or "")[:1000] if isinstance(operation, dict) else "", "supported": False, "issues": [], "warnings": []}
            try:
                if not isinstance(operation, dict):
                    raise SpecError("operation은 객체여야 합니다.")
                row["connector"] = _connector(doc, path, item, method, operation, resolver, source_url, row["warnings"])
                row["supported"] = True
            except (SpecError, TypeError, AttributeError) as exc:
                row["issues"].append(str(exc) if isinstance(exc, SpecError) else "지원하지 않는 명세 구조입니다.")
            rows.append(row)
            ids.setdefault(ident, []).append(row)
    for matches in ids.values():
        if len(matches) > 1:
            for row in matches:
                row["supported"] = False
                row.pop("connector", None)
                row["issues"].append("operationId가 중복되어 선택할 수 없습니다.")
    warnings = [FORMAT_NOTE]
    if doc.get("webhooks"):
        warnings.append("webhooks는 가져오지 않습니다. paths의 요청 operation만 표시합니다.")
    return {"version": doc["openapi"], "operations": rows, "warnings": warnings, "questions": []}


def build_connector(spec_text: str, operation_id: str, source_url: str | None = None) -> dict:
    inspection = inspect_spec(spec_text, source_url)
    matches = [row for row in inspection["operations"] if row["id"] == operation_id]
    if len(matches) != 1:
        raise SpecError("가져올 operation을 하나 선택하세요.")
    if not matches[0]["supported"]:
        raise SpecError("; ".join(matches[0]["issues"]))
    return copy.deepcopy(matches[0]["connector"])


def prepare(source_url: str | None = None, spec_text: str | None = None, operation_id: str | None = None) -> dict:
    if not spec_text:
        if not source_url:
            return {"operations": [], "warnings": [FORMAT_NOTE], "questions": ["OpenAPI JSON 또는 명세 URL을 입력하세요."]}
        fetched = fetch_spec(source_url)
        spec_text, source_url = fetched["text"], fetched["source_url"]
    result = inspect_spec(spec_text, source_url)
    choices = result["operations"]
    if not operation_id:
        if len(choices) == 1:
            operation_id = choices[0]["id"]
        else:
            result["questions"].append("가져올 operation을 하나 선택하세요.")
            return result
    matches = [row for row in choices if row["id"] == operation_id]
    if len(matches) != 1 or not matches[0]["supported"]:
        result["questions"].extend(matches[0]["issues"] if len(matches) == 1 else ["가져올 operation을 하나 선택하세요."])
        return result
    result["selected"] = copy.deepcopy(matches[0]["connector"])
    result["warnings"].extend(matches[0]["warnings"])
    return result
