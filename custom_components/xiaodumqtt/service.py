"""Support for bemfa service."""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime
from asyncio import Task, Lock, Queue
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import base64
import json
from aiohttp import ClientSession, ClientResponse
from .mqtt_service import DuerMqttService
from . import DOMAIN
from . const import CONST_POST_SYNC_DEVICE_URL, CONST_POST_SYNC_STATE_URL, CONST_GET_VERSION_CHECK_URL, CONST_VERSION, MQTT_TOPICS
import aiohttp

_LOGGER = logging.getLogger(__name__)
TOPIC_COMMAND = 'ha2xiaodu/command/'
TOPIC_REPORT = 'ha2xiaodu/report/'
TOPIC_PING = 'topic_ping'


class DuerService:
    """Service handles mqtt topocs and connection."""

    def __init__(self, hass: HomeAssistant, token: str) -> None:
        """Initialize."""
        self.hass = hass
        self._token = token
        self._duer_mqtt_service = DuerMqttService(hass)
        self.mqtt_online_cb: callable[None,
                                      bool] = None
        self.mqtt_online = False
        self._start = False
        self._duer_mqtt_service.on_message_cb_list.append(
            self._on_mqtt_message)
        self._duer_mqtt_service.on_connect_cb_list.append(
            self._on_mqtt_connect)
        self._mqtt_url: str = None
        self._web_url: str = None
        self._port: str = None
        self._user: str = None
        self._pwd: str = None
        self._version_check = False
        self._entity_list = []
        self._session = async_create_clientsession(self.hass, False, True)
        self._state_change_unsub = None
        self._sync_state_queue: Queue = Queue(3000)
        self._sync_state_task: Task = None
        self._sync_state_lock = Lock()

    def _sub_state_change(self):
        @callback
        async def _entity_state_change_processor(event) -> None:
            new_state: State = event.data.get("new_state")
            if new_state is None:
                return
            try:
                _LOGGER.debug(f"entity state change: {new_state}")
                self._sync_state_queue.put_nowait(new_state)
            except Exception as ex:
                _LOGGER.error(f'sync state queue full: {ex}')
        self._state_change_unsub = async_track_state_change_event(
            self.hass, self._entity_list, _entity_state_change_processor)
        _LOGGER.debug('state change sub success')

    async def async_start(self, entity_list: list) -> None:
        """启动服务"""
        self._entity_list = entity_list
        _LOGGER.debug('duer mqtt service start')
        _LOGGER.debug(f'token:{self._token}')
        self._duer_mqtt_service.entity_list = entity_list
        try:
            # 解析token
            token_bytes = base64.b64decode(self._token)
            token_data = json.loads(token_bytes.decode())
            self._mqtt_url = token_data.get('mqtt_url')
            self._web_url = token_data.get('web_url')
            self._port = token_data.get('port')
            self._user = token_data.get('username')
            self._pwd = token_data.get('password')
            
            # 尝试版本检查但不中断启动
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{self._web_url}/api/plugin/version", ssl=False) as response:
                        if response.status == 200:
                            data = await response.json()
                            _LOGGER.info(f"Plugin version: {data.get('version')}")
            except Exception as e:
                _LOGGER.warning(f"Version check warning (non-critical): {e}")
            
            # 继续MQTT启动流程
            self._version_check = await self._check_plugin_version()

            async def _start(event: Event | None = None):
                """启动服务的内部函数"""
                try:
                    if self._version_check:
                        _LOGGER.debug('check version ok start post data')
                        # 先启动MQTT连接
                        await self._duer_mqtt_service.connect(
                            self._mqtt_url, self._port, self._user, self._pwd)
                        
                        # 等待MQTT连接成功
                        for _ in range(10):  # 最多等待10秒
                            if self.mqtt_online:
                                break
                            await asyncio.sleep(1)
                        
                        if not self.mqtt_online:
                            _LOGGER.error("MQTT连接失败")
                            return
                        
                        # 同步所有设备信息
                        await self._sync_all_devices()
                        
                        # 订阅状态变化
                        self._sub_state_change()
                        
                        # 启动状态同步任务
                        self._sync_state_task = self.hass.async_create_background_task(
                            self._sync_entities_state_loop(), f'{self._user}_sync_state_entities')
                        
                        self._start = True
                except Exception as ex:
                    _LOGGER.error(f'start mqtt service err:{ex}')

            if self.hass.state == CoreState.running:
                await _start()
            else:
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, lambda _: self.hass.async_create_task(_start()))
        except Exception as e:
            _LOGGER.error(f"Failed to start service: {e}")
            raise

    async def _sync_all_devices(self):
        """同步所有设备信息和状态"""
        try:
            _LOGGER.info("开始同步所有设备信息和状态")
            _LOGGER.info(f"当前选定的设备列表: {self._entity_list}")
            
            # 同步设备列表
            entity_list = []
            for entity_id in self._entity_list:
                try:
                    state = self.hass.states.get(entity_id)
                    if not isinstance(state, State):
                        _LOGGER.warning(f"设备 {entity_id} 状态不可用")
                        continue
                    
                    # 获取设备域
                    domain = state.entity_id.split('.')[0]
                    _LOGGER.debug(f"正在处理设备: {entity_id}, 域: {domain}")
                    
                    # 构建设备信息
                    device_info = {
                        'entity_id': state.entity_id,
                        'domain': domain,
                        'friendly_name': state.attributes.get('friendly_name', state.entity_id),
                        'state': state.state,
                    }

                    # 对script类型设备进行特殊处理
                    if domain == 'script':
                        _LOGGER.debug(f"处理脚本设备: {entity_id}")
                        _LOGGER.debug(f"脚本原始状态: {state.state}")
                        _LOGGER.debug(f"脚本原始属性: {state.attributes}")
                        
                        # 处理last_triggered，确保它是字符串格式
                        last_triggered = state.attributes.get('last_triggered')
                        if last_triggered is not None:
                            last_triggered = last_triggered.isoformat() if hasattr(last_triggered, 'isoformat') else str(last_triggered)
                        
                        device_info.update({
                            'supported_features': 1,  # 基本的开关功能
                            'attributes': {
                                'friendly_name': state.attributes.get('friendly_name', state.entity_id),
                                'last_triggered': last_triggered,
                                'mode': state.attributes.get('mode', 'single'),
                                'icon': state.attributes.get('icon', None)
                            },
                            'state': 'off'  # 脚本总是处于"off"状态，直到被触发
                        })
                        _LOGGER.debug(f"处理后的脚本设备信息: {device_info}")
                    elif domain == 'cover':
                        # 复制属性字典，避免修改只读字典
                        attrs = dict(state.attributes)
                        # 处理可能包含datetime的属性
                        for key, value in attrs.items():
                            if hasattr(value, 'isoformat'):
                                attrs[key] = value.isoformat()
                        
                        # 添加百分比支持
                        supported_features = attrs.get('supported_features', 0)
                        current_position = attrs.get('current_position', 0)
                        current_time = int(datetime.now().timestamp())
                        
                        device_info.update({
                            'supported_features': supported_features,
                            'attributes': attrs,
                            'extensions': {
                                'percentage': {
                                    'name': 'percentage',
                                    'value': current_position,
                                    'scale': '%',
                                    'timestampOfSample': current_time,
                                    'uncertaintyInMilliseconds': 10,
                                    'legalValue': '[0, 100]'
                                }
                            }
                        })
                    else:
                        # 复制属性字典，避免修改只读字典
                        attrs = dict(state.attributes)
                        # 处理可能包含datetime的属性
                        for key, value in attrs.items():
                            if hasattr(value, 'isoformat'):
                                attrs[key] = value.isoformat()
                        device_info['supported_features'] = attrs.get('supported_features', 0)
                        device_info['attributes'] = attrs

                    entity_list.append(device_info)
                    _LOGGER.debug(f"添加设备: {device_info['friendly_name']} ({entity_id}), 域: {domain}")
                except Exception as e:
                    _LOGGER.error(f"处理设备 {entity_id} 时出错: {e}", exc_info=True)

            # 发送设备列表
            if entity_list:
                try:
                    # 首先发送清除所有设备的请求
                    clear_data = {
                        'type': 'clear_devices',
                        'openid': self._user,
                        'secret': self._pwd
                    }
                    _LOGGER.info("正在发送清除设备请求...")
                    clear_response = await self._post_data(f'{self._web_url}{CONST_POST_SYNC_DEVICE_URL}', clear_data)
                    _LOGGER.info(f"清除设备请求响应: {clear_response}")
                    
                    # 然后发送新的设备列表
                    post_device_data = {
                        'type': 'syncentity',
                        'data': entity_list,
                        'openid': self._user,
                        'secret': self._pwd
                    }
                    _LOGGER.info(f"正在同步 {len(entity_list)} 个设备...")
                    sync_response = await self._post_data(f'{self._web_url}{CONST_POST_SYNC_DEVICE_URL}', post_device_data)
                    _LOGGER.info(f"设备同步响应: {sync_response}")
                except Exception as e:
                    _LOGGER.error(f"设备同步过程中出错: {e}", exc_info=True)

            # 同步每个设备的当前状态
            _LOGGER.info("开始同步设备状态...")
            for entity_id in self._entity_list:
                try:
                    state = self.hass.states.get(entity_id)
                    if isinstance(state, State):
                        domain = state.entity_id.split('.')[0]
                        # 创建状态数据的可修改副本
                        state_data = {
                            'entity_id': state.entity_id,
                            'state': state.state,
                            'attributes': dict(state.attributes)  # 创建属性的副本
                        }
                        
                        # 对script类型的状态更新进行特殊处理
                        if domain == 'script':
                            _LOGGER.debug(f"同步脚本状态: {entity_id}")
                            # 处理last_triggered
                            last_triggered = state.attributes.get('last_triggered')
                            if last_triggered is not None:
                                last_triggered = last_triggered.isoformat() if hasattr(last_triggered, 'isoformat') else str(last_triggered)
                            
                            state_data['state'] = 'off'  # 脚本总是处于"off"状态
                            state_data['attributes'] = {
                                'friendly_name': state.attributes.get('friendly_name', state.entity_id),
                                'last_triggered': last_triggered,
                                'mode': state.attributes.get('mode', 'single'),
                                'icon': state.attributes.get('icon', None)
                            }
                            _LOGGER.debug(f"处理后的脚本状态数据: {state_data}")
                        else:
                            # 处理其他设备的datetime属性
                            for key, value in state_data['attributes'].items():
                                if hasattr(value, 'isoformat'):
                                    state_data['attributes'][key] = value.isoformat()
                        
                        post_state_data = {
                            'type': 'state_changed',
                            'data': state_data,
                            'openid': self._user,
                            'secret': self._pwd
                        }
                        state_response = await self._post_data(f'{self._web_url}{CONST_POST_SYNC_STATE_URL}', post_state_data)
                        _LOGGER.debug(f"设备 {entity_id} 状态同步响应: {state_response}")
                        await asyncio.sleep(0.1)  # 避免请求过于频繁
                except Exception as e:
                    _LOGGER.error(f"同步设备 {entity_id} 状态时出错: {e}", exc_info=True)
            
            _LOGGER.info("所有设备信息和状态同步完成")
        except Exception as e:
            _LOGGER.error(f"同步所有设备信息和状态失败: {e}", exc_info=True)

    def stop(self) -> None:
        """停止服务"""
        if self._state_change_unsub:
            self._state_change_unsub()
        if self._sync_state_task:
            self._sync_state_task.cancel()
        self._duer_mqtt_service.stop()
        self._start = False
        _LOGGER.info("服务已停止")

    async def _get_data(self, session: ClientSession, url: str):
        try:
            _LOGGER.debug(f"尝试请求URL: {url}")
            res: ClientResponse = await session.get(url)
            _LOGGER.debug(f"请求状态码: {res.status}")
            res.raise_for_status()
            dic_res: dict = await res.json()
            _LOGGER.debug(f"返回数据: {dic_res}")
            if 'code' in dic_res and dic_res['code'] == 0:
                _LOGGER.debug(f"res raw_data:{dic_res}")
                return dic_res
        except Exception as ex:
            _LOGGER.error(f'get data err:{ex}, URL: {url}')

    async def _post_data(self, url: str, data: dict):
        try:
            if isinstance(self._session, ClientSession):
                if self._session.closed:
                    self._session = async_create_clientsession(
                        self.hass, False, True)
            
            # 使用简单的Content-Type头，不添加Authorization头
            post_headers = {'Content-Type': 'application/json'}
            
            # 在数据中已经包含了openid和secret作为凭据
            j_data = json.dumps(data)
            _LOGGER.debug(f"post json:{j_data}")
            
            # 添加SSL=False参数禁用SSL验证
            res: ClientResponse = await self._session.post(
                url, data=j_data, headers=post_headers, ssl=False)
            
            _LOGGER.debug(f"POST请求状态码: {res.status}")
            res.raise_for_status()
            
            dic_res: dict = await res.json()
            if 'code' in dic_res and dic_res['code'] == 0:
                _LOGGER.debug(f"res raw_data:{dic_res}")
            else:
                _LOGGER.error(f"post data:{data}, res raw_data:{dic_res}")
            
        except Exception as ex:
            _LOGGER.error(f'post data err:{ex}, url={url}')

    async def _check_plugin_version(self):
        # 临时解决方案：直接返回True，绕过版本检查
        _LOGGER.warning("版本检查已被临时禁用")
        return True

    def _sync_device_entities(self, entities: list[str]):
        if isinstance(entities, list):
            _LOGGER.debug('start sync entities')
            entity_list = []
            for entity in entities:
                state: State = self.hass.states.get(entity)
                if isinstance(state, State):
                    entity_list.append(state.as_dict())
            post_device_data = {
                'type': 'syncentity',
                'data': entity_list,
                'openid': self._user,
                'secret': self._pwd
            }
            self.hass.add_job(
                self._post_data(f'{self._web_url}{CONST_POST_SYNC_DEVICE_URL}', post_device_data))
            _LOGGER.debug('sync entities finish')

    @callback
    async def _sync_entities_state_loop(self):
        # await asyncio.sleep(10)
        # _LOGGER.debug('start sync entities')
        # for entity in self._entity_list:
        #     e_state: State = self.hass.states.get(entity)
        #     if isinstance(e_state, State):
        #         post_device_data = {
        #             'type': 'state_changed',
        #             'data': e_state.as_dict(),
        #             'openid': self._user,
        #             'secret': self._pwd
        #         }
        #         await self._post_data(self._session, f'{self._web_url}{CONST_POST_SYNC_STATE_URL}', post_device_data)
        #         await asyncio.sleep(0.01)
        _LOGGER.debug('start sync state queue loop')
        # self._sync_state_queue = asyncio.Queue(3000)
        while True:
            try:
                if isinstance(self._sync_state_queue, asyncio.Queue):
                    state: State = await self._sync_state_queue.get()
                    self._sync_state_queue.task_done()
                    _LOGGER.debug('post_change data')
                    if isinstance(state, State):
                        post_device_data = {
                            'type': 'state_changed',
                            'data': state.as_dict(),
                            'openid': self._user,
                            'secret': self._pwd
                        }
                        await self._post_data(f'{self._web_url}{CONST_POST_SYNC_STATE_URL}', post_device_data)
            except Exception as ex:
                _LOGGER.error(f'get queue error {ex}')
            await asyncio.sleep(0.01)

    def _on_mqtt_connect(self, state):
        """MQTT连接状态回调"""
        self.mqtt_online = state
        if state:
            # 移除用户名后缀
            base_username = self._user.split('_')[0] if '_' in self._user else self._user
            # 使用统一的主题格式
            self._duer_mqtt_service.subscribe(
                MQTT_TOPICS['COMMAND'].format(username=base_username), 0)
            _LOGGER.debug(f"已订阅主题: {MQTT_TOPICS['COMMAND'].format(username=base_username)}")
        
        if callable(self.mqtt_online_cb):
            self.mqtt_online_cb(state)

    def _on_mqtt_message(self, data: dict):
        _LOGGER.debug(f'get mqtt message:{data}')
        cmd_type = data.get('type')
        if cmd_type:
            match cmd_type:
                case 'syncentity':
                    _LOGGER.debug(f'sync device entitys:{self._entity_list}')
                    self._sync_device_entities(self._entity_list)
                case 'callservice':
                    # 确保传递完整的消息数据，包括raw_payload
                    self._call_service(data)
                case 'device_removed':
                    # 处理设备删除通知
                    entity_id = data.get('entity_id')
                    friendly_name = data.get('friendly_name')
                    _LOGGER.info(f"收到设备删除通知: {entity_id} ({friendly_name})")
                    # 从实体列表中移除设备
                    if entity_id in self._entity_list:
                        self._entity_list.remove(entity_id)
                        _LOGGER.info(f"已从实体列表中移除设备: {entity_id}")
                        # 重新同步设备列表
                        self._sync_device_entities(self._entity_list)

    def _call_service(self, data: dict) -> None:
        _LOGGER.debug(f'call hass service: {data}')
        service = data.get('service')
        s_data = data.get('service_data', {})
        entity_id = data.get('entity_id')
        domain = entity_id.split('.')[0]
        command = data.get('command')
        raw_payload = data.get('raw_payload', {})
        _LOGGER.debug(f'处理命令: {command}, raw_payload: {raw_payload}')

        # 处理灯光命令
        if domain == 'light':
            if command == 'SetBrightnessPercentage':
                service = 'turn_on'
                # 从raw_payload中正确获取亮度值
                brightness = raw_payload.get('brightness', {}).get('value', 0)
                _LOGGER.debug(f'设置灯光亮度到: {brightness}%')
                s_data['brightness_pct'] = brightness
            elif command == 'TurnOn':
                service = 'turn_on'
            elif command == 'TurnOff':
                service = 'turn_off'
        # 处理窗帘的特殊命令
        elif domain == 'cover':
            if command == 'TurnOn':
                # 检查是否包含百分比值
                delta_value = raw_payload.get('deltaValue', {})
                if delta_value and 'value' in delta_value:
                    # 如果包含百分比值，使用set_cover_position
                    service = 'set_cover_position'
                    percentage = int(delta_value.get('value', 0))
                    _LOGGER.debug(f'设置窗帘位置到: {percentage}%')
                    s_data['position'] = percentage
                else:
                    # 如果没有百分比值，执行完全打开
                    service = 'open_cover'
            elif command == 'TurnOff':
                service = 'close_cover'
            elif command == 'Pause':
                service = 'stop_cover'
            elif command == 'SetOpenPercentage' or command == 'TurnOnPercent':
                service = 'set_cover_position'
                # 从raw_payload中获取百分比值
                delta_value = raw_payload.get('deltaValue', {})
                if delta_value and 'value' in delta_value:
                    try:
                        percentage = int(str(delta_value['value']).strip())
                        _LOGGER.debug(f'设置窗帘位置到: {percentage}%')
                        s_data['position'] = percentage
                    except ValueError as e:
                        _LOGGER.error(f'解析百分比值出错: {e}')
                else:
                    _LOGGER.error(f'无法从payload中获取百分比值: {raw_payload}')

        # 确保entity_id在service_data中
        if 'entity_id' not in s_data:
            s_data['entity_id'] = entity_id

        # 调用Home Assistant服务
        _LOGGER.debug(f'调用服务 {domain}.{service}, 数据: {s_data}')
        self.hass.async_create_task(
            self.hass.services.async_call(domain, service, s_data)
        )
