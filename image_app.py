"""可独立运行，也可由原项目的Streamlit多页面入口打开。"""
import os
import streamlit as st
from image_generation import NOTICE, PLATFORMS, build_payload, generate_image, serialize


def main():
    st.set_page_config(page_title='商品图片创意草稿', page_icon='🎨', layout='centered')
    st.title('商品图片创意草稿')
    st.warning(NOTICE)
    st.caption('未提供实物参考照片，图中外观仅为概念示意，不能证明与实际商品一致。')
    st.info('Seedream图片和文本共用侧栏人民币预算；单图请求开启AI水印。首次使用请核对并保存图片单价。')
    from sidebar import show_sidebar
    import prompt_store
    show_sidebar()
    prompt_store.panel(kind='image')
    endpoint = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
    model = st.session_state['image_model'].strip()
    api_key = st.session_state['image_api_key'].strip()
    # 点击生成才请求API；表单编辑、下载和页面重跑均不自动生成。
    with st.form('image_form'):
        platform = st.selectbox('发布平台', list(PLATFORMS), key='image_platform')
        product = st.text_input('产品名称', value='海风帆布包', max_chars=100, key='image_product')
        submitted = st.form_submit_button('生成一张图片（可能计费）')
    if submitted:
        st.session_state.pop('image_outcome', None)
        try:
            build_payload(platform, product, model)
            if not api_key.strip():
                raise ValueError('请先在左侧填写火山方舟 API Key。')
            with st.spinner('正在生成，请勿重复提交……'):
                st.session_state['image_outcome'] = generate_image(platform, product, endpoint, model, api_key)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except OSError:
            st.error('无法读取提示词或创建初始日志，本次未发送API请求。')
    outcome = st.session_state.get('image_outcome')
    if outcome:
        record = outcome['record']
        st.subheader('最近一次提交结果')
        st.caption('提示词版本：' + record['prompt_version'])
        if record.get('estimated_cost_cny') is not None:
            st.caption('本次费用估算：¥' + record['estimated_cost_cny'])
        st.caption(f"平台：{record['platform']} ｜ 产品：{record['product']} ｜ 状态：{record['status']}")
        if outcome['error']:
            st.error(outcome['error'])
        if outcome['image_bytes']:
            st.image(outcome['image_bytes'], caption='AI生成 · 广告创意草稿')
            width, height = record['image_dimensions']
            expected_width, expected_height = map(int, record['request_payload']['size'].split('x'))
            if width * expected_height != height * expected_width:
                st.warning('返回图片比例与要求不一致，请人工核对；程序未自动裁剪。')
            st.download_button('下载图片草稿', outcome['image_bytes'],
                               file_name=f"draft_{record['request_id']}.{outcome['extension']}", mime=outcome['mime'])
        if outcome['save_error']:
            st.warning(outcome['save_error'])
        else:
            st.caption('本次日志：' + outcome['log_path'])
        with st.expander('查看本次提示词'):
            st.code(record['prompt'], language='text')
        st.download_button('下载本次JSON记录', serialize(record).encode('utf-8'),
                           file_name=record['request_id'] + '.json', mime='application/json')


if __name__ == '__main__':
    main()
