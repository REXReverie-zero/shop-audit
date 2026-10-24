from session_storage import data_dir
"""第五课：保存人工修改版本，绑定复核，保存确认后的导出快照。"""
import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from api_probe import ProbeError
from core import fingerprint
from generation import write_atomic
from review import assessment, review_id, validate_review


def now():
    return datetime.now(timezone.utc).isoformat()


def workspace_path(base, directory):
    return Path(directory) / "edits" / (base["generation_id"] + ".json")


def load_workspace(base, directory=None):
    directory = data_dir() if directory is None else Path(directory)
    path = workspace_path(base, directory)
    if not path.exists():
        return {"base_generation_id": base["generation_id"], "input_fingerprint": base["input_fingerprint"],
                "revisions": [{"version_id": base["generation_id"], "revision": 0,
                               "before": None, "after": base["result"]["copy"], "reason": "模型原始草稿",
                               "actor": "model", "saved_at": base["started_at"]}],
                "current_final_id": None, "confirmation_history": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if (document["base_generation_id"] != base["generation_id"]
                or document["input_fingerprint"] != base["input_fingerprint"]
                or not document["revisions"]):
            raise ValueError()
        for index, revision in enumerate(document["revisions"]):
            if (revision["revision"] != index or not isinstance(revision["after"], str)
                    or not revision["after"].strip() or len(revision["after"]) > 600):
                raise ValueError()
        return document
    except (OSError, ValueError, KeyError, TypeError):
        raise ProbeError("人工修改记录无法读取，请保留文件并排查。") from None


@contextmanager
def workspace_lock(base, directory):
    path = workspace_path(base, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    try:
        with lock.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        raise ProbeError("另一窗口正在保存，或上次保存被中断，请保留记录并排查。") from None
    try:
        yield path
    finally:
        lock.unlink(missing_ok=True)


def current_view(base, document):
    """保留原始生成；编辑后的文案使用新的版本编号，旧映射不能当作当前映射。"""
    view = copy.deepcopy(base)
    current = document["revisions"][-1]
    view["base_generation_id"] = base["generation_id"]
    view["generation_id"] = current["version_id"]
    view["result"]["copy"] = current["after"]
    view["human_changes"] = copy.deepcopy(document["revisions"][1:])
    if current["revision"]:
        view["result"]["claims"] = []
        view["check_status"] = {"mapping_structure": "needs_recheck", "model_review": "needs_recheck",
                                "source_truth": "not_verified", "human_copy_confirmation": "not_done"}
    return view


def save_edit(base, text, reason, expected_version, directory=None):
    directory = data_dir() if directory is None else Path(directory)
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise ProbeError("人工文案必须为1至600字。")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 400:
        raise ProbeError("请填写1至400字修改理由。")
    with workspace_lock(base, directory) as path:
        document = load_workspace(base, directory)
        previous = document["revisions"][-1]
        if previous["version_id"] != expected_version:
            raise ProbeError("其他窗口已保存新版本。请刷新并核对，避免覆盖修改。")
        # 即使改动后恢复原文，也创建新版本，要求重新复核。
        document["revisions"].append({"revision": previous["revision"] + 1,
            "version_id": uuid4().hex, "before": previous["after"], "after": text,
            "reason": reason.strip(), "actor": "user", "saved_at": now(),
            "previous_version_id": previous["version_id"], "check_status": "needs_recheck"})
        document["current_final_id"] = None
        write_atomic(path, document)
    return document


def validate_confirmation(base, document, reviewed, note, confirmed):
    view = current_view(base, document)
    if confirmed is not True:
        raise ProbeError("请先勾选对当前版本的人工确认。")
    if not isinstance(note, str) or not note.strip() or len(note) > 600:
        raise ProbeError("请填写1至600字人工判断与处理说明。")
    if not reviewed or reviewed.get("status") != "ok":
        raise ProbeError("当前版本尚未完成复核，不能确认导出。")
    if (reviewed.get("review_id") != review_id(view)
            or reviewed.get("draft") != view["result"]["copy"]
            or reviewed.get("input_fingerprint") != base["input_fingerprint"]):
        raise ProbeError("复核属于旧版本，请对当前文案重新复核。")
    validate_review({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(reviewed["result"], ensure_ascii=False)}}]}, view)
    if any(c["status"] in {"unsupported", "contradiction"} for c in reviewed["result"]["checks"]):
        raise ProbeError("仍有无依据或矛盾描述，请修改文案并重新复核后再确认。")
    return view


def confirm_final(base, reviewed, note, confirmed, expected_version, directory=None):
    directory = data_dir() if directory is None else Path(directory)
    with workspace_lock(base, directory) as path:
        document = load_workspace(base, directory)
        if document["revisions"][-1]["version_id"] != expected_version:
            raise ProbeError("当前文案版本已变化，请重新检查。")
        view = validate_confirmation(base, document, reviewed, note, confirmed)
        old = read_final(base, document, directory)
        if old and old["human_note"] == note.strip() and old["review"]["review_id"] == reviewed["review_id"]:
            return old, document
        final = {"export_id": uuid4().hex, "confirmed_at": now(), "actor": "user",
                 "version_id": expected_version, "input_fingerprint": base["input_fingerprint"],
                 "input": copy.deepcopy(base["input"]), "copy": view["result"]["copy"],
                 "human_note": note.strip(), "human_changes": copy.deepcopy(document["revisions"][1:]),
                 "original_generation": copy.deepcopy(base), "review": copy.deepcopy(reviewed),
                 "check_status": {"human_confirmed": True, "model_review": assessment(reviewed["result"]),
                                  "source_truth": "not_verified", "coverage": "not_guaranteed"},
                 "notice": "人工确认仅针对本版本，不是事实真实性保证；模拟资料不代表真实发布或反馈。"}
        folder = Path(directory) / "finals"
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(folder / (final["export_id"] + ".json"), final)
        document["current_final_id"] = final["export_id"]
        document["confirmation_history"].append({"export_id": final["export_id"],
            "version_id": expected_version, "confirmed_at": final["confirmed_at"]})
        write_atomic(path, document)
        return final, document


def read_final(base, document, directory=None):
    directory = data_dir() if directory is None else Path(directory)
    export_id = document.get("current_final_id")
    if not export_id:
        return None
    try:
        if not isinstance(export_id, str) or len(export_id) != 32 or any(c not in "0123456789abcdef" for c in export_id):
            raise ValueError()
        final = json.loads((Path(directory) / "finals" / (export_id + ".json")).read_text(encoding="utf-8"))
        current = document["revisions"][-1]
        if (final["version_id"] != current["version_id"] or final["copy"] != current["after"]
                or final["input_fingerprint"] != base["input_fingerprint"]):
            raise ValueError()
        validate_confirmation(base, document, final["review"], final["human_note"],
                              final["check_status"]["human_confirmed"])
        return final
    except (OSError, KeyError, ValueError, TypeError):
        raise ProbeError("已确认快照无法读取或已过期，请保留文件并排查。") from None


def final_text(final):
    from review import review_text
    lines = ["宣传文案（当前版本已人工确认）", "资料性质：" + final["input"]["data_kind"],
             "导出编号：" + final["export_id"], "确认时间：" + final["confirmed_at"],
             "版本编号：" + final["version_id"], "", final["copy"], "", "人工判断与处理说明：",
             final["human_note"], "", "检查状态：人工已确认本版本；资料真实性未核实；模型可能漏检或误判。",
             final["notice"], "", "人工修改记录："]
    for change in final["human_changes"]:
        lines.extend([f"第{change['revision']}次修改：{change['saved_at']}",
                      "修改前：" + change["before"], "修改后：" + change["after"], "理由：" + change["reason"]])
    if not final["human_changes"]:
        lines.append("无文字修改；人工核对后保留原稿。")
    lines.extend(["", "附：当前版本的模型复核报告（此附件保留复核发生时的未确认状态）：",
                  review_text(final["review"]).decode("utf-8-sig")])
    return ("\n".join(lines) + "\n").encode("utf-8-sig")
