"""Cloud edition: each visitor supplies keys; never read or save host keys."""
DEFAULT_MODEL = 'doubao-seedream-5-0-flash-260915'
FIELDS = {'text_api_key', 'image_api_key', 'image_model'}


def load(root):
    return None


def effective(root=None, environ=None):
    return {'text_api_key': '', 'image_api_key': '', 'image_model': DEFAULT_MODEL}


def save(root, settings):
    raise RuntimeError('云端版本禁止保存密钥到服务器文件。')
