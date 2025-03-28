from __future__ import annotations
import logging
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import CONF_TOKEN, Platform
from .const import DOMAIN, CONF_FILTER, CONF_INCLUDE_ENTITIES
from .service import DuerService
import aiohttp
import base64
import json
_LOGGER = logging.getLogger(__name__)
CONST_PLATFORMS = [Platform.BINARY_SENSOR,]


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug(f'update entry data {entry.source}')
    if entry.source == SOURCE_IMPORT:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up bemfa from a config entry."""
    if not hass.data.get(DOMAIN):
        hass.data.setdefault(DOMAIN, {})
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entity_filter = {}
    enttities = []
    data_conf = entry.data
    options_conf = entry.options

    # _LOGGER.debug(f'entry conf:{entry.as_dict()}')
    # _LOGGER.debug(f'entry data:{entry.data}')
    # _LOGGER.debug(f'entry options:{entry.options}')
    if not options_conf:
        entity_filter = data_conf.get(CONF_FILTER, {})
    else:
        entity_filter = options_conf.get(CONF_FILTER, {})
    if len(entity_filter[CONF_INCLUDE_ENTITIES]) > 0:
        enttities = entity_filter[CONF_INCLUDE_ENTITIES]
        _LOGGER.debug(f'include entities:{enttities}')
    service = DuerService(hass, entry.data[CONF_TOKEN])
    hass.data[DOMAIN][entry.entry_id] = {
        "service": service,
    }
    try:
        # 解析token中的base_url
        token = entry.data[CONF_TOKEN]
        token_bytes = base64.b64decode(token)
        token_data = json.loads(token_bytes.decode())
        base_url = token_data.get("web_url", "")
        
        # 尝试获取版本但不中断设置
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/api/plugin/version", ssl=False) as response:
                    _LOGGER.debug(f"版本检查状态码: {response.status}")
                    if response.status == 200:
                        try:
                            version_data = await response.json()
                            _LOGGER.info(f"插件版本: {version_data.get('version', 'unknown')}")
                        except Exception as e:
                            _LOGGER.warning(f"解析版本JSON错误，但继续设置: {e}")
        except Exception as e:
            _LOGGER.warning(f"版本检查失败，但继续设置: {e}")
        await service.async_start(
            enttities
        )
    except Exception as e:
        _LOGGER.warning(f"获取base_url失败，但继续设置: {e}")
        return False
    # for platform in CONST_PLATFORMS:
    #     hass.async_create_task(
    await hass.config_entries.async_forward_entry_setups(
        entry, CONST_PLATFORMS)
    # )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    unload_ok = False
    if data is not None:
        data["service"].stop()
        unload_ok = await hass.config_entries.async_unload_platforms(entry, CONST_PLATFORMS)
        if unload_ok:
            hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    # await hass.async_add_executor_job(
    #     remove_state_files_for_entry_id, hass, entry.entry_id
    # )
    _LOGGER.debug('remove entry invoke')
