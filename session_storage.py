"""Resolve private storage per call, including across page changes."""
from pathlib import Path
from tempfile import TemporaryDirectory


def data_dir():
    import streamlit as st
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is None:
        raise RuntimeError('会话存储只能在 Streamlit 会话中使用。')
    key = '_qd_private_workspace'
    if key not in st.session_state:
        st.session_state[key] = TemporaryDirectory(prefix='qd_session_')
    folder = Path(st.session_state[key].name) / 'data'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder
