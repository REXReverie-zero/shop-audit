"""人民币保守预算账本；只覆盖当前项目目录，不是账户消费限额。"""
import copy
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

DEFAULT_LIMIT_CNY = "35.00"
INPUT_CONTEXT_BOUND = 1048576
MAX_OUTPUT_BOUND = 2200
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# 官方人民币高峰、输入未命中缓存价；不是美元汇率换算。
DEFAULT_PRICING = {"input_cny_per_million": "2", "output_cny_per_million": "8",
                   "checked_date": "2026-10-18", "valid_until": "2026-10-25"}
HISTORICAL_PRICING = dict(DEFAULT_PRICING, checked_date="2026-10-17", valid_until="2026-10-24")
STATES = {"reserved", "unknown", "settled_upper", "covered_by_baseline"}


class BudgetError(ValueError):
    pass


def today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("1000000000000"):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise BudgetError("人民币预算金额无效，已暂停请求。") from None


def round_up(value):
    return format(value.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), ".8f")


def cost_upper(usage, pricing):
    if not isinstance(usage, dict):
        raise BudgetError("缺少完整用量，费用待对账。")
    prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if (type(prompt) is not int or type(output) is not int or prompt < 0 or output < 0
            or prompt > INPUT_CONTEXT_BOUND or output > MAX_OUTPUT_BOUND
            or usage.get("total_tokens", prompt + output) != prompt + output):
        raise BudgetError("用量字段无效，费用待对账。")
    return (Decimal(prompt) * amount(pricing["input_cny_per_million"])
            + Decimal(output) * amount(pricing["output_cny_per_million"])) / Decimal(1000000)


def read_events(directory):
    path = Path(directory) / "api_calls.jsonl"
    if not path.exists():
        return {}
    last = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("call_id"), str):
                raise ValueError()
            last[event["call_id"]] = event
    except (OSError, ValueError, TypeError):
        raise BudgetError("调用日志不完整或损坏，请保留文件并对账，不能按零费用继续。") from None
    return last


def historical_entry(event):
    item = {"source": "historical_log", "status": "unknown", "occupied_cny": "0"}
    if event is None:
        return item
    if event.get("request_sent") is False:
        item["status"] = "settled_upper"
        return item
    try:
        if event.get("requested_model") != "deepseek-flash" or event.get("status") == "started":
            raise BudgetError("历史费用不明。")
        pricing = event.get("budget_pricing_cny")
        if pricing is None:
            if event.get("started_at", "")[:10] != "2026-10-17":
                raise BudgetError("缺少当时人民币单价。")
            pricing = HISTORICAL_PRICING
        item["pricing"] = copy.deepcopy(pricing)
        item["occupied_cny"] = round_up(cost_upper(event.get("usage"), pricing))
        item["status"] = "settled_upper"
    except (BudgetError, KeyError, TypeError):
        pass
    return item


def new_document():
    return {"schema": 2, "currency": "CNY", "limit_cny": DEFAULT_LIMIT_CNY,
            "pricing": copy.deepcopy(DEFAULT_PRICING), "baseline_cny": "0", "entries": {},
            "reconciliations": [], "settings_history": [], "needs_reconciliation": False}


def migrate_legacy(old, events):
    """保留原账本，已知用量按人民币价重算；未知占用继续阻止调用。"""
    if (not isinstance(old.get("entries"), dict) or "baseline_usd" not in old
            or "limit_usd" not in old):
        raise BudgetError("旧预算账本损坏，请保留文件并排查。")
    document = new_document()
    document["legacy_usd_ledger"] = copy.deepcopy(old)
    document["needs_reconciliation"] = amount(old["baseline_usd"]) > 0
    for call_id, original in old["entries"].items():
        if original["status"] not in STATES:
            raise BudgetError("旧预算账本状态无效。")
        amount(original["occupied_usd"])
        item = historical_entry(events.get(call_id))
        item["legacy_entry"] = copy.deepcopy(original)
        if original["status"] in {"reserved", "unknown"}:
            item["status"] = "unknown"
        elif original["status"] == "covered_by_baseline":
            item.update(status="covered_by_baseline", occupied_cny="0")
        document["entries"][call_id] = item
    return document


def load(directory):
    path = Path(directory) / "budget.json"
    events = read_events(directory)
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else new_document()
        if document["schema"] == 1:
            document = migrate_legacy(document, events)
        if (document["schema"] != 2 or document["currency"] != "CNY"
                or not isinstance(document["entries"], dict)
                or type(document["needs_reconciliation"]) is not bool):
            raise ValueError()
        amount(document["baseline_cny"])
        amount(document["limit_cny"])
        if (amount(document["pricing"]["input_cny_per_million"]) <= 0
                or amount(document["pricing"]["output_cny_per_million"]) <= 0):
            raise ValueError()
        date.fromisoformat(document["pricing"]["checked_date"])
        date.fromisoformat(document["pricing"]["valid_until"])
        warning = document.get("warning_percent", 80)
        if type(warning) is not int or not 1 <= warning <= 100:
            raise ValueError()
        pricing = document.get("image_pricing")
        if pricing is not None:
            if not isinstance(pricing, dict) or amount(pricing["cny_per_image"]) <= 0 or not isinstance(pricing["model"], str):
                raise ValueError()
            date.fromisoformat(pricing["valid_until"])
        for item in document["entries"].values():
            amount(item["occupied_cny"])
            if item.get("kind") == "image":
                if type(item.get("generated_images")) is not int or not 0 <= item["generated_images"] <= 1:
                    raise ValueError()
                if type(item.get("request_sent")) is not bool:
                    raise ValueError()
            if item["status"] not in STATES:
                raise ValueError()
        for call_id, event in events.items():
            if call_id not in document["entries"] and event.get("request_sent") is not False:
                document["entries"][call_id] = historical_entry(event)
        return document
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, BudgetError):
            raise
        raise BudgetError("预算账本无法读取，请保留文件并排查。") from None


def status(directory):
    document = load(directory)
    occupied = amount(document["baseline_cny"]) + sum(
        (amount(item["occupied_cny"]) for item in document["entries"].values()), Decimal(0))
    limit = amount(document["limit_cny"])
    pending = sum(item["status"] in {"unknown", "reserved"} for item in document["entries"].values())
    reason = None
    if pending or document["needs_reconciliation"]:
        reason = "有进行中或费用不明的请求，已暂停新请求；请等待完成或用人民币人工对账。"
    elif today().isoformat() > document["pricing"]["valid_until"]:
        reason = "价格核对记录已过期，请核对官方人民币价格后更新。"
    elif occupied >= limit:
        reason = "本项目人民币预算已用尽，可修改预算上限。"
    settled = amount(document["baseline_cny"]) + sum(
        (amount(item["occupied_cny"]) for item in document["entries"].values()
         if item["status"] == "settled_upper"), Decimal(0))
    images = [item for item in document["entries"].values() if item.get("kind") == "image"]
    events = read_events(directory)
    text_ids = set(events) | {key for key, item in document["entries"].items() if item.get("kind", "text") == "text"}
    text_calls = sum(events.get(key, {}).get("request_sent", document["entries"].get(key, {}).get("request_sent", True)) is not False for key in text_ids)
    return {"estimated_cny": round_up(settled), "image_generated": sum(item.get("generated_images", 0) for item in images),
            "image_requested": sum(item.get("requested_images", 1) for item in images if item.get("request_sent") is True),
            "image_calls": sum(item.get("request_sent") is True for item in images), "text_calls": text_calls,
            "warning_percent": document.get("warning_percent", 80),
            "image_pricing": document.get("image_pricing"),
            "limit_cny": str(limit), "occupied_upper_cny": round_up(occupied),
            "available_cny": round_up(max(limit - occupied, Decimal(0))), "pending_count": pending,
            "blocked_reason": reason, "pricing": document["pricing"],
            "legacy_migrated": "legacy_usd_ledger" in document,
            "needs_reconciliation": document["needs_reconciliation"]}


def reservation(payload, pricing):
    output = payload.get("max_tokens")
    if (payload.get("model") != "deepseek-flash" or type(output) is not int
            or not 1 <= output <= MAX_OUTPUT_BOUND or payload.get("thinking") != {"type": "disabled"}
            or payload.get("tools") or len(json.dumps(payload).encode("utf-8")) > 65536):
        raise BudgetError("请求超出预算控制支持的范围，未发送。资料较多时请分批处理。")
    return cost_upper({"prompt_tokens": INPUT_CONTEXT_BOUND, "completion_tokens": output}, pricing)


def preflight(directory, payload):
    info = status(directory)
    if info["blocked_reason"]:
        raise BudgetError(info["blocked_reason"])
    value = reservation(payload, info["pricing"])
    if amount(info["available_cny"]) < value:
        raise BudgetError("剩余人民币预算不足以预留本次请求费用，未发送。")
    return value


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "budget.lock"
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        raise BudgetError("预算账本正在使用或上次写入被中断，未发送请求。") from None
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def save(directory, document):
    from generation import write_atomic
    write_atomic(Path(directory) / "budget.json", document)


def reserve(directory, call_id, payload):
    with locked(directory):
        value = preflight(directory, payload)
        document = load(directory)
        if call_id in document["entries"]:
            raise BudgetError("请求编号已存在，未重复发送。")
        document["entries"][call_id] = {"status": "reserved", "occupied_cny": round_up(value),
            "source": "request_reservation", "pricing": copy.deepcopy(document["pricing"]),
            "max_tokens": payload["max_tokens"], "kind": "text", "request_sent": False}
        save(directory, document)
    return round_up(value)


def settle(directory, call_id, usage):
    with locked(directory):
        document = load(directory)
        item = document["entries"][call_id]
        try:
            value = cost_upper(usage, item["pricing"])
            if (not isinstance(usage, dict) or usage["completion_tokens"] > item["max_tokens"]
                    or value > amount(item["occupied_cny"])):
                raise BudgetError("用量超过预留范围。")
            item["occupied_cny"] = round_up(value)
            item["status"] = "settled_upper"
        except (BudgetError, KeyError):
            item["status"] = "unknown"
        save(directory, document)
    return item


def reconcile(directory, historical_upper_cny, note, confirmed):
    if confirmed is not True or not isinstance(note, str) or not note.strip() or len(note) > 600:
        raise BudgetError("请确认请求均已停止，并填写人民币对账依据。")
    value = amount(historical_upper_cny)
    with locked(directory):
        document = load(directory)
        document["reconciliations"].append({"recorded_at": datetime.now(timezone.utc).isoformat(),
            "actor": "user", "note": note.strip(), "historical_upper_cny": round_up(value),
            "previous_baseline_cny": document["baseline_cny"], "previous_entries": copy.deepcopy(document["entries"])})
        document["baseline_cny"] = round_up(value)
        document["needs_reconciliation"] = False
        for item in document["entries"].values():
            item["status"] = "covered_by_baseline"
            item["occupied_cny"] = "0"
        save(directory, document)


def update_limit(directory, limit_cny):
    value = amount(limit_cny)
    with locked(directory):
        document = load(directory)
        document["settings_history"].append({"recorded_at": datetime.now(timezone.utc).isoformat(),
            "old_limit_cny": document["limit_cny"], "new_limit_cny": str(value)})
        document["limit_cny"] = str(value)
        save(directory, document)


def update_pricing(directory, input_rate, output_rate, confirmed):
    incoming, outgoing = amount(input_rate), amount(output_rate)
    if confirmed is not True or incoming <= 0 or outgoing <= 0:
        raise BudgetError("请先核对官方人民币高峰单价并确认。")
    with locked(directory):
        document = load(directory)
        document["pricing"] = {"input_cny_per_million": str(incoming), "output_cny_per_million": str(outgoing),
                               "checked_date": today().isoformat(), "valid_until": (today() + timedelta(days=7)).isoformat()}
        save(directory, document)


# ==================== 图片与文案共用同一本人民币账本 ====================
def mark_sent(directory, call_id):
    with locked(directory):
        document = load(directory)
        document["entries"][call_id]["request_sent"] = True
        save(directory, document)


def update_warning(directory, percent):
    if type(percent) is not int or not 1 <= percent <= 100:
        raise BudgetError("预警阈值必须为1—100的整数百分比。")
    with locked(directory):
        document = load(directory)
        document["warning_percent"] = percent
        save(directory, document)


def update_image_pricing(directory, price, model, confirmed):
    value = amount(price)
    if confirmed is not True or value <= 0 or not isinstance(model, str) or not model.strip():
        raise BudgetError("请填写已核对的每张人民币单价、模型ID并确认。")
    with locked(directory):
        document = load(directory)
        document["image_pricing"] = {"cny_per_image": str(value), "model": model.strip(),
            "checked_date": today().isoformat(), "valid_until": (today() + timedelta(days=7)).isoformat()}
        save(directory, document)


def reserve_image(directory, call_id, model):
    with locked(directory):
        info = status(directory)
        # 图片不依赖文本单价有效期，其他余额/待对账限制仍共用。
        if info["blocked_reason"] and not info["blocked_reason"].startswith("价格核对记录已过期"):
            raise BudgetError(info["blocked_reason"])
        document = load(directory)
        pricing = document.get("image_pricing")
        if (not pricing or pricing.get("model") != model or today().isoformat() > pricing.get("valid_until", "")):
            raise BudgetError("请先在侧栏核对并保存当前Seedream模型的每张单价。")
        price = amount(pricing["cny_per_image"])
        if price <= 0 or amount(info["available_cny"]) < price:
            raise BudgetError("剩余预算不足以覆盖本次1张图片费用，未发送请求。")
        if call_id in document["entries"]:
            raise BudgetError("图片调用编号重复，未发送请求。")
        item = {"kind": "image", "source": "seedream", "status": "reserved", "occupied_cny": round_up(price),
                "pricing": copy.deepcopy(pricing), "requested_images": 1, "generated_images": 0, "request_sent": False}
        document["entries"][call_id] = item
        save(directory, document)
        return copy.deepcopy(item)


def settle_image(directory, call_id, count, request_sent):
    with locked(directory):
        document = load(directory)
        item = document["entries"][call_id]
        item["request_sent"] = bool(request_sent)
        if not request_sent:
            item.update(status="settled_upper", occupied_cny="0.00000000", generated_images=0)
        elif type(count) is int and 0 <= count <= 1:
            item.update(status="settled_upper", generated_images=count,
                        occupied_cny=round_up(Decimal(count) * amount(item["pricing"]["cny_per_image"])))
        else:
            # 超时或没有可信用量，不能把未知消费记成0。保留预留并暂停后续调用。
            item["status"] = "unknown"
        save(directory, document)
        return copy.deepcopy(item)


def history_rows(directory):
    document = load(directory)
    return [{"调用编号": key, "类型": "图片" if item.get("kind") == "image" else "文本",
             "状态": item["status"], "图片张数": item.get("generated_images"),
             "人民币占用": item["occupied_cny"]} for key, item in document["entries"].items()]


def import_image_logs(directory, folders):
    """按调用ID去重导入旧图片日志；无当时单价时保留张数、标记费用待对账。"""
    from image_generation import returned_image_count
    with locked(directory):
        document = load(directory)
        imported = 0
        for folder in folders:
            for path in sorted(Path(folder).glob('*.json')):
                try:
                    record = json.loads(path.read_text(encoding='utf-8'))
                    call_id = record['request_id']
                    if not isinstance(call_id, str) or not call_id or 'request_payload' not in record:
                        raise ValueError()
                    if call_id in document['entries']:
                        continue
                    count = returned_image_count(record.get('response'))
                    sent = record.get('request_sent', True) is not False
                    pricing = record.get('budget_pricing_cny')
                    known = (not sent or (type(count) is int and isinstance(pricing, dict)
                             and amount(pricing.get('cny_per_image')) > 0))
                    value = Decimal(count) * amount(pricing['cny_per_image']) if known and sent else Decimal(0)
                    document['entries'][call_id] = {'kind': 'image', 'source': 'imported_image_log',
                        'status': 'settled_upper' if known else 'unknown', 'occupied_cny': round_up(value),
                        'request_sent': sent, 'requested_images': 1, 'generated_images': count or 0,
                        'pricing': pricing, 'imported_file': path.name}
                    imported += 1
                except (ValueError, KeyError, TypeError, OSError):
                    raise BudgetError('旧图片日志无法解析，请保留并检查：' + path.name) from None
        save(directory, document)
        return imported
