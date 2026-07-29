setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': 'setting',
    'menu': {
        'uri': __package__,
        'name': 'I-Proxy2',
        'list': [
            {'uri': 'setting', 'name': '상태'},
            {'uri': 'log', 'name': '로그'},
        ],
    },
    'default_route': 'single',
}

from plugin import *  # pylint: disable=wildcard-import,unused-wildcard-import

P = create_plugin_instance(setting)

try:
    from .logic import Logic

    P.set_module_list([Logic])
except Exception as e:
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
