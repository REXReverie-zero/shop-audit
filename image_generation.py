from session_storage import data_dir
"""火山方舟 Seedream 调用模块，预算与文案共用。
"""
import base64
import binascii
import hashlib
import io
import json
import os
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import budget
import prompt_store
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
API_URL = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
DEFAULT_MODEL = 'doubao-seedream-4-0-250828'
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
NOTICE = 'AI图片仅为广告创意草稿，不能直接商用；商用前需人工审核。图像可能存在细节失真，版权及授权情况未经核验，仅供创意参考。'
# 指定像素尺寸：小红书精确3:4；朋友圈精确16:9。
PLATFORMS = {'小红书': {'size': '1728x2304'}, '朋友圈': {'size': '2560x1440'}}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def https_url(value):
    """配置中的URL不接受内嵌账号、密钥或非HTTPS协议。"""
    parsed = urlparse(value)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password


def build_payload(platform, product, model, *, prompt_spec=None):
    product = product.strip()
    if platform not in PLATFORMS or not product or len(product) > 100:
        raise ValueError('请选择平台并填写1—100字的产品名称。')
    if not model.strip():
        raise ValueError('请填写生图模型名称。')
    config = PLATFORMS[platform]
    prompt_spec = prompt_spec or prompt_store.get('image', platform)
    template = prompt_spec['text']
    return {'model': model.strip(), 'prompt': template.replace('{product}', product),
            'size': config['size'],
            # pro/flash不接受组图参数，即使值为disabled也会报400。
            # 旧模型的组图默认关闭；统一省略组图和流式选项，兼容ep接入点。
            # 不发送OpenAI的n，也不自动删除参数后重试付费请求。
            'response_format': 'b64_json',  # 图片直接返回，避免下载链接过期。
            'watermark': True}


def http_error_message(status, result):
    """将云端可读的错误信息展示出来，避免把所有400误报成尺寸问题。"""
    tips = {400: '服务端拒绝请求参数。', 401: '请检查方舟 API Key。',
            403: '请确认模型已开通且密钥有调用权限。',
            429: '请求受限，请核对额度、余额及限流设置。'}
    message = f'HTTP {status}：' + tips.get(status, '请检查方舟服务状态和返回日志。')
    error = result.get('error') if isinstance(result, dict) else None
    if isinstance(error, dict):
        code, parameter, detail = error.get('code'), error.get('param'), error.get('message')
        if isinstance(code, str):
            message += '\n错误代码：' + code[:200]
        if isinstance(parameter, str):
            message += '\n参数：' + parameter[:200]
        if isinstance(detail, str):
            message += '\n服务端说明：' + detail[:1600]
    return message


def serialize(record, key=''):
    """不记录请求头；额外屏蔽响应中可能回显的当前密钥。"""
    value = json.dumps(record, ensure_ascii=False, indent=2)
    return value.replace(key, '[REDACTED]') if key else value


def save_record(record, directory, key=''):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (record['request_id'] + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(serialize(record, key), encoding='utf-8')
    temporary.replace(path)
    return path


def bounded_body(response, limit):
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > limit:
            raise ValueError('服务端返回内容超过大小限制，已停止接收。')
        chunks.append(chunk)
    return b''.join(chunks)


def image_from_response(result):
    """读取常见的base64或URL结果；下载图片不会转发API密钥。"""
    if isinstance(result, dict) and result.get('error'):
        raise ValueError('方舟返回错误：' + json.dumps(result['error'], ensure_ascii=False))
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError('没有找到data[0]图片结果；该服务可能采用不同接口格式。')
    item = data[0]
    if item.get('error'):
        raise ValueError('方舟返回图片级错误：' + json.dumps(item['error'], ensure_ascii=False))
    if item.get('b64_json'):
        value = item['b64_json']
        if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise ValueError('图片编码格式不正确或图片过大。')
        content = base64.b64decode(value, validate=True)
    elif item.get('url'):
        url = item['url']
        if not isinstance(url, str) or not https_url(url):
            raise ValueError('返回的图片链接不是有效的HTTPS地址。')
        # 不跟随重定向；如供应商需要其他协议，应单独适配。
        with requests.get(url, timeout=(10, 60), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('图片下载没有返回HTTP 200。')
            content = bounded_body(response, MAX_IMAGE_BYTES)
    else:
        raise ValueError('返回结果没有b64_json或url。')
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('图片为空或超过大小限制。')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as picture:
            dimensions, kind = picture.size, picture.format
            picture.verify()
    formats = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp')}
    if kind not in formats:
        raise ValueError('仅支持PNG、JPEG、WEBP图片。')
    extension, mime = formats[kind]
    return content, dimensions, extension, mime


def returned_image_count(result):
    if not isinstance(result, dict):
        return None
    usage = result.get('usage')
    if isinstance(usage, dict) and 'generated_images' in usage:
        count = usage['generated_images']
        return count if type(count) is int and 0 <= count <= 1 else None
    data = result.get('data')
    if isinstance(data, list):
        count = sum(isinstance(item, dict) and bool(item.get('url') or item.get('b64_json')) for item in data)
        if count == 1:
            return count
    return None


def generate_image(platform, product, endpoint, model, api_key, directory=None, *, budget_directory=None):
    """只有网页按钮主动调用本函数；日志创建失败时不发送请求。"""
    if not api_key.strip() or not api_key.isascii() or any(c.isspace() for c in api_key):
        raise ValueError('请填写有效的火山方舟 API Key（不能包含空格或换行）。')
    if endpoint != API_URL:
        raise ValueError('本页面仅支持火山方舟北京地域官方生图地址。')
    ledger_dir = Path(budget_directory) if budget_directory is not None else (Path(directory) if directory is not None else data_dir())
    directory = Path(directory) if directory is not None else data_dir() / 'image_logs'
    prompt_spec = prompt_store.get('image', platform)
    payload = build_payload(platform, product, model, prompt_spec=prompt_spec)
    record = {'request_id': uuid.uuid4().hex, 'started_at': now_iso(),
              'platform': platform, 'product': product.strip(), 'endpoint': endpoint,
              'prompt_version': prompt_spec['version'], 'prompt': payload['prompt'],
              'payload_schema_version': 'seedream-single-v2',
              'request_payload': payload, 'status': 'started', 'response': None,
              'human_review': 'not_done', 'notice': NOTICE,
              'automatic_retries': 0, 'cost': None, 'cost_note': '按本次模型每张单价估算，共用人民币预算；官方账单为准。', 'request_sent': False}
    reserved = budget.reserve_image(ledger_dir, record['request_id'], model)
    record['budget_pricing_cny'] = reserved['pricing']
    try:
        path = save_record(record, directory, api_key)
    except OSError:
        budget.settle_image(ledger_dir, record['request_id'], None, False)
        raise
    outcome = {'record': record, 'image_bytes': None, 'error': None, 'save_error': None, 'log_path': str(path)}
    started, stage = time.monotonic(), 'api_request'
    generated_count = None
    try:
        budget.mark_sent(ledger_dir, record['request_id'])
        record['request_sent'] = True
        with requests.post(endpoint, headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
                           json=payload, timeout=(10, 180), stream=True, allow_redirects=False) as response:
            record['http_status'] = response.status_code
            record['provider_request_id'] = response.headers.get('X-Request-Id')
            raw = bounded_body(response, MAX_RESPONSE_BYTES)
            try:
                record['response'] = json.loads(raw)
            except (ValueError, UnicodeError):
                record['response'] = {'raw_text': raw.decode('utf-8', errors='replace')}
                response.raise_for_status()
                raise ValueError('API没有返回合法JSON。')
            generated_count = returned_image_count(record['response'])
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('当前代码仅支持同步返回图片的HTTP 200接口，不支持异步任务轮询。')
        stage = 'image_processing'
        content, dimensions, extension, mime = image_from_response(record['response'])
        outcome.update(image_bytes=content, extension=extension, mime=mime)
        record.update(status='succeeded', image_dimensions=list(dimensions),
                      image_sha256=hashlib.sha256(content).hexdigest())
        stage = 'local_image_save'
        image_path = directory / (record['request_id'] + '.' + extension)
        image_path.write_bytes(content)
        record['image_file'] = image_path.name
    except budget.BudgetError as exc:
        record['status'] = 'budget_error'
        outcome['error'] = str(exc)
    except requests.Timeout:
        record['status'] = 'timeout'
        outcome['error'] = '请求或下载超时；服务端可能已经生成并计费。请先核对日志和服务端记录，再决定是否重新提交。'
    except requests.HTTPError as exc:
        record['status'] = 'http_error'
        status = exc.response.status_code if exc.response is not None else '未知'
        outcome['error'] = http_error_message(status, record.get('response'))
    except requests.RequestException:
        record['status'] = 'network_error'
        outcome['error'] = '网络连接失败。程序没有自动重试；是否产生费用需核对服务端记录。'
    except (ValueError, TypeError, binascii.Error, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        record['status'] = 'invalid_response'
        outcome['error'] = str(exc)
    except OSError:
        record['status'] = 'local_save_error' if stage == 'local_image_save' else 'image_read_error'
        outcome['error'] = '图片保存或读取失败；如已显示图片，请直接下载，不要为此重复生成。'
    finally:
        if outcome['error']:
            outcome['error'] = outcome['error'].replace(api_key, '[REDACTED]')
        record.update(finished_at=now_iso(), elapsed_seconds=round(time.monotonic() - started, 3),
                      error=outcome['error'], failure_stage=stage if outcome['error'] else None)
        try:
            item = budget.settle_image(ledger_dir, record['request_id'], generated_count, record['request_sent'])
            record['generated_images'] = item['generated_images']
            record['budget_status'] = item['status']
            record['estimated_cost_cny'] = item['occupied_cny'] if item['status'] == 'settled_upper' else None
        except (budget.BudgetError, OSError):
            record['budget_status'] = 'settlement_failed'
            outcome['save_error'] = '用量对账写入失败，请保留日志核对；不要为此重复生成。'
        # 内存与落盘记录均做密钥屏蔽；下载JSON不泄露Authorization。
        outcome['record'] = json.loads(serialize(record, api_key))
        try:
            save_record(outcome['record'], directory)
        except OSError:
            outcome['save_error'] = '最终日志保存失败，请立即下载JSON备份；无需重新调用API。'
    return outcome


