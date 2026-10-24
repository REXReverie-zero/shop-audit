from session_storage import data_dir
"""只读浏览旧版人工确认快照，不重新生成、不改写历史文件。"""
import json
from pathlib import Path
import streamlit as st
from sidebar import show_sidebar

ROOT = Path(__file__).resolve().parent


def load_finals(directory=None):
    folder = Path(directory) if directory is not None else data_dir() / 'finals'
    records, invalid = [], []
    for path in sorted(folder.glob('*.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict) or not isinstance(record.get('copy'), str):
                raise ValueError()
            records.append((path, record))
        except (ValueError, OSError):
            invalid.append(path.name)
    records.sort(key=lambda pair: pair[1].get('confirmed_at', ''), reverse=True)
    return records, invalid


def main():
    st.set_page_config(page_title='历史确认成果', page_icon='📁', layout='centered')
    show_sidebar()
    st.title('历史确认成果')
    st.info('只读展示已保存的人工确认快照。本页不重新生成、不复核、不调用API，也不把旧版本视为当前提示词的运行结果。')
    records, invalid = load_finals()
    if invalid:
        st.warning('有历史文件无法解析，原文件已保留：' + '、'.join(invalid))
    if not records:
        st.caption('当前工程data/finals目录还没有人工确认快照。')
        return
    labels = {path.name: record.get('input', {}).get('product', '未命名商品') +
              ' · ' + path.stem[:12] for path, record in records}
    choice = st.selectbox('已保存成果', list(labels), format_func=labels.get)
    path, record = next(pair for pair in records if pair[0].name == choice)
    st.caption('历史确认时间：' + str(record.get('confirmed_at', '未记录')))
    st.text(record['copy'])
    st.write('当时的人工处理说明：')
    st.text(record.get('human_note', '未记录'))
    st.warning('历史人工确认不保证资料真实；模拟资料仍为模拟，不代表真实商家效果。')
    with st.expander('查看当时资料、复核和修改记录'):
        st.json(record)
    # JSON下载保持原始字节；TXT是标明重建性质的阅读副本。
    st.download_button('下载原始历史JSON', path.read_bytes(), file_name=path.name,
                       mime='application/json')
    text = ('历史人工确认快照（从JSON重建的阅读副本）\n'
            '不代表当前提示词运行结果；真实性未核实。\n\n' + record['copy'] +
            '\n\n当时人工处理说明：\n' + record.get('human_note', '未记录'))
    st.download_button('下载历史阅读TXT', text.encode('utf-8-sig'),
                       file_name='历史成果_' + path.stem[:12] + '.txt', mime='text/plain')


if __name__ == '__main__':
    main()
