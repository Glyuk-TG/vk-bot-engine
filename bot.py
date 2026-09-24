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
NODES_BY_ID = {n['name']: n for n in NODES.values()}
START_NODE = 'msg_1'

# ============== СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ ==============

user_states = {}

# ============== VK API ==============

vk_session = VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)

# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============

def get_user_name(user_id):
    try:
        res = vk.users.get(user_ids=user_id, fields='first_name')
        if res and len(res) > 0:
            return res[0]['first_name']
    except Exception as e:
        log.warning(f'Не удалось получить имя для {user_id}: {e}')
    return 'друг'


def replace_vars(text, user_id, state):
    if not text:
        return ''
    text = text.replace('%first_name%', get_user_name(user_id))
    for var_name, var_value in state.get('vars', {}).items():
        text = text.replace(f'%{var_name}%', str(var_value))
    return text


def build_keyboard(buttons, inline=False):
    """Собирает клавиатуру VK с поддержкой open_link и callback"""
    keyboard = VkKeyboard(one_time=False, inline=inline)

    callback_buttons = [b for b in buttons if not b.get('url')]
    link_buttons = [b for b in buttons if b.get('url')]

    first = True

    # Ссылочные кнопки
    for btn in link_buttons:
        if not first:
            keyboard.add_line()
        first = False
        keyboard.add_openlink_button(
            label=btn['label'][:40],
            link=btn['url']
        )

    # Callback-кнопки
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


def find_node_by_id(node_id):
    """Ищет узел по числовому ID (msg_4, btn_4, ...)"""
    for nid, n in NODES_BY_ID.items():
        if nid.endswith(f'_{node_id}') or nid == node_id:
            return nid
    return None


def get_next_node(node_id):
    node = NODES_BY_ID.get(node_id)
    if not node:
        return None
    outputs = node.get('outputs', {})
    for out_key in sorted(outputs.keys()):
        connections = outputs[out_key].get('connections', [])
        if connections:
            target_id = connections[0]['node']
            return find_node_by_id(target_id)
    return None


def process_node(user_id, node_id, state):
    node = NODES_BY_ID.get(node_id)
    if not node:
        log.warning(f'Узел не найден: {node_id}')
        return None

    ntype = node['data'].get('type')

    if ntype == 'message':
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        buttons = node['data'].get('buttons', [])
        keyboard_type = node['data'].get('keyboard_type', 'reply')
        keyboard = None
        if buttons:
            keyboard = build_keyboard(buttons, inline=(keyboard_type == 'inline'))
        send_message(user_id, text, keyboard=keyboard)

        # Автопереход (эмуляция)
        redirect = node['data'].get('auto_redirect')
        if redirect:
            delay = redirect.get('delay', 5)
            url = redirect.get('url', '')
            if url:
                time.sleep(delay)
                kb = VkKeyboard(inline=True)
                kb.add_openlink_button(label='↩️ На главную', link=url)
                send_message(user_id, '👇 Нажми, чтобы перейти:', keyboard=kb)

        if buttons:
            return None  # ждём нажатия
        return get_next_node(node_id)

    elif ntype == 'buttons':
        # На случай, если остались старые узлы типа buttons
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        buttons = node['data'].get('buttons', [])
        keyboard = build_keyboard(buttons) if buttons else None
        send_message(user_id, text, keyboard=keyboard)
        return None

    elif ntype == 'wait':
        text = replace_vars(node['data'].get('text', ''), user_id, state)
        send_message(user_id, text)
        state['waiting_var'] = node['data'].get('var_name', 'answer')
        return None

    return None


def run_chain(user_id, start_node_id, state):
    current = start_node_id
    steps = 0
    max_steps = 50

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
        return

    text = (event.message.text or '').strip()
    payload = None

    if hasattr(event.message, 'payload') and event.message.payload:
        try:
            payload = json.loads(event.message.payload)
        except Exception as e:
            log.warning(f'Ошибка парсинга payload: {e}')

    state = user_states.get(user_id, {'node': None, 'vars': {}})

    # /start
    if text.lower() in ('/start', 'начать', 'start', 'привет'):
        state = {'node': None, 'vars': {}}
        user_states[user_id] = state
        run_chain(user_id, START_NODE, state)
        return

    # Нажатие callback-кнопки
    if payload and 'next' in payload and payload['next']:
        next_id = payload['next']
        target = find_node_by_id(next_id)
        if target:
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

    # Фолбэк: реагируем на текст, совпадающий с label кнопок
    current_node_name = state.get('node')
    if current_node_name:
        current_node = NODES_BY_ID.get(current_node_name)
        if current_node:
            for btn in current_node['data'].get('buttons', []):
                if btn['label'].strip().lower() == text.lower():
                    if btn.get('url'):
                        # Открываем ссылку — VK сам не откроет, но хотя бы сообщим
                        send_message(user_id, f'🔗 {btn["url"]}')
                    elif btn.get('next'):
                        target = find_node_by_id(btn['next'])
                        if target:
                            run_chain(user_id, target, state)
                            user_states[user_id] = state
                    return

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
