"""复核页面单独放置，保持主页面便于初学者阅读。"""
import streamlit as st
from core import json_bytes
from review import STATUS_LABELS, assessment, read_review, review_once, review_text


def show_review(generated):
    st.subheader("独立复核草稿")
    st.caption("第二次模型调用会对照原始资料检查草稿全文；刷新、下载和查看结果不发起请求。")
    reviewed = read_review(generated)
    consent = st.checkbox("我知悉复核会再调用一次 API 并产生费用", key="review_paid_ok")
    if st.button("复核这份草稿（调用 API）", disabled=not consent or reviewed is not None):
        with st.spinner("正在对照原始资料复核……"):
            reviewed, reused = review_once(generated)
    if not reviewed:
        st.info("尚未执行独立复核。")
        return
    if reviewed["status"] == "started":
        st.warning("复核正在进行或上次被中止；已阻止重复扣费，请保留记录并排查。")
        return
    if reviewed["status"] != "ok":
        st.error(reviewed.get("error", "复核未完成。"))
        st.caption("未自动重试，请保留错误记录。")
        return
    if assessment(reviewed["result"]) == "flags_reported":
        st.warning("模型标出了待处理项，请逐条核对。")
    else:
        st.info("模型未报告问题。这不代表全部事实已核验通过。")
    for check in reviewed["result"]["checks"]:
        st.text("【" + STATUS_LABELS[check["status"]] + "】" + check["text"])
        st.text("依据：" + ("、".join(check["source_ids"]) or "无") + "；理由：" + check["reason"])
    for warning in reviewed["result"]["input_warnings"]:
        st.warning(warning)
    st.text(reviewed["result"]["summary"])
    st.caption("本报告仅适用于这一版资料和文案。修改文案后必须重新复核。")
    call = reviewed.get("call", {})
    st.caption(f"复核耗时：{call.get('elapsed_seconds')}秒；tokens：{call.get('usage')}；费用以官方账单为准。")
    short_id = reviewed["review_id"][:12]
    st.download_button("下载复核报告 TXT（人工未确认）", review_text(reviewed),
                       f"复核报告_{short_id}.txt", "text/plain")
    st.download_button("下载复核过程 JSON", json_bytes(reviewed),
                       f"复核过程_{short_id}.json", "application/json")
    return reviewed
