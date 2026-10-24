"""Cloud sidebar: credentials belong only to the active visitor session."""
import streamlit as st
from budget_ui import show_budget
import local_api_settings


def initial_text_key():
    return ''


def show_sidebar():
    for name, default in local_api_settings.effective().items():
        if name not in st.session_state:
            st.session_state[name] = default
        st.session_state['widget_' + name] = st.session_state[name]

    def sync(name):
        st.session_state[name] = st.session_state['widget_' + name]

    with st.sidebar:
        st.title('API 与预算')
        with st.expander('文本大模型 · DeepSeek', expanded=True):
            st.text_input('文本 API 密钥', type='password', key='widget_text_api_key',
                          on_change=sync, args=('text_api_key',))
        with st.expander('图像 · 火山方舟 Seedream', expanded=True):
            st.text_input('方舟 API Key', type='password', key='widget_image_api_key',
                          on_change=sync, args=('image_api_key',))
            st.text_input('Seedream 模型 ID / ep-接入点 ID', key='widget_image_model',
                          on_change=sync, args=('image_model',))
        st.caption('请使用自己的 API 密钥。密钥仅保留在当前会话内，云端不提供保存密钥功能。')
        st.info('预算、提示词和历史资料仅属于当前会话。刷新、断线或服务重启后可能重置，请及时下载成果及账本。预算是本会话估算，账户实际扣费以服务商账单为准。')
    show_budget()
