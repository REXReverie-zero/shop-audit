import ast
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from streamlit.testing.v1 import AppTest

SCRIPT = """
import json
import streamlit as st
from session_storage import data_dir
import budget
import core
import prompt_store
from history_app import load_finals
from case_catalog import saved_inputs
from api_probe import load_key
from sidebar import show_sidebar
st.set_page_config(page_title='isolation test')
show_sidebar()
folder = data_dir()
if st.button('write private records', key='write'):
    budget.update_limit(folder, '17')
    current = prompt_store.load()['templates']['image_xiaohongshu']
    prompt_store.save('image_xiaohongshu', 'Private cloud {product}', current['version'])
    record = core.prepare_record(core.sample_task(), confirmed=True)
    core.save_record(record)
    (folder / 'finals').mkdir(exist_ok=True)
    (folder / 'finals' / 'private.json').write_text(json.dumps({'copy': 'PRIVATE'}), encoding='utf-8')
    (folder / 'image_logs').mkdir(exist_ok=True)
    (folder / 'image_logs' / 'private.json').write_text('{}', encoding='utf-8')
st.session_state['observed'] = {
    'path': str(folder),
    'budget': budget.status(folder)['limit_cny'],
    'prompt': prompt_store.load()['templates']['image_xiaohongshu']['text'],
    'finals': len(load_finals()[0]),
    'inputs': len(saved_inputs()),
    'image_logs': len(list((folder / 'image_logs').glob('*.json'))),
}
"""

class CloudTests(unittest.TestCase):
    def run_app(self, app):
        app.run(timeout=25)
        self.assertFalse(list(app.exception), str([e.message for e in app.exception]))
        return app

    def test_two_sessions_and_reruns(self):
        a = self.run_app(AppTest.from_string(SCRIPT))
        b = self.run_app(AppTest.from_string(SCRIPT))
        self.assertNotEqual(a.session_state['observed']['path'], b.session_state['observed']['path'])
        a.button(key='write').click()
        self.run_app(a)
        self.run_app(b)
        av, bv = a.session_state['observed'], b.session_state['observed']
        self.assertEqual(float(av['budget']), 17.0)
        self.assertEqual(bv['budget'], '35.00')
        self.assertEqual(av['prompt'], 'Private cloud {product}')
        self.assertNotEqual(av['prompt'], bv['prompt'])
        self.assertEqual(av['finals'], 1)
        self.assertEqual(bv['finals'], 0)
        self.assertEqual(av['inputs'], 1)
        self.assertEqual(bv['inputs'], 0)
        self.assertEqual(av['image_logs'], 1)
        self.assertEqual(bv['image_logs'], 0)
        path = av['path']
        self.run_app(a)
        self.assertEqual(a.session_state['observed']['path'], path)

    def test_credentials_and_no_save_button(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'host-test-key', 'ARK_API_KEY':'host-image-key'}):
            a = self.run_app(AppTest.from_string(SCRIPT))
            b = self.run_app(AppTest.from_string(SCRIPT))
            self.assertEqual(a.session_state['text_api_key'], '')
            self.assertEqual(a.session_state['image_api_key'], '')
            a.text_input(key='widget_text_api_key').set_value('visitor-test-key')
            self.run_app(a)
            self.run_app(b)
            self.assertEqual(a.session_state['text_api_key'], 'visitor-test-key')
            self.assertEqual(b.session_state['text_api_key'], '')
            self.assertFalse(any('保存到本机' in x.label for x in a.button))
            import local_api_settings
            with self.assertRaises(RuntimeError):
                local_api_settings.save(ROOT, {})
            for file in Path(a.session_state['observed']['path']).rglob('*'):
                if file.is_file():
                    self.assertNotIn(b'visitor-test-key', file.read_bytes())

    def test_no_session_fails_closed(self):
        from session_storage import data_dir
        with self.assertRaises(RuntimeError):
            data_dir()

    def test_pages_render(self):
        for entry in [ROOT / 'app.py', *sorted((ROOT / 'pages').glob('*.py'))]:
            with self.subTest(entry=entry.name):
                self.run_app(AppTest.from_file(str(entry)))

    def test_source_and_manifest(self):
        for p in ROOT.rglob('*.py'):
            ast.parse(p.read_text(encoding='utf-8'))
        self.assertFalse((ROOT / 'data').exists())
        self.assertFalse((ROOT / '.streamlit/secrets.toml').exists())
        self.assertFalse((ROOT / '.streamlit/api_settings.json').exists())
        import tomllib
        config = tomllib.loads((ROOT / '.streamlit/config.toml').read_text())
        self.assertNotIn('address', config['server'])
        for dependency in ['streamlit', 'requests', 'pillow']:
            self.assertIn(dependency, (ROOT / 'requirements.txt').read_text())

if __name__ == '__main__':
    unittest.main(verbosity=2)

