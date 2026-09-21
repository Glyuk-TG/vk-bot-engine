import os
import json
import time
import random
import logging
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

# ============== НАСТРОЙКИ ==============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

VK_TOKEN = os.environ.get('VK_TOKEN')
VK_GROUP_ID = os.environ.get('VK_GROUP_ID')

if not VK_TOKEN or not VK_GROUP_ID:
    log.error('Не заданы переменные окружения VK_TOKEN и VK_GROUP_ID')
    exit(1)

VK_GROUP_ID = int(VK_GROUP_ID)

# ============== ЗАГРУЗКА СЦЕНАРИЯ ==============

with open('scenario.json', 'r', encoding='utf-8') as f:
    scenario = json.load(f)

NODES = scenario['drawflow']['Home']['data']

# Индекс: id -> node
NODES_BY_ID = {n['name']: n for n in NODES.values()}

# Стартовый узел — первый по порядку (msg_1)
START_NODE = 'msg_1'

# ============== СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ ==============

# user_id -> {'node': 'msg_1', 'vars': {...}}
user_states = {}

# ============== VK API ==============

vk_session = VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)

# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============

def get_user_name(user_id):
    """Получает имя пользователя через VK API"""
    try:
        res = vk.users.get(user_ids=user_id, fields='first_name')
        if res and len(res) > 0:
            return res[0]['first_name']
    except Exception as e:
        log.warning(f'Не удалось получить имя для {user_id}: {e}')
    return 'друг'


def replace_vars(text, user_id, state):
    """Заменяет %first_name% и другие переменные"""
    if not text:
        return ''
    text = text.replace('%first_name%', get_user_name(user_id))
    # Заменяем пользовательские переменные
    for var_name, var_value in state.get('vars', {}).items():
        text = text.replace(f'%{var_name}%', str(var_value))
    return text


def build_keyboard(buttons):
    """Собирает клавиатуру VK с поддержкой open_link и callback"""
    keyboard = VkKeyboard(one_time=False, inline=False)

    # Разделяем кнопки: сначала все обычные, потом все ссылочные
    # Но VK требует, чтобы open_link были отдельными рядами
    callback_buttons = [b for b in buttons if not b.get('url')]
    link_buttons = [b for b in buttons if b.get('url')]

    first = True

    # Ссылочные кнопки — каждая на своём ряду
    for btn in link_buttons:
        if not first:
            keyboard.add_line()
        first = False
        keyboard.add_openlink_button(
            label=btn['label'][:40],
            link=btn['url']
        )

    # Обычные кнопки — можно по 2 в ряд
    # Но проще каждую на своём ряду, чтобы не путаться
    for btn in callback_buttons:
        if not first:
            keyboard.add_line()
        first = False
        payload = {'next': btn.get('next', '')}
        keyboard.add_button(
            label=btn['label'][:40],
            color=VkKeyboardColor.PRIMARY,
            payload=payload
        )

    return keyboard


def send_message(user_id, text, keyboard=None):
    """Отправляет сообщение"""
    params = {
        'user_id': user_id,
        'message': text,
        'random_id': random.randint(0, 2**31 - 1),
    }
    if keyboard:
        params['keyboard'] = keyboard.get_keyboard()
    try:
        vk.messages.send(**params)
    except Exception as e:
        log.error(f'Ошибка отправки сообщения {user_id}: {e}')


def get_next_node(node_id):
    """Возвращает ID следующего узла (первое соединение)"""
    node = NODES_BY_ID.get(node_id)
    if not node:
        return None
    outputs = node.get('outputs', {})
    for out_key in sorted(outputs.keys()):
        connections = outputs[out_key].get('connections', [])
        if connections:
            target_id = connections[0]['node']
            target_node = NODES_BY_ID.get(f'msg_{target_id}') or \
                          NODES_BY_ID.get(f'btn_{target_id}') or \
                          NODES_BY_ID.get(f'cond_{target_id}') or \
                          NODES_BY_ID.get(f'wait_{target_id}')
            if not target_node:
                # Ищем по числу в имени
                for nid, n in NODES_BY_ID.items():
                    if nid.endswith(f'_{target_id}'):
                        return nid
            else:
                return target_node['name']
    return None


def find_node_by_id(node_id):
    """Ищет узел по числовому ID"""
    for nid, n in NODES_BY_ID.items():
        if nid.endswith(f'_{node_id}') or nid == node_id:
            return nid
    return None


def process_node(user_id, node_id, state):
    """
    Выполняет узел. Возвращает следующий node_id или None.
    """
    node = NODES_BY_ID.get(node_id)
    if not node:
        log.warning(f'Узел не найден: {node_id}')
        return None

    ntype = node['data'].get('type')

    if ntype == 'message':
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        send_message(user_id, text)
        return get_next_node(node_id)

    elif ntype == 'buttons':
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        buttons = node['data'].get('buttons', [])
        keyboard = build_keyboard(buttons)
        send_message(user_id, text, keyboard=keyboard)
        return None  # Ждём нажатия кнопки

    elif ntype == 'wait':
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        send_message(user_id, text)
        state['waiting_var'] = node['data'].get('var_name', 'answer')
        return None  # Ждём ввода

    elif ntype == 'condition':
        # В нашем сценарии не используется, но пусть будет
        return get_next_node(node_id)

    return None


def run_chain(user_id, start_node_id, state):
    """Запускает цепочку узлов до первой остановки (buttons/wait)"""
    current = start_node_id
    steps = 0
    max_steps = 50  # защита от бесконечного цикла

    while current and steps < max_steps:
        steps += 1
        state['node'] = current
        next_node = process_node(user_id, current, state)
        if next_node is None:
            break
        current = next_node


# ============== ОБРАБОТКА СООБЩЕНИЙ ==============

def handle_message(event):
    user_id = event.message.from_id
    if user_id < 0:
        return  # игнорируем сообщения от групп

    text = (event.message.text or '').strip()
    payload = None

    # Проверяем payload (нажатие кнопки)
    if hasattr(event.message, 'payload') and event.message.payload:
        try:
            payload = json.loads(event.message.payload)
        except Exception as e:
            log.warning(f'Ошибка парсинга payload: {e}')

    state = user_states.get(user_id, {'node': None, 'vars': {}})

    # Обработка /start или первого сообщения
    if text.lower() in ('/start', 'начать', 'start', 'привет'):
        state = {'node': None, 'vars': {}}
        user_states[user_id] = state
        run_chain(user_id, START_NODE, state)
        return

    # Нажатие кнопки-перехода (callback)
    if payload and 'next' in payload and payload['next']:
        next_id = payload['next']
        # next в JSON — это числовой ID или имя
        target = find_node_by_id(next_id) or next_id
        if target in NODES_BY_ID:
            run_chain(user_id, target, state)
            user_states[user_id] = state
        return

    # Ожидание текстового ввода
    if state.get('waiting_var'):
        var_name = state['waiting_var']
        state['vars'][var_name] = text
        state['waiting_var'] = None
        next_node = get_next_node(state['node'])
        if next_node:
            run_chain(user_id, next_node, state)
        user_states[user_id] = state
        return

    # Если ничего не подошло — предложим /start
    send_message(user_id, 'Напиши /start, чтобы начать заново.')


# ============== ЗАПУСК ==============

def main():
    log.info('Бот запущен. Слушаю сообщения...')
    log.info(f'Узлов в сценарии: {len(NODES_BY_ID)}')
    log.info(f'Стартовый узел: {START_NODE}')

    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    try:
                        handle_message(event)
                    except Exception as e:
                        log.error(f'Ошибка обработки сообщения: {e}', exc_info=True)
        except Exception as e:
            log.error(f'Ошибка LongPoll: {e}. Перезапуск через 5 сек...')
            time.sleep(5)


if __name__ == '__main__':
    main()
