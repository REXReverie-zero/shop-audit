from session_storage import data_dir
"""第二课：固定小输入、一次请求、严格检查 JSON；只使用 Python 标准库。"""
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
import uuid
import budget
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "deepseek-flash"
URL = "https://api.deepseek.com/chat/completions"
PROMPT_VERSION = "connectivity-v1"
TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 65536
SYSTEM_PROMPT = 'Return only a JSON object. Example JSON output: {"status":"ok"}.'
USER_PROMPT = 'Connectivity test only. Return exactly {"status":"ok"}.'


class ProbeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def load_key():
    # 网页会话的输入优先；本机配置只由侧栏“保存到本机”按钮写入。
    try:
        import streamlit as st
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx(suppress_warning=True) is not None and "text_api_key" in st.session_state:
            key = st.session_state["text_api_key"].strip()
            if not key or not key.isascii() or any(c.isspace() for c in key):
                raise ProbeError("请在侧栏填写有效的文本API密钥。")
            return key
    except ImportError:
        pass
    try:
        import local_api_settings
        key = local_api_settings.effective(ROOT)['text_api_key']
    except (OSError, ValueError):
        raise ProbeError("无法读取本机API配置，请在侧栏重新填写并保存。") from None
    if not isinstance(key, str) or not key.strip() or not key.isascii() or any(c.isspace() for c in key):
        raise ProbeError("密钥配置为空或格式异常。")
    return key.strip()


def request_payload():
    return {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": USER_PROMPT}],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 128,
        "stream": False,
    }


def post_once(key, payload):
    request = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    # 不跟随重定向，不自动重试；超时也可能已计费。
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProbeError("响应超过大小限制。")
        return body


def safe_metadata(data, event):
    if not isinstance(data, dict):
        return
    model = data.get("model")
    if isinstance(model, str) and 0 < len(model) <= 100 and all(c.isalnum() or c in "-_." for c in model):
        event["returned_model"] = model
    usage = data.get("usage")
    if isinstance(usage, dict):
        event["usage"] = {k: v for k, v in usage.items()
                          if k in {"prompt_tokens", "completion_tokens", "total_tokens",
                                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                          and type(v) is int and v >= 0}


def check_response(data):
    result = extract_json(data)
    if result != {"status": "ok"}:
        raise ProbeError("JSON 字段或取值不符合本次测试要求。")
    return result


def extract_json(data, max_chars=12000):
    """JSON格式和业务字段分开检查，供连通测试与文案生成共用。"""
    try:
        choice = data["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ProbeError("输出未正常结束（可能被截断），不能判为成功。")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ProbeError("模型返回了空内容。")
        if len(content) > max_chars:
            raise ProbeError("模型内容超过长度限制。")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ProbeError("模型应返回 JSON 对象。")
        return result
    except (KeyError, IndexError, TypeError):
        raise ProbeError("响应缺少必要字段或字段类型不正确。") from None
    except json.JSONDecodeError:
        raise ProbeError("模型内容不是有效 JSON。") from None


def run_probe(key, log_path, transport=post_once):
    return run_json_call(key, log_path, request_payload(), PROMPT_VERSION,
                         "connectivity_test", check_response, transport=transport)


def run_json_call(key, log_path, payload, prompt_version, kind, validator,
                  context=None, transport=post_once):
    event = {
        "call_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "requested_model": payload["model"], "prompt_version": prompt_version,
        "kind": kind, "attempt": 1, "automatic_retries": 0,
        "usage": None, "status": "error", "currency": "CNY", "cost_cny": None,
        "cost_note": "费用未计算，以官方账单为准；失败请求也可能计费。",
        "human_changes": [], "request_payload": payload,
    }
    if context:
        event["input_fingerprint"] = context["input_fingerprint"]
        event["generation_id"] = context["generation_id"]
        if "review_id" in context:
            event["review_id"] = context["review_id"]
        if "human_changes" in context:
            event["human_changes"] = context["human_changes"]
    # 请求前确保日志可写；不把密钥、请求头、原始错误响应写入日志。
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        try:
            event["reserved_upper_cny"] = budget.reserve(log_path.parent, event["call_id"], payload)
            event["budget_pricing_cny"] = budget.load(log_path.parent)["entries"][event["call_id"]]["pricing"]
        except budget.BudgetError as exc:
            raise ProbeError(str(exc)) from None
        started = dict(event, status="started")
        log.write(json.dumps(started, ensure_ascii=False) + "\n")
        log.flush()
        begin = time.monotonic()
        try:
            budget.mark_sent(log_path.parent, event["call_id"])
            event["request_sent"] = True
            raw = transport(key, payload)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProbeError("响应超过大小限制。")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ProbeError("接口响应不是有效 JSON。") from None
            safe_metadata(data, event)
            event["result"] = validator(data)
            event["status"] = "ok"
        except urllib.error.HTTPError as exc:
            event["http_status"] = exc.code
            event["error"] = {401: "密钥无效，请检查本地配置。",
                              402: "余额不足，请在官方平台检查。",
                              429: "触发频率或配额限制，请稍后手动测试。"}.get(
                                  exc.code, "官方接口返回 HTTP 错误，请检查接口状态和参数。")
        except (TimeoutError, urllib.error.URLError, OSError):
            event["error"] = "连接失败或超时；未自动重试。请求可能已到达服务端。"
        except ProbeError as exc:
            event["error"] = str(exc)
        finally:
            event["elapsed_seconds"] = round(time.monotonic() - begin, 3)
            try:
                settled = budget.settle(log_path.parent, event["call_id"], event["usage"])
                event["cost_upper_cny"] = settled["occupied_cny"] if settled["status"] == "settled_upper" else None
                event["budget_status"] = settled["status"]
                event["cost_note"] = "预算按高峰未命中价保守占用；不是实际账单。费用不明时暂停新请求。"
            except (budget.BudgetError, OSError):
                event["budget_status"] = "settlement_failed"
            event = json.loads(json.dumps(event, ensure_ascii=False).replace(key, "[REDACTED]"))
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            log.flush()
    return event


def main():
    try:
        key = load_key()
        print("将向 DeepSeek 官方接口发送 1 次小请求，可能产生少量费用。")
        print("输入仅含固定连通测试；不含商家资料。自动重试次数为 0。")
        if input("输入大写 YES 后按回车开始，其他输入取消：").strip() != "YES":
            print("已取消，没有发送请求。")
            return 0
        event = run_probe(key, data_dir() / "api_calls.jsonl")
        if event["status"] == "ok":
            print('连通测试通过：{"status":"ok"}')
        else:
            print("测试未通过：" + event["error"])
        print("耗时（秒）：", event["elapsed_seconds"])
        print("token 用量：", event["usage"])
        print("记录已保存到 data/api_calls.jsonl；费用以官方账单为准。")
        return 0 if event["status"] == "ok" else 1
    except ProbeError as exc:
        print(str(exc))
        return 1
    except OSError:
        print("本地记录写入失败，请检查 data 目录；请求可能已经发送，请勿连续重试。")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已中止；若已发送请求，可能已计费。未自动重试。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
