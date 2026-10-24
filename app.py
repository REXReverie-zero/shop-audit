"""Streamlit页面：先读core.py，再看这里如何把函数接到按钮上。"""
import streamlit as st
from api_probe import ProbeError
from generation import generate_once, read_generation, draft_text
from editing_ui import show_editor
from sidebar import show_sidebar
import prompt_store
from case_catalog import CASES, saved_inputs
from core import (APP_TITLE, CHANNELS, DATA_KINDS, LIMITS,
                  fingerprint, json_bytes, prepare_record, sample_task,
                  save_record, text_bytes)

st.set_page_config(page_title=APP_TITLE, page_icon="📝", layout="centered")
show_sidebar()


def mark_modified():
    st.session_state["confirmed"] = False
    st.session_state["paid_ok"] = False
    st.session_state["review_paid_ok"] = False
    # 修改过即撤销本次确认；即使又改回原文，也必须重新确认。
    st.session_state["record"] = None


def fill_sample():
    apply_task(sample_task())


def apply_task(task):
    for field in ["product", "audience", "channel", "data_kind"]:
        st.session_state[field] = task[field]
    # 清除旧用例的多余行，保留恢复资料原有的编号和空白。
    for key in list(st.session_state):
        if key.startswith(("fact_", "source_")):
            del st.session_state[key]
    source_ids = [row["id"] for row in task["sources"]] or ["S01"]
    st.session_state["source_ids"] = source_ids
    for i, sid in enumerate(source_ids, 1):
        row = next((x for x in task["sources"] if x["id"] == sid), {})
        st.session_state[f"fact_{i}"] = row.get("fact", "")
        st.session_state[f"source_{i}"] = row.get("source", "")
    mark_modified()


def add_source():
    ids = st.session_state["source_ids"]
    next_id = max(int(sid[1:]) for sid in ids) + 1
    ids.append(f"S{next_id:02}")
    mark_modified()


def remove_last_source():
    ids = st.session_state["source_ids"]
    if len(ids) > 1:
        index = len(ids)
        ids.pop()
        st.session_state.pop(f"fact_{index}", None)
        st.session_state.pop(f"source_{index}", None)
        mark_modified()


def part_name(index):
    return {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}.get(index, str(index))


def fill_case():
    selected = next(c for c in CASES if c["id"] == st.session_state["case_id"])
    apply_task(selected["input"])


def restore_saved():
    matches = [r for r in saved_inputs() if r["record_id"] == st.session_state.get("saved_input_id")]
    if matches:
        apply_task(matches[0]["task"])


st.title(APP_TITLE)
prompt_store.panel()
st.caption("整理资料 → 确认 → 生成草稿 → 独立复核")
st.info("先保存资料单，再分别点击生成和复核。每个按钮各调用一次 DeepSeek；相同版本会复用结果。")
st.button("填入模拟练习资料", on_click=fill_sample)
with st.expander("恢复已保存资料 / 载入模拟用例"):
    previous_inputs = saved_inputs()
    if previous_inputs:
        labels = {r["record_id"]: r["label"] for r in previous_inputs}
        st.selectbox("已保存的资料单", list(labels), format_func=lambda value: labels[value], key="saved_input_id")
        st.button("恢复选中资料（不调用 API）", on_click=restore_saved)
        st.caption("恢复后重新勾选并保存即可找回同一输入的结果。会保留原始空格和标点，减少误生成新任务。")
    names = {c["id"]: c["id"] + " · " + c["name"] for c in CASES}
    case_id = st.selectbox("模拟测试用例", list(names), format_func=lambda value: names[value], key="case_id")
    chosen_case = next(c for c in CASES if c["id"] == case_id)
    st.text("预期检查点（不是实际测试结果）：" + chosen_case["checkpoints"])
    st.button("载入选中模拟用例（不调用 API）", on_click=fill_case)
    st.caption("所有用例均为虚构。载入不调用API；格式通过不代表模型质量通过，不需要把10个用例全部付费运行。")
product = st.text_input("商品名称", key="product", max_chars=LIMITS["product"], on_change=mark_modified)
audience = st.text_input("目标受众", key="audience", max_chars=LIMITS["audience"], on_change=mark_modified)
channel = st.selectbox("传播渠道", CHANNELS, key="channel", on_change=mark_modified)
data_kind = st.selectbox("资料性质", DATA_KINDS, key="data_kind", on_change=mark_modified)
st.subheader("商品事实与资料来源")
st.caption("至少填写一部分，可按需增加。每部分写一个事实及对应来源；来源请注明文件名、网页地址或提供者及日期。这里不会自动访问来源。")
if "source_ids" not in st.session_state:
    st.session_state["source_ids"] = ["S01"]
left, right = st.columns(2)
left.button("增加一部分", on_click=add_source)
right.button("移除最后一部分", on_click=remove_last_source,
             disabled=len(st.session_state["source_ids"]) <= 1)
rows = []
for i, sid in enumerate(st.session_state["source_ids"], 1):
    with st.expander(f"第{part_name(i)}部分 · 事实与来源（{sid}）", expanded=True):
        fact = st.text_area(f"{sid} 商品事实", key=f"fact_{i}",
                            max_chars=LIMITS["fact"], on_change=mark_modified)
        source = st.text_input(f"{sid} 资料来源", key=f"source_{i}",
                               max_chars=LIMITS["source"], on_change=mark_modified)
        rows.append({"id": sid, "fact": fact, "source": source})
task = {"product": product, "audience": audience, "channel": channel,
        "data_kind": data_kind, "sources": rows}
confirmed = st.checkbox("我已逐条核对录入内容与资料编号（不代表资料已获独立核实）", key="confirmed")

if st.button("检查并保存资料单", type="primary"):
    # 付费调用将来同样只能放在明确的按钮操作里。
    st.session_state["record"] = None
    try:
        record = prepare_record(task, confirmed, st.session_state.get("last_record"))
        record, path = save_record(record)
        st.session_state["record"] = record
        st.session_state["last_record"] = record
        st.session_state["saved_path"] = str(path)
    except ValueError as exc:
        st.error(str(exc))
    except OSError as exc:
        st.error(f"保存失败，请检查data目录和已有文件：{exc}")

record = st.session_state.get("record")
if record and confirmed and fingerprint(task) == record["input_fingerprint"]:
    st.success("资料单已保存。格式检查通过；资料真实性未核实；模型复核未执行。")
    st.text(text_bytes(record).decode("utf-8-sig"))
    short_id = record["record_id"][:12]
    st.download_button("下载JSON资料单", json_bytes(record), f"资料单_{short_id}.json", "application/json")
    st.download_button("下载TXT资料单", text_bytes(record), f"资料单_{short_id}.txt", "text/plain")
    with st.expander("查看保存位置"):
        st.code(st.session_state["saved_path"], language=None)
    st.subheader("生成文案草稿")
    st.caption("会把当前确认的商品、受众、渠道及事实来源发送给 DeepSeek 官方接口。来源链接不会被自动访问。")
    try:
        generated = read_generation(task)
        paid_ok = st.checkbox("我同意将本次资料用于生成，并知悉会产生 API 费用", key="paid_ok")
        if st.button("生成一次文案（调用 API）", disabled=not paid_ok or generated is not None):
            with st.spinner("正在生成，请等待，勿重复点击……"):
                generated, reused = generate_once(record)
        if generated:
            if generated["status"] == "ok":
                st.success("草稿已保存；再次打开相同资料会复用本次结果，不重复调用。")
                st.warning("以下是生成时的草稿快照，仅结构检查通过。独立复核状态见下方报告，人工确认尚未完成。")
                st.text(generated["result"]["copy"])
                st.write("关键事实与依据（请逐条判断摘录是否真正支持描述）：")
                for claim in generated["result"]["claims"]:
                    st.text(claim["text"] + " → " + "、".join(claim["source_ids"]))
                    for evidence in claim["evidence"]:
                        st.text("原文 [" + evidence["source_id"] + "]：" + evidence["quote"])
                for warning in generated["result"]["warnings"]:
                    st.warning(warning)
                call = generated.get("call", {})
                st.caption(f"耗时：{call.get('elapsed_seconds')}秒；tokens：{call.get('usage')}；费用以官方账单为准。")
                st.download_button("下载草稿 TXT（未复核）", draft_text(generated),
                                   f"草稿_{short_id}.txt", "text/plain")
                st.download_button("下载草稿及过程 JSON", json_bytes(generated),
                                   f"草稿过程_{short_id}.json", "application/json")
                show_editor(generated)
            elif generated["status"] == "started":
                st.warning("任务正在进行或上次被中止。为避免重复扣费已停止再次调用；请保留记录并排查。")
            else:
                st.error(generated.get("error", "生成失败。"))
                st.caption("未自动重试。请把错误提示告诉开发助手，不要删除记录反复尝试。")
    except ProbeError as exc:
        st.error(str(exc))
    except OSError:
        st.error("无法读写生成记录；请求可能已发送，请保留记录并排查，勿重复点击。")
else:
    st.caption("填写或修改资料后，请重新核对并保存。只有当前已确认的版本可以下载。")
st.warning("当前规则只检查必填、长度和编号，不判断真假、矛盾或营销用语。模型复核也不会构成事实保证。")
