from session_storage import data_dir
"""第四课：对照原始资料复核草稿；模型判断不是事实保证。"""
import json
import budget
from datetime import datetime, timezone
from pathlib import Path

from api_probe import MODEL, ProbeError, extract_json, load_key, run_json_call
from core import fingerprint, validate_task
from generation import write_atomic

PROMPT_VERSION = "review-v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review_v1.txt"
STATUS_LABELS = {"supported": "资料支持（模型判断）", "unsupported": "无资料依据",
                 "contradiction": "与资料矛盾", "needs_confirmation": "待人工确认"}


def review_payload(generated):
    if generated.get("status") != "ok":
        raise ProbeError("只能复核成功生成的草稿。")
    task = generated["input"]
    if validate_task(task, True) or generated["input_fingerprint"] != fingerprint(task):
        raise ProbeError("原始资料已改动或格式不完整，不能沿用本次草稿复核。")
    draft = generated["result"]["copy"]
    if not isinstance(draft, str) or not draft.strip() or len(draft) > 600:
        raise ProbeError("待复核文案为空或超过600字。")
    return {"model": MODEL, "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"}, "max_tokens": 2200, "stream": False,
            "messages": [{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                         {"role": "user", "content": json.dumps(
                             {"original_task": task, "draft": draft}, ensure_ascii=False)}]}


def review_id(generated):
    return fingerprint({"payload": review_payload(generated), "version": PROMPT_VERSION,
                        "generation_id": generated["generation_id"]})


def validate_review(data, generated):
    result = extract_json(data)
    if set(result) != {"checks", "input_warnings", "summary"}:
        raise ProbeError("复核结果缺少字段或出现未约定字段。")
    checks, warnings, summary = result["checks"], result["input_warnings"], result["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise ProbeError("复核总结为空或过长。")
    if not isinstance(warnings, list) or len(warnings) > 8 or any(
            not isinstance(w, str) or not w.strip() or len(w) > 400 for w in warnings):
        raise ProbeError("原始资料提醒的格式不正确。")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
        raise ProbeError("复核必须返回1至20条逐项检查。")
    draft = generated["result"]["copy"]
    valid_ids = {r["id"] for r in generated["input"]["sources"]}
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"text", "status", "source_ids", "reason"}:
            raise ProbeError("逐项复核字段不正确。")
        text, status, ids, reason = (check[k] for k in ["text", "status", "source_ids", "reason"])
        if not isinstance(text, str) or not text.strip() or text not in draft:
            raise ProbeError("复核引用的描述没有逐字出现在当前文案中。")
        if not isinstance(status, str) or status not in STATUS_LABELS:
            raise ProbeError("出现未约定的复核分类。")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
            raise ProbeError("复核判断缺少理由或理由过长。")
        if (not isinstance(ids, list) or len(ids) > len(valid_ids)
                or any(not isinstance(sid, str) or sid not in valid_ids for sid in ids)
                or len(ids) != len(set(ids))):
            raise ProbeError("复核引用了不存在或重复的资料编号。")
        if status in {"supported", "contradiction"} and not ids:
            raise ProbeError("支持或矛盾的判断必须给出对应资料编号。")
    return result


def assessment(result):
    flagged = any(c["status"] != "supported" for c in result["checks"]) or bool(result["input_warnings"])
    return "flags_reported" if flagged else "no_flags_reported"


def read_review(generated, directory=None):
    directory = data_dir() if directory is None else Path(directory)
    rid = review_id(generated)
    path = Path(directory) / "reviews" / f"{rid}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record["review_id"] != rid or record["draft_fingerprint"] != fingerprint(generated["result"]["copy"]):
            raise ValueError()
        if record["status"] == "ok":
            validate_review({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(record["result"], ensure_ascii=False)}}]}, generated)
            # 判断状态从内容重新计算，不能相信文件里自称通过。
            record["check_status"]["model_review"] = assessment(record["result"])
        elif record["status"] not in {"started", "error"}:
            raise ValueError()
        return record
    except (OSError, ValueError, KeyError, TypeError, ProbeError):
        raise ProbeError("复核记录无法读取，已停止重复调用；请保留文件并排查。") from None


def review_once(generated, directory=None, *, caller=run_json_call, key_loader=load_key):
    directory = data_dir() if directory is None else Path(directory)
    payload = review_payload(generated)
    existing = read_review(generated, directory)
    if existing:
        return existing, True
    try:
        budget.preflight(directory, payload)
    except budget.BudgetError as exc:
        raise ProbeError(str(exc)) from None
    rid = review_id(generated)
    key = key_loader()
    folder = Path(directory) / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{rid}.json"
    record = {"review_id": rid, "generation_id": generated["generation_id"],
              "base_generation_id": generated.get("base_generation_id", generated["generation_id"]),
              "input_fingerprint": fingerprint(generated["input"]),
              "draft_fingerprint": fingerprint(generated["result"]["copy"]),
              "input": generated["input"], "draft": generated["result"]["copy"],
              "generation_call": generated.get("call"),
              "prompt_version": PROMPT_VERSION, "payload": payload,
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "started",
              "check_status": {"source_truth": "not_verified", "model_review": "not_completed",
                               "coverage": "not_guaranteed", "human_confirmation": "not_done"},
              "human_changes": generated.get("human_changes", [])}
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise ProbeError("同一版本已有复核任务，未重复发送；请刷新查看。") from None
    try:
        event = caller(key, Path(directory) / "api_calls.jsonl", payload, PROMPT_VERSION,
                       "copy_review", lambda data: validate_review(data, generated),
                       context={"input_fingerprint": record["input_fingerprint"],
                                "generation_id": generated["generation_id"], "review_id": rid,
                                "human_changes": generated.get("human_changes", [])})
        record["call"] = {k: v for k, v in event.items() if k != "result"}
        record["status"] = event["status"]
        if event["status"] == "ok":
            record["result"] = event["result"]
            record["check_status"]["model_review"] = assessment(event["result"])
        else:
            record["error"] = event["error"]
        write_atomic(path, record)
    except OSError:
        raise ProbeError("复核记录保存失败，可能已计费；请勿重试，保留data目录排查。") from None
    return record, False


def review_text(record):
    result = record["result"]
    heading = "模型标出了待处理项" if assessment(result) == "flags_reported" else "模型未报告问题（不代表全部核验通过）"
    lines = ["宣传草稿与模型复核报告（人工确认未完成，非最终发布稿）",
             "资料性质：" + record["input"]["data_kind"], "复核编号：" + record["review_id"],
             "提示词版本：" + record["prompt_version"], "状态：" + heading,
             "资料真实性未核实；模型可能漏检或误判。", "", record["draft"], "", "逐项复核："]
    for check in result["checks"]:
        lines.extend([f"[{STATUS_LABELS[check['status']]}] {check['text']}",
                      "依据编号：" + ("、".join(check["source_ids"]) or "无"), "理由：" + check["reason"]])
    lines.extend(["", "原始资料提醒：", *result["input_warnings"], "", "复核总结：", result["summary"],
                  "", "完整原始资料："])
    for row in record["input"]["sources"]:
        lines.append(f"[{row['id']}] {row['fact']} 来源：{row['source']}")
    lines.extend(["", "生成调用：" + json.dumps(record.get("generation_call"), ensure_ascii=False),
                  "复核调用：" + json.dumps(record.get("call"), ensure_ascii=False)])
    return ("\n".join(lines) + "\n").encode("utf-8-sig")
