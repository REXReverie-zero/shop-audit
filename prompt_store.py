"""四套可编辑模板：保存版本历史，以内容指纹追踪实际运行版本。"""
import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
from session_storage import data_dir
LABELS = {'copy_xiaohongshu': '小红书 · 文案', 'copy_moments': '朋友圈 · 文案',
          'image_xiaohongshu': '小红书 · 图像', 'image_moments': '朋友圈 · 图像'}


def defaults():
    base = (ROOT / 'prompts/generate_v1.txt').read_text(encoding='utf-8').strip()
    return {
        'copy_xiaohongshu': base + '\n平台要求：适合小红书，表达自然、段落简短，不虚构个人使用经历，不滥用话题标签。',
        'copy_moments': base + '\n平台要求：适合本地小店朋友圈，简短亲切，重点写清商品资料和真实服务限制。',
        'image_xiaohongshu': (ROOT / 'prompts/image_xiaohongshu_v1.txt').read_text(encoding='utf-8').strip(),
        'image_moments': (ROOT / 'prompts/image_moments_v1.txt').read_text(encoding='utf-8').strip(),
    }


def version(key, text):
    return key + '-' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def load(directory=None):
    folder = Path(directory) if directory is not None else data_dir()
    path = folder / 'prompt_templates.json'
    if not path.exists():
        return {'schema': 1, 'templates': {key: {'text': value, 'version': version(key, value),
                'saved_at': None} for key, value in defaults().items()}, 'history': []}
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        if doc['schema'] != 1 or set(doc['templates']) != set(LABELS) or not isinstance(doc['history'], list):
            raise ValueError()
        for key, item in doc['templates'].items():
            validate(key, item['text'])
            if item['version'] != version(key, item['text']):
                raise ValueError()
        return doc
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('提示词配置损坏，请保留文件并排查。') from None


def validate(key, text):
    if key not in LABELS or not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000:
        raise ValueError('每套提示词需填写1—12000字。')
    if key.startswith('image_') and '{product}' not in text:
        raise ValueError('图像提示词必须保留 {product} 占位符。')


def get(kind, platform, directory=None):
    suffix = {'小红书': 'xiaohongshu', '朋友圈': 'moments'}.get(platform)
    if not suffix:  # 原公众号入口仍采用原稿，不伪造新迭代记录。
        text = (ROOT / 'prompts/generate_v1.txt').read_text(encoding='utf-8')
        return {'text': text, 'version': 'generate-v1'}
    return copy.deepcopy(load(directory)['templates'][kind + '_' + suffix])


def save(key, text, expected_version, directory=None):
    validate(key, text)
    folder = Path(directory) if directory is not None else data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / 'prompt_templates.lock'
    try:
        with lock.open('x'): pass
    except FileExistsError:
        raise ValueError('提示词正被其他页面保存，请稍后刷新。') from None
    try:
        doc = load(folder)
        old = doc['templates'][key]
        if old['version'] != expected_version:
            raise ValueError('提示词已被其他窗口修改，请刷新后再编辑。')
        if old['text'] == text.strip(): return old
        item = {'text': text.strip(), 'version': version(key, text.strip()),
                'saved_at': datetime.now(timezone.utc).isoformat()}
        doc['history'].append({'key': key, 'before': old, 'after': item, 'actor': 'user'})
        doc['templates'][key] = item
        temporary = folder / (uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(folder / 'prompt_templates.json')
        finally: temporary.unlink(missing_ok=True)
        return item
    finally: lock.unlink(missing_ok=True)


def panel(kind=None):
    import streamlit as st
    choices = [key for key in LABELS if kind is None or key.startswith(kind + '_')]
    if not choices:
        raise ValueError('未知的提示词类型。')
    image_only = kind == 'image'
    scope = kind or 'all'
    title = '在线编辑提示词 · 图像' if image_only else '在线编辑提示词 · 文案 / 图像'
    with st.expander(title, expanded=False):
        st.caption('修改后点击保存。新版本用于后续调用；历史结果、修改前后模板和人工确认记录保留。编辑和保存不调用API。')
        try:
            doc = load()
            choice_widget = 'prompt_choice_' + scope
            if st.session_state.get(choice_widget) not in choices:
                st.session_state[choice_widget] = choices[0]
            key = st.selectbox('提示词模板', choices, format_func=LABELS.get, key=choice_widget)
            item = doc['templates'][key]
            widget = 'prompt_editor_' + key
            stamp = 'prompt_loaded_' + key
            if st.session_state.get(stamp) != item['version'] or widget not in st.session_state:
                st.session_state[widget] = item['text']
                st.session_state[stamp] = item['version']
            st.caption('当前版本：' + item['version'])
            st.text_area('提示词内容', key=widget, height=300, max_chars=12000)
            if st.button('保存此提示词（不调用API）', key='save_prompt'):
                save(key, st.session_state[widget], item['version'])
                st.session_state['prompt_saved_notice'] = '提示词已保存，后续生成使用新版本。'
                st.rerun()
            if st.session_state.get('prompt_saved_notice'):
                st.success(st.session_state.pop('prompt_saved_notice'))
            st.download_button('导出全部提示词与版本记录', json.dumps(doc, ensure_ascii=False, indent=2).encode(),
                               file_name='prompt_templates.json', mime='application/json', key='download_prompts')
            st.caption('图像比例、单图、水印由程序固定。' if image_only else
                       '文案仍需遵守JSON结构与资料引用约束；图像比例、单图、水印由程序固定。')
        except (ValueError, OSError) as exc:
            st.error(str(exc))
