from session_storage import data_dir
"""文本与图片统一人民币预算：每3秒仅刷新本地账本，不请求模型。"""
import json
import streamlit as st
import budget


@st.fragment(run_every='3s')
def live_metrics():
    try:
        info = budget.status(data_dir())
        st.metric('累计图片生成张数', info['image_generated'])
        st.caption(f"图片请求 {info['image_calls']} 次 / 请求 {info['image_requested']} 张 · 文本调用 {info['text_calls']} 次")
        st.metric('累计API费用（估算，元）', '¥' + f"{float(info['estimated_cny']):.6f}")
        st.caption('按返回用量和调用时单价估算，不是官方账单；费用未知的请求另占用预留。')
        limit, occupied = float(info['limit_cny']), float(info['occupied_upper_cny'])
        ratio = occupied / limit if limit > 0 else 1.0
        st.progress(min(max(ratio, 0), 1.0), text=f"预算占用 ¥{occupied:.6f} / ¥{limit:.2f}")
        st.caption(f"可用 ¥{float(info['available_cny']):.6f} · 待确认请求 {info['pending_count']} 个")
        if ratio >= 1:
            st.error('预算已达上限，暂停新调用。')
        elif ratio * 100 >= info['warning_percent']:
            st.warning(f"预算占用已达到 {info['warning_percent']}% 预警阈值。")
        if info['blocked_reason']: st.warning(info['blocked_reason'])
        st.caption('累计范围为本项目data账本；其他程序或其他目录的消费不在此限额内。')
    except (budget.BudgetError, OSError) as exc:
        st.error(str(exc))


def show_budget():
    with st.sidebar:
        st.subheader('共同预算与累计用量')
        live_metrics()
        try:
            info = budget.status(data_dir())
            with st.expander('预算上限与预警阈值'):
                if st.session_state.get('budget_limit_loaded') != info['limit_cny']:
                    st.session_state['budget_limit_cny'] = float(info['limit_cny'])
                    st.session_state['budget_limit_loaded'] = info['limit_cny']
                with st.form('budget_limit'):
                    limit = st.number_input('预算上限（人民币元，0为暂停）', min_value=0.0, max_value=1000000000000.0,
                                            step=1.0, format='%.2f', key='budget_limit_cny')
                    warning = st.number_input('预警阈值（预算百分比）', min_value=1, max_value=100,
                                              value=info['warning_percent'], step=1, key='budget_warning_percent')
                    if st.form_submit_button('保存预算设置'):
                        budget.update_limit(data_dir(), limit)
                        budget.update_warning(data_dir(), warning)
                        st.rerun()
            with st.expander('API单价（人民币）'):
                st.caption('输入所选模型实际适用的单价。每次请求保存单价快照，修改单价不回算历史。')
                st.markdown(f'[DeepSeek价格]({budget.PRICING_SOURCE}) · [Seedream价格与模型](https://www.volcengine.com/docs/ark/models)')
                with st.form('budget_prices'):
                    incoming = st.number_input('文本输入：元/百万tokens（缓存未命中上界）', min_value=0.000001,
                                               value=float(info['pricing']['input_cny_per_million']), format='%.6f', key='budget_input_cny')
                    outgoing = st.number_input('文本输出：元/百万tokens', min_value=0.000001,
                                               value=float(info['pricing']['output_cny_per_million']), format='%.6f', key='budget_output_cny')
                    verified = st.checkbox('我已核对当前文本模型人民币单价', key='verify_text_price')
                    if st.form_submit_button('保存文本单价'):
                        budget.update_pricing(data_dir(), incoming, outgoing, verified)
                        st.rerun()
                price = info.get('image_pricing') or {}
                with st.form('image_prices'):
                    unit = st.number_input('Seedream：元/张', min_value=0.0, value=float(price.get('cny_per_image', 0)),
                                           format='%.6f', step=0.01, key='image_unit_price')
                    st.caption('若两种尺寸价格不同，请填写较高单价；这里按每张计费模型估算。')
                    verified = st.checkbox('已核对侧栏当前Seedream模型和两种尺寸单价', key='verify_image_price')
                    if st.form_submit_button('保存图片单价'):
                        budget.update_image_pricing(data_dir(), unit, st.session_state.get('image_model', ''), verified)
                        st.rerun()
                if price: st.caption('已登记图片模型：' + price['model'])
            with st.expander('调用明细与人工对账'):
                st.caption('如曾使用旧版生图页面，可导入原目录的image_logs、seedream_image_logs；按调用ID去重。')
                if st.button('导入旧图片日志（不调用API）', key='import_old_images'):
                    total = budget.import_image_logs(data_dir(), [data_dir() / 'image_logs'])
                    st.session_state['import_notice'] = f'导入{total}条图片调用；缺少当时单价的记录需人工对账。'
                    st.rerun()
                if st.session_state.get('import_notice'):
                    st.info(st.session_state.pop('import_notice'))
                st.dataframe(budget.history_rows(data_dir()), hide_index=True)
                st.download_button('下载共同预算账本', json.dumps(budget.load(data_dir()), ensure_ascii=False, indent=2).encode(),
                                   file_name='budget.json', mime='application/json')
                st.caption('仅在所有请求停止并核对账单后填写项目累计人民币费用上界，不能把余额或充值额当作费用。对账不清除图片张数。')
                with st.form('budget_reconcile'):
                    total = st.text_input('项目累计费用上界（人民币元）')
                    note = st.text_area('对账依据', max_chars=600)
                    confirmed = st.checkbox('所有请求已停止，我已核对项目累计费用')
                    if st.form_submit_button('保存人工对账'):
                        budget.reconcile(data_dir(), total, note, confirmed)
                        st.rerun()
        except (budget.BudgetError, OSError) as exc:
            st.error(str(exc))
