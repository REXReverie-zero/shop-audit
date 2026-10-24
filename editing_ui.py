"""人工编辑、复核和最终确认。输入变化时立即撤销当前页面的确认入口。"""
import streamlit as st
from core import json_bytes
from editing import (load_workspace, current_view, save_edit, confirm_final, read_final, final_text)
from review_ui import show_review


def show_editor(base):
    document = load_workspace(base)
    revision = document["revisions"][-1]
    prefix = "edit_" + base["generation_id"]
    text_key, dirty_key, seen_key = prefix + "_text", prefix + "_dirty", prefix + "_seen"
    reason_key, confirm_key, note_key = prefix + "_reason", prefix + "_confirmed", prefix + "_note"
    if text_key not in st.session_state:
        st.session_state[text_key] = revision["after"]
        st.session_state[seen_key] = revision["version_id"]
        st.session_state[dirty_key] = False
    elif st.session_state[seen_key] != revision["version_id"] and not st.session_state.get(dirty_key):
        st.session_state[text_key] = revision["after"]
        st.session_state[seen_key] = revision["version_id"]
        st.session_state[confirm_key] = False

    def changed():
        st.session_state[dirty_key] = True
        st.session_state[confirm_key] = False
        st.session_state["review_paid_ok"] = False

    def note_changed():
        st.session_state[confirm_key] = False

    st.subheader("人工编辑当前文案")
    st.caption("保存编辑不调用API。改动后需要重新复核；上方生成原稿及其依据仅作为历史记录。")
    st.text_area("当前文案（可编辑，最多600字）", key=text_key, max_chars=600, height=130, on_change=changed)
    st.text_input("修改理由（保存修改时必填）", key=reason_key, max_chars=400)
    dirty = st.session_state.get(dirty_key, False)
    if st.button("保存人工修改（不调用 API）", disabled=not dirty):
        updated = save_edit(base, st.session_state[text_key], st.session_state.get(reason_key, ""),
                            st.session_state[seen_key])
        st.session_state[seen_key] = updated["revisions"][-1]["version_id"]
        st.session_state[dirty_key] = False
        st.session_state[confirm_key] = False
        st.session_state["review_paid_ok"] = False
        st.rerun()
    if dirty:
        st.warning("文案已改动，旧复核和旧确认不适用于当前编辑。请先填写理由并保存，再重新复核。")
        return
    current = current_view(base, document)
    st.caption(f"当前已保存修订次数：{revision['revision']}；版本：{revision['version_id'][:12]}")
    with st.expander("查看人工修改历史"):
        for change in document["revisions"][1:]:
            st.text(f"第{change['revision']}次：{change['reason']}")
            st.text("修改前：" + change["before"])
            st.text("修改后：" + change["after"])
        if not document["revisions"][1:]:
            st.text("尚无文字修改，可核对后保留原稿。")
    reviewed = show_review(current)
    if not reviewed:
        return
    st.subheader("人工确认与最终导出")
    st.caption("逐项阅读当前复核及原始资料。若仍有无依据或矛盾描述，请先修改重查；待确认提醒需要在下方说明处理方式。")
    st.text_area("人工判断与处理说明（必填）", key=note_key, max_chars=600, on_change=note_changed)
    confirmed = st.checkbox("我已人工检查当前文案、资料和复核提示，并确认本版本的处理说明", key=confirm_key)
    if st.button("确认并保存最终版本（不调用 API）", disabled=not confirmed):
        final, document = confirm_final(base, reviewed, st.session_state.get(note_key, ""), confirmed,
                                       revision["version_id"])
        st.success("当前版本已人工确认并保存。")
    final = read_final(base, document)
    if final:
        st.info("下面下载的是已保存的人工确认快照。更改文案后，当前下载入口会收起；旧文件保留为历史记录。")
        short_id = final["export_id"][:12]
        st.download_button("下载人工确认版 TXT", final_text(final), f"人工确认版_{short_id}.txt", "text/plain")
        st.download_button("下载完整过程 JSON", json_bytes(final), f"完整过程_{short_id}.json", "application/json")
