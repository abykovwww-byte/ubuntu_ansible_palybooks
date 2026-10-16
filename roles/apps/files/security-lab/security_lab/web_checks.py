"""Web-tool admission. CLI code/flags and artifact locations remain host-owned."""
import hashlib
import re
from typing import Literal
from pydantic import Field, field_validator

from .contracts import Denied, Scope
from .tool_catalog import CurlCheck


class WebCheck(CurlCheck):
    method: Literal['GET'] = 'GET'
    seconds: int = Field(default=60, ge=30, le=180)

    @field_validator('url')
    @classmethod
    def no_template_interpolation(cls, value):
        if any(c in value for c in '${}'):
            raise ValueError('Template interpolation is unsupported')
        return value


class BrowserCheck(WebCheck):
    tool: Literal['playwright'] = 'playwright'
    max_requests: int = Field(default=30, ge=1, le=40)


class FFUFCheck(WebCheck):
    tool: Literal['ffuf'] = 'ffuf'
    paths: list[str] = Field(default_factory=lambda: [
        'robots.txt', 'sitemap.xml', '.well-known/security.txt', 'api', 'api/health',
        'openapi.json', 'swagger.json', 'health', 'status', 'dashboard'], min_length=1, max_length=32)

    @field_validator('paths')
    @classmethod
    def relative_paths(cls, values):
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', v)
               or any(p in {'.', '..'} for p in v.split('/')) or len(v) > 200 for v in values):
            raise ValueError('Expected literal relative paths without encodings or traversal')
        return list(dict.fromkeys(values))


class ZAPCheck(WebCheck):
    tool: Literal['zap'] = 'zap'
    seconds: int = Field(default=120, ge=60, le=180)


WEB_CHECKS = (BrowserCheck, FFUFCheck, ZAPCheck)


def web_policy(spec, scope: Scope):
    target = next((t for t in scope.targets if t.permits(spec.url, 'GET')), None)
    if target is None:
        raise Denied('Web check outside approved HTTP service, path or method')
    urls = [spec.url]
    if isinstance(spec, FFUFCheck):
        base = spec.url.rstrip('/') + '/'
        urls = [base + path for path in spec.paths]
        urls.append(base + 'security-lab-not-found-' + hashlib.sha256(spec.url.encode()).hexdigest()[:12])
    if any(not target.permits(url, 'GET') for url in urls):
        raise Denied('A wordlist candidate or control URL exceeds the approved paths')
    return {'tool': spec.tool, 'url': spec.url, 'urls': urls, 'target': target.model_dump(mode='json'),
            'seconds': spec.seconds, 'rps': scope.budget.rps,
            'max_requests': spec.max_requests if isinstance(spec, BrowserCheck) else len(urls)}
