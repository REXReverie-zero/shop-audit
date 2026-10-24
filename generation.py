from session_storage import data_dir
"""第三课：一次文案生成、依据结构检查、磁盘去重与草稿导出。"""
import json
import os
import budget
import prompt_store
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from api_probe import MODEL, ProbeError, extract_json, load_key, run_json_call
from core import fingerprint, validate_task

PROMPT_VERSION = "generate-v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "generate_v1.txt"
MAX_OUTPUT_TOKENS = 1800


def generation_payload(task, *, prompt_spec=None):
    errors = validate_task(task, True)
    if errors:
        raise ProbeError("\n".join(errors))
    prompt_spec = prompt_spec or prompt_store.get("copy", task["channel"])
    return {"model": MODEL, "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"}, "stream": False,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "system", "content": prompt_spec["text"] + "\n固定输出约束：\n" + PROMPT_PATH.read_text(encoding="utf-8")},
                         {"role": "user", "content": json.dumps(task, ensure_ascii=False)}]}


def generation_id(task, payload=None):
    # 提示词实际内容和输出上限也参与去重，修改提示词后不复用旧生成。
    return fingerprint({"task": task, "payload": payload if payload is not None else generation_payload(task), "version": PROMPT_VERSION})


def validate_generation(data, task):
    result = extract_json(data)
    if set(result) != {"copy", "claims", "warnings"}:
        raise ProbeError("生成结果缺少字段或出现未约定字段。")
    copy, claims, warnings = result["copy"], result["claims"], result["warnings"]
    if not isinstance(copy, str) or not copy.strip() or len(copy) > 600:
        raise ProbeError("文案为空或超过600字。")
    if not isinstance(warnings, list) or len(warnings) > 8 or any(
            not isinstance(w, str) or not w.strip() or len(w) > 400 for w in warnings):
        raise ProbeError("待确认提示格式不正确。")
    if not isinstance(claims, list) or len(claims) > 12:
        raise ProbeError("事实映射格式不正确或超过12项。")
    if not claims and (copy != "资料不足，请补充或修正资料后再生成。" or not warnings):
        raise ProbeError("有文案却没有关键事实的依据映射。")
    sources = {r["id"]: r["fact"] for r in task["sources"]}
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"text", "source_ids", "evidence"}:
            raise ProbeError("关键事实的字段不正确。")
        text, ids, evidence = claim["text"], claim["source_ids"], claim["evidence"]
        if not isinstance(text, str) or not text.strip() or text not in copy:
            raise ProbeError("关键事实描述没有逐字出现在文案中。")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= len(sources)
                or any(not isinstance(sid, str) or sid not in sources for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ProbeError("引用了不存在、重复或无效的资料编号。")
        if not isinstance(evidence, list) or len(evidence) != len(ids):
            raise ProbeError("每个引用编号都需要一条原文摘录。")
        covered = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"source_id", "quote"}:
                raise ProbeError("依据摘录格式不正确。")
            sid, quote = item["source_id"], item["quote"]
            if not isinstance(sid, str) or sid not in ids or sid in covered:
                raise ProbeError("依据摘录编号无效或重复。")
            if not isinstance(quote, str) or not quote.strip() or quote not in sources[sid]:
                raise ProbeError("依据摘录不是对应资料中的原文。")
            covered.add(sid)
    return result


def read_generation(task, directory=None, *, payload=None):
    directory = data_dir() if directory is None else Path(directory)
    path = Path(directory) / "generations" / f"{generation_id(task, payload)}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record["input_fingerprint"] != fingerprint(task):
            raise ValueError()
        if record["status"] == "ok":
            validate_generation({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(record["result"], ensure_ascii=False)}}]}, task)
        elif record["status"] not in {"started", "error"}:
            raise ValueError()
        return record
    except (OSError, ValueError, KeyError, TypeError, ProbeError):
        raise ProbeError("已存生成记录不可读取。为避免重复扣费，已停止；请保留文件并排查。") from None


def write_atomic(path, record):
    temporary = path.with_name(uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_once(input_record, directory=None, *, caller=run_json_call,
                  key_loader=load_key):
    directory = data_dir() if directory is None else Path(directory)
    task = input_record["input"]
    errors = validate_task(task, input_record["check_status"]["input_confirmed_by_user"])
    if errors or input_record["input_fingerprint"] != fingerprint(task):
        raise ProbeError("资料尚未确认或已经改动，请重新检查并保存。")
    prompt_spec = prompt_store.get("copy", task["channel"])
    payload = generation_payload(task, prompt_spec=prompt_spec)
    prompt_version = prompt_spec["version"]
    gid = generation_id(task, payload)
    existing = read_generation(task, directory, payload=payload)
    if existing:
        # 包括失败和未知状态；不自动重复发送。
        return existing, True
    try:
        budget.preflight(directory, payload)
    except budget.BudgetError as exc:
        raise ProbeError(str(exc)) from None
    key = key_loader()
    folder = Path(directory) / "generations"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{gid}.json"
    record = {"generation_id": gid, "input_fingerprint": fingerprint(task),
              "input_record_id": input_record["record_id"], "input": task,
              "prompt_version": prompt_version, "prompt_fingerprint": fingerprint(payload),
              "payload": payload, "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "started", "check_status": {"source_truth": "not_verified",
              "mapping_structure": "not_checked", "semantic_support": "not_verified",
              "claim_coverage": "not_verified", "model_review": "not_run",
              "human_copy_confirmation": "not_done"}, "human_changes": []}
    # 原子创建预约记录；多标签页同时点击时只有一个能进入付费调用。
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise ProbeError("这份资料已有生成任务，请刷新查看；未重复发送请求。") from None
    try:
        event = caller(key, Path(directory) / "api_calls.jsonl", payload,
                       prompt_version, "copy_generation",
                       lambda data: validate_generation(data, task),
                       context={"input_fingerprint": fingerprint(task), "generation_id": gid})
        record["call"] = {k: v for k, v in event.items() if k != "result"}
        record["status"] = event["status"]
        if event["status"] == "ok":
            record["result"] = event["result"]
            record["check_status"]["mapping_structure"] = "passed"
        else:
            record["error"] = event["error"]
        write_atomic(path, record)
    except OSError:
        # 可能已经计费。磁盘的started状态继续阻止重复请求。
        raise ProbeError("本地记录保存失败，可能已计费；请勿重试，保留data目录用于排查。") from None
    return record, False


def draft_text(record):
    result = record["result"]
    lines = ["宣传文案草稿（未复核，未完成人工确认）", "资料性质：" + record["input"]["data_kind"],
             "生成编号：" + record["generation_id"], "提示词版本：" + record["prompt_version"],
             "检查：仅编号、摘录及结构检查通过；语义支持和事实覆盖未验证；真实性未核实。",
             "", result["copy"], "", "模型提供的关键事实与依据（需人工检查）："]
    for claim in result["claims"]:
        lines.append(claim["text"] + " → " + "、".join(claim["source_ids"]))
        for evidence in claim["evidence"]:
            lines.append(f"  [{evidence['source_id']}] {evidence['quote']}")
    lines.extend(["", "完整原始资料："])
    for row in record["input"]["sources"]:
        lines.append(f"[{row['id']}] {row['fact']} 来源：{row['source']}")
    lines.extend(["", "模型提示：", *result["warnings"],
                  "", "调用记录：" + json.dumps(record.get("call", {}), ensure_ascii=False)])
    return ("\n".join(lines) + "\n").encode("utf-8-sig")
