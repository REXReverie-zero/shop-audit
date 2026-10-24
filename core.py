"""第一课：输入资料 -> 明确规则校验 -> 保存快照 -> 导出。这里不调用 AI。"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

APP_TITLE = "青岛小商家宣传文案与依据核对助手"
VERSION = "0.1.0"
CHANNELS = ["朋友圈", "小红书", "公众号"]
DATA_KINDS = ["模拟练习资料", "真实资料（尚未独立核实）"]
LIMITS = {"product": 80, "audience": 120, "fact": 400, "source": 300}
from session_storage import data_dir


def sample_task():
    """虚构的练习材料；不代表真实商家、访谈或反馈。"""
    return {
        "product": "海风笔记本（模拟商品）",
        "audience": "神",
        "channel": "朋友圈",
        "data_kind": DATA_KINDS[0],
        "sources": [
            {"id": "S01", "fact": "A5规格，每本80页。", "source": "模拟产品规格单，2026-10-17"},
            {"id": "S02", "fact": "单本售价12元。", "source": "模拟价目表，2026-10-17"},
        ],
    }


def validate_task(task, confirmed):
    """返回错误列表。通过只表示格式完整，不表示事实真实。"""
    errors = []
    for field, label in [("product", "商品名称"), ("audience", "目标受众")]:
        value = task.get(field, "")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"请填写{label}。")
        elif len(value) > LIMITS[field]:
            errors.append(f"{label}不能超过{LIMITS[field]}字。")
    if task.get("channel") not in CHANNELS:
        errors.append("请选择支持的传播渠道。")
    if task.get("data_kind") not in DATA_KINDS:
        errors.append("请选择资料性质。")
    rows = task.get("sources", [])
    if not isinstance(rows, list) or not rows:
        errors.append("请填写至少1条事实与资料来源，可按需增加部分。")
        rows = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            errors.append("每条资料必须包含编号、事实和来源。")
            continue
        sid = row.get("id")
        if (not isinstance(sid, str) or not re.fullmatch(r"S(?:0[1-9]|[1-9][0-9]+)", sid)
                or sid in seen):
            errors.append("资料编号必须按S01、S02等格式填写，且不能重复。")
        seen.add(str(sid))
        for field, label in [("fact", "商品事实"), ("source", "资料来源")]:
            value = row.get(field, "")
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{sid}：请填写{label}。")
            elif len(value) > LIMITS[field]:
                errors.append(f"{sid}：{label}不能超过{LIMITS[field]}字。")
    if confirmed is not True:
        errors.append("请先确认已逐条核对录入内容与资料编号。")
    return errors


def fingerprint(task):
    """给内容计算指纹；内容变化，指纹就变化。不是事实验证。"""
    text = json.dumps(task, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_record(task, confirmed, previous=None, actor="user"):
    errors = validate_task(task, confirmed)
    if errors:
        raise ValueError("\n".join(errors))
    # JSON复制避免之后修改输入时，连保存前的快照一起改掉。
    snapshot = json.loads(json.dumps(task, ensure_ascii=False))
    changed = [] if not previous else [
        k for k in snapshot if snapshot[k] != previous["input"].get(k)
    ]
    return {
        "schema_version": VERSION,
        "record_id": fingerprint({"input": snapshot, "actor": actor}),
        "input_fingerprint": fingerprint(snapshot),
        "snapshot_ready_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "input": snapshot,
        "check_status": {
            "format_rules": "passed", "input_confirmed_by_user": actor == "user",
            "confirmation_actor": actor,
            "source_truth": "not_verified", "model_review": "not_run",
            "copy_status": "not_generated",
        },
        "api_calls": [], "api_cost_cny": 0,
        "history": [{"action": "input_confirmed", "actor": actor,
                     "previous_record_id": previous["record_id"] if previous else None,
                     "changed_fields": changed}],
        "notice": "仅资料单；未生成文案，未进行模型复核。格式通过和人工确认不等于事实保证。",
    }


def json_bytes(record):
    return (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def text_bytes(record):
    task = record["input"]
    lines = [APP_TITLE, "资料单（非宣传成稿）", f"记录编号：{record['record_id']}",
             f"快照时间：{record['snapshot_ready_at']}", f"资料性质：{task['data_kind']}",
             f"商品：{task['product']}", f"目标受众：{task['audience']}",
             f"传播渠道：{task['channel']}", ""]
    for row in task["sources"]:
        lines.extend([f"[{row['id']}] {row['fact']}", f"来源：{row['source']}", ""])
    confirmation = "录入者已确认" if record["check_status"]["input_confirmed_by_user"] else "自动演示/测试确认，非真实用户确认"
    lines.extend([f"检查状态：格式规则通过；{confirmation}；资料真实性未核实；模型复核未执行。",
                  "模型调用：0次；API费用：0元人民币。", record["notice"]])
    # UTF-8 BOM方便Windows记事本识别中文。
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


def save_record(record, directory=None):
    """同一内容复用快照，重复点击或刷新不会新增记录。"""
    directory = data_dir() if directory is None else Path(directory)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['record_id']}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), path
    temp = directory / f"{uuid4().hex}.tmp"
    try:
        temp.write_bytes(json_bytes(record))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return record, path
