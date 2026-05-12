import json
import os
import asyncio
import time
import random
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Any
from pathlib import Path

import aiofiles

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api import FunctionTool
from astrbot.core.provider.entities import ProviderRequest

# ===================== 常量定义 =====================
DEFAULT_CONVERSATION_ID = "default_conversation"
DEFAULT_BODY_NAME = "默认身体"
DEFAULT_OUTFIT_NAME = "常服"
TEMP_CONVERSATION_PREFIX = "default_conversation_temp_"
TEMP_FILE_MAX_AGE_HOURS = 24
DEFAULT_DESCRIPTION = "无简介"

# 配置默认值
DEFAULT_ALLOW_LLM_MODIFY_BODY = False
DEFAULT_ALLOW_LLM_SWITCH_BODY = True
DEFAULT_ALLOW_LLM_MODIFY_OUTFIT = True
DEFAULT_ALLOW_LLM_SWITCH_OUTFIT = True
DEFAULT_ALLOW_CUSTOM_FIELDS = True
DEFAULT_LLM_INSERT_POSITION = "system_prompt_end"

# 提示词模板默认值
DEFAULT_BODY_PREFIX_PROMPT = "【你当前的身体形象（必须严格遵守）】"
DEFAULT_BODY_SUFFIX_PROMPT = "【身体形象设定结束】"
DEFAULT_OUTFIT_PREFIX_PROMPT = "【你当前的衣着形象（必须严格遵守）】"
DEFAULT_OUTFIT_SUFFIX_PROMPT = "【衣着形象设定结束】"
DEFAULT_AVAILABLE_BODY_LIST_PREFIX = "\n【可选身体方案】"
DEFAULT_AVAILABLE_BODY_LIST_SUFFIX = "【可选身体方案结束】"
DEFAULT_AVAILABLE_OUTFIT_LIST_PREFIX = "\n【可用衣着列表（可自主切换）】"
DEFAULT_AVAILABLE_OUTFIT_LIST_SUFFIX = "【可用衣着列表结束】"

# ===================== 新数据模型 =====================
@dataclass
class BodySchema:
    """身体方案"""
    description: str = field(default=DEFAULT_DESCRIPTION)
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."

@dataclass
class Outfit:
    """衣着方案"""
    description: str = field(default=DEFAULT_DESCRIPTION)
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."

@dataclass
class ConversationAvatar:
    """对话级完整形象数据"""
    conversation_id: str
    current_body: str = DEFAULT_BODY_NAME
    bodies: Dict[str, BodySchema] = field(default_factory=dict)
    current_outfit: str = DEFAULT_OUTFIT_NAME
    outfits: Dict[str, Outfit] = field(default_factory=dict)

# ===================== 会话ID获取 =====================
def _is_valid_id_value(value: Any) -> bool:
    """检查ID值是否可行（排除 None、空串、布尔值、数值0等无效标识）"""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, int) and value == 0:
        return False
    str_val = str(value).strip()
    if not str_val:
        return False
    if str_val == "0":
        return False
    return True

def _get_conversation_id(event: AstrMessageEvent) -> str:
    """获取稳定会话ID，失败时生成唯一兜底"""
    # 辅助：安全获取 sender_id / platform（优先调用官方方法，getattr 兜底）
    def _safe_sender_id(ev) -> Optional[str]:
        if hasattr(ev, "get_sender_id"):
            return ev.get_sender_id()
        return getattr(ev, "sender_id", None)

    def _safe_platform(ev) -> Optional[str]:
        if hasattr(ev, "get_platform_name"):
            return ev.get_platform_name()
        return getattr(ev, "platform", None)

    def _sid_from_event(ev: AstrMessageEvent) -> Optional[str]:
        if ev is None:
            return None
        # 优先尝试获取 session_id
        sid = getattr(ev, "session_id", None)
        if _is_valid_id_value(sid):
            return str(sid).strip()
        # 尝试 get_session_id 方法
        if hasattr(ev, "get_session_id"):
            sid = ev.get_session_id()
            if _is_valid_id_value(sid):
                return str(sid).strip()
        # 尝试 message_obj 中的 session_id
        if hasattr(ev, "message_obj") and hasattr(ev.message_obj, "session_id"):
            msg_sid = ev.message_obj.session_id
            if _is_valid_id_value(msg_sid):
                return str(msg_sid).strip()
        # 尝试 unified_msg_origin
        unified_origin = getattr(ev, "unified_msg_origin", None)
        if _is_valid_id_value(unified_origin):
            return str(unified_origin).strip()
        # 尝试 sender_id + platform 组合
        sender_id = _safe_sender_id(ev)
        platform = _safe_platform(ev)
        if _is_valid_id_value(sender_id) and _is_valid_id_value(platform):
            return f"{str(platform).strip()}_{str(sender_id).strip()}"
        # 尝试仅 sender_id（即使没有platform也比随机好）
        if _is_valid_id_value(sender_id):
            return f"sender_{str(sender_id).strip()}"
        # 尝试 group_id + platform 组合（群消息场景）
        group_id = getattr(ev, "group_id", None)
        if _is_valid_id_value(group_id) and _is_valid_id_value(platform):
            return f"{str(platform).strip()}_group_{str(group_id).strip()}"
        # 尝试 message_id（虽然不是会话级，但至少能区分消息）
        message_id = getattr(ev, "message_id", None)
        if _is_valid_id_value(message_id):
            return f"msg_{str(message_id).strip()}"
        return None

    sid = _sid_from_event(event)
    if sid:
        # 清洗：只保留 ASCII 字母、数字、- 和 _
        safe_sid = "".join(
            c if (c.isascii() and (c.isalnum() or c in "-_")) else "_"
            for c in sid
        )
        logger.debug(f"会话ID: {safe_sid}")
        return safe_sid
    
    # 极端情况：无法获取任何稳定标识
    # 使用时间戳+随机数生成唯一ID，避免不同用户共享数据
    sender_id = _safe_sender_id(event)
    platform = _safe_platform(event)
    
    if _is_valid_id_value(sender_id):
        fallback_id = f"{DEFAULT_CONVERSATION_ID}_sender_{str(sender_id).strip()}"
    elif _is_valid_id_value(platform):
        fallback_id = f"{DEFAULT_CONVERSATION_ID}_{str(platform).strip()}_unknown"
    else:
        # 完全无法识别身份，生成唯一临时ID
        # 使用时间戳+随机数确保唯一性，避免数据混淆
        fallback_id = f"{DEFAULT_CONVERSATION_ID}_temp_{int(time.time())}_{random.randint(0, 999999)}"
        # 记录严重警告，提示数据不会持久化
        logger.error(
            f"无法获取任何会话标识！生成临时ID: {fallback_id} "
            f"(事件类型: {type(event).__name__})"
        )
        logger.warning(
            f"[{fallback_id}] 警告：本次会话使用临时ID，形象数据可能无法正确持久化"
        )
    
    logger.warning(
        f"无法获取稳定会话ID，使用兜底: {fallback_id} (事件类型: {type(event).__name__})"
    )
    return fallback_id

# ===================== 会话级锁管理器 =====================
class ConversationLockManager:
    def __init__(self, ttl_seconds: int = 300):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_access: Dict[str, float] = {}
        self._global_lock = asyncio.Lock()
        self.ttl = ttl_seconds
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_started: bool = False
        self._data_dir: Optional[Path] = None

    def set_data_dir(self, data_dir: Path):
        """设置数据目录，用于临时文件清理"""
        self._data_dir = data_dir

    async def _cleanup_loop(self):
        """定期清理过期锁和临时会话文件"""
        while True:
            await asyncio.sleep(600)  # 每10分钟清理一次
            
            # 第一步：在锁内收集待清理的锁
            to_remove = []
            async with self._global_lock:
                now = time.monotonic()
                for cid, last in list(self._last_access.items()):
                    if now - last > self.ttl:
                        lock = self._locks.get(cid)
                        if lock and not lock.locked():
                            to_remove.append(cid)
            
            # 第二步：释放锁后执行清理操作（删除前重新检查TTL，防止竞态）
            for cid in to_remove:
                async with self._global_lock:
                    last = self._last_access.get(cid)
                    if last is None:
                        continue
                    now = time.monotonic()
                    if now - last <= self.ttl:
                        logger.debug(f"跳过清理 {cid}：在收集期间被重新访问")
                        continue
                    self._locks.pop(cid, None)
                    self._last_access.pop(cid, None)
                logger.debug(f"已清理过期锁: {cid}")
            
            # 第三步：清理临时文件（不使用锁，避免阻塞）
            await self._cleanup_temp_files_async()

    async def _cleanup_temp_files_async(self):
        """清理超过24小时的临时会话文件（异步执行，避免长时间持锁）"""
        if not self._data_dir:
            return
        try:
            now = time.time()
            max_age_seconds = TEMP_FILE_MAX_AGE_HOURS * 3600
            temp_files = list(self._data_dir.glob(f"{TEMP_CONVERSATION_PREFIX}*.json"))
            
            for file_path in temp_files:
                try:
                    file_age = now - file_path.stat().st_mtime
                    if file_age > max_age_seconds:
                        file_path.unlink()
                        logger.info(f"已清理过期临时会话文件: {file_path.name}")
                except OSError as e:
                    logger.warning(f"清理临时文件 {file_path.name} 失败: {e}")
        except Exception as e:
            logger.warning(f"临时文件清理过程出错: {e}")

    def _is_cleanup_running(self) -> bool:
        """检查清理任务是否正在运行"""
        if self._cleanup_task is None:
            return False
        return not self._cleanup_task.done()

    def start(self):
        """显式启动清理任务，应在插件初始化时调用"""
        # 检查任务是否正在运行，如果已完成则需要重新启动
        if not self._is_cleanup_running():
            try:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                self._cleanup_started = True
                logger.debug("锁管理器清理任务已启动")
            except RuntimeError as e:
                logger.error(f"启动锁管理器清理任务失败: {e}")
                # 重置标志位，允许后续重试
                self._cleanup_started = False
                self._cleanup_task = None

    async def get_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._global_lock:
            # 懒启动兜底：如果清理任务未运行，尝试在首次获取锁时启动
            if not self._is_cleanup_running():
                try:
                    self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                    self._cleanup_started = True
                    logger.debug("锁管理器清理任务已启动（懒启动）")
                except RuntimeError as e:
                    logger.warning(f"懒启动锁管理器清理任务失败: {e}")
                    self._cleanup_started = False
                    self._cleanup_task = None
            now = time.monotonic()
            self._last_access[conversation_id] = now
            if conversation_id not in self._locks:
                self._locks[conversation_id] = asyncio.Lock()
            return self._locks[conversation_id]

    async def cleanup(self, conversation_id: str):
        """立即移除指定会话的锁"""
        async with self._global_lock:
            self._locks.pop(conversation_id, None)
            self._last_access.pop(conversation_id, None)

    async def close(self):
        """取消后台清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._cleanup_started = False
        self._cleanup_task = None

_lock_manager = ConversationLockManager(ttl_seconds=300)

# ===================== 辅助函数：字段过滤 =====================
def filter_fields(fields: Dict[str, str], plugin) -> Dict[str, str]:
    """根据配置过滤不允许的字段"""
    allow_custom = plugin.config.get(
        "allow_custom_fields", DEFAULT_ALLOW_CUSTOM_FIELDS
    )
    if allow_custom:
        return fields
    allowed_str = plugin.config.get("allowed_fields", "")
    allowed = [f.strip() for f in allowed_str.split(",") if f.strip()]
    # 修复：如果白名单为空，返回空字典，禁止所有字段
    if not allowed:
        logger.warning("允许自定义字段已关闭且白名单为空，所有字段操作将被拒绝")
        return {}
    return {k: v for k, v in fields.items() if k in allowed}

# ===================== LLM工具：身体方案操作 =====================
@dataclass
class CreateBodySchemaTool(FunctionTool):
    name: str = "create_body_schema"
    description: str = "创建身体方案（发色、瞳色、身高、胸围等）。仅在目标方案不存在时才能调用，若已存在将返回错误。"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "schema_name": {"type": "string", "description": "身体方案名称，如「正常体型」"},
            "description": {"type": "string", "description": "50字内简介"},
            "fields": {"type": "object", "description": "身体词条，如：{\"发色\":\"蓝色长发\",\"瞳色\":\"金色\"}"}
        },
        "required": ["schema_name", "fields"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str, fields: Dict[str, str], 
                       description: str = DEFAULT_DESCRIPTION):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_body", DEFAULT_ALLOW_LLM_MODIFY_BODY
        ):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            fields = filter_fields(fields, self._avatar_plugin_instance)
            if not fields:
                return "❌ 当前配置下不允许创建任何字段，请联系管理员调整配置"
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar:
                avatar = ConversationAvatar(conversation_id=conversation_id)
            # 检查方案是否已存在，不允许覆盖
            if schema_name in avatar.bodies:
                return f"❌ 身体方案 [{schema_name}] 已存在，请使用修改工具或先删除后重建"
            body_schema = BodySchema(description=description, fields=fields)
            avatar.bodies[schema_name] = body_schema
            if len(avatar.bodies) == 1:
                avatar.current_body = schema_name
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 身体方案 [{schema_name}] 已保存")
            return f"✅ 身体方案 [{schema_name}] 已保存\n简介：{description}\n词条：{fields}"

@dataclass
class SelectBodySchemaTool(FunctionTool):
    name: str = "select_body_schema"
    description: str = "切换当前使用的身体方案"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"schema_name": {"type": "string", "description": "身体方案名称"}},
        "required": ["schema_name"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_switch_body", DEFAULT_ALLOW_LLM_SWITCH_BODY
        ):
            return "❌ 管理员已禁止LLM切换身体方案"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.warning(f"[{conversation_id}] 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            avatar.current_body = schema_name
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 当前身体方案已切换为 [{schema_name}]")
            return f"✅ 当前身体方案已切换为 [{schema_name}]"

@dataclass
class ModifyBodyFieldTool(FunctionTool):
    name: str = "modify_body_field"
    description: str = "修改身体方案的单个词条或简介（仅1-3条修改）"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "schema_name": {"type": "string", "description": "身体方案名称"},
            "field_name": {"type": "string", "description": "词条名或description"},
            "field_value": {"type": "string", "description": "新值"}
        },
        "required": ["schema_name", "field_name", "field_value"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str, field_name: str, 
                       field_value: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_body", DEFAULT_ALLOW_LLM_MODIFY_BODY
        ):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.warning(f"[{conversation_id}] 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            if field_name == "description":
                if len(field_value) > 50:
                    field_value = field_value[:47] + "..."
                avatar.bodies[schema_name].description = field_value
                await self._avatar_plugin_instance.save_conversation_avatar(avatar)
                logger.info(f"[{conversation_id}] 身体方案 [{schema_name}] 简介已更新")
                return f"✅ 身体方案 [{schema_name}] 简介已更新：{field_value}"
            # 字段过滤
            filtered = {field_name: field_value}
            filtered = filter_fields(filtered, self._avatar_plugin_instance)
            if field_name not in filtered:
                logger.warning(f"[{conversation_id}] 词条 [{field_name}] 不在允许列表中")
                return f"❌ 词条 [{field_name}] 不在允许列表中，请联系管理员"
            avatar.bodies[schema_name].fields[field_name] = field_value
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 身体词条已修改：{field_name} → {field_value}")
            return f"✅ 身体词条已修改：{field_name} → {field_value}"

@dataclass
class DeleteBodySchemaTool(FunctionTool):
    name: str = "delete_body_schema"
    description: str = "删除身体方案（不能删除当前使用的）"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"schema_name": {"type": "string", "description": "身体方案名称"}},
        "required": ["schema_name"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_body", DEFAULT_ALLOW_LLM_MODIFY_BODY
        ):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.warning(f"[{conversation_id}] 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            if avatar.current_body == schema_name:
                logger.warning(f"[{conversation_id}] 不能删除当前正在使用的身体方案 [{schema_name}]")
                return f"❌ 不能删除当前正在使用的身体方案，请先切换"
            del avatar.bodies[schema_name]
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 身体方案 [{schema_name}] 已删除")
            return f"✅ 身体方案 [{schema_name}] 已删除"

# ===================== LLM工具：衣着方案操作 =====================
@dataclass
class CreateOutfitTool(FunctionTool):
    name: str = "create_avatar_outfit"
    description: str = "创建衣着方案（上衣、下着、鞋子等）。仅在目标方案不存在时才能调用，若已存在将返回错误。"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "衣着名称"},
            "description": {"type": "string", "description": "50字内简介"},
            "fields": {"type": "object", "description": "衣着词条"}
        },
        "required": ["outfit_name", "fields"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, fields: Dict[str, str], 
                       description: str = DEFAULT_DESCRIPTION):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_outfit", DEFAULT_ALLOW_LLM_MODIFY_OUTFIT
        ):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            fields = filter_fields(fields, self._avatar_plugin_instance)
            if not fields:
                return "❌ 当前配置下不允许创建任何字段，请联系管理员调整配置"
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar:
                avatar = ConversationAvatar(conversation_id=conversation_id)
            # 检查方案是否已存在，不允许覆盖
            if outfit_name in avatar.outfits:
                return f"❌ 衣着方案 [{outfit_name}] 已存在，请使用修改工具或先删除后重建"
            outfit = Outfit(description=description, fields=fields)
            avatar.outfits[outfit_name] = outfit
            if len(avatar.outfits) == 1:
                avatar.current_outfit = outfit_name
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 衣着方案 [{outfit_name}] 已保存")
            return f"✅ 衣着方案 [{outfit_name}] 已保存\n简介：{description}\n词条：{fields}"

@dataclass
class SelectOutfitTool(FunctionTool):
    name: str = "select_avatar_outfit"
    description: str = "切换当前衣着方案"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "衣着名称"}},
        "required": ["outfit_name"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_switch_outfit", DEFAULT_ALLOW_LLM_SWITCH_OUTFIT
        ):
            return "❌ 管理员已禁止LLM切换衣着方案"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.warning(f"[{conversation_id}] 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            avatar.current_outfit = outfit_name
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 当前衣着已切换为 [{outfit_name}]")
            return f"✅ 当前衣着已切换为 [{outfit_name}]"

@dataclass
class ModifyOutfitFieldTool(FunctionTool):
    name: str = "modify_avatar_field"
    description: str = "修改衣着方案的单个词条或简介（1-3条）"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "衣着名称"},
            "field_name": {"type": "string", "description": "词条名或description"},
            "field_value": {"type": "string", "description": "新值"}
        },
        "required": ["outfit_name", "field_name", "field_value"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, field_name: str, 
                       field_value: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_outfit", DEFAULT_ALLOW_LLM_MODIFY_OUTFIT
        ):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.warning(f"[{conversation_id}] 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            if field_name == "description":
                if len(field_value) > 50:
                    field_value = field_value[:47] + "..."
                avatar.outfits[outfit_name].description = field_value
                await self._avatar_plugin_instance.save_conversation_avatar(avatar)
                logger.info(f"[{conversation_id}] 衣着 [{outfit_name}] 简介已更新")
                return f"✅ 衣着 [{outfit_name}] 简介已更新：{field_value}"
            filtered = {field_name: field_value}
            filtered = filter_fields(filtered, self._avatar_plugin_instance)
            if field_name not in filtered:
                logger.warning(f"[{conversation_id}] 词条 [{field_name}] 不在允许列表中")
                return f"❌ 词条 [{field_name}] 不在允许列表中"
            avatar.outfits[outfit_name].fields[field_name] = field_value
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 衣着词条已修改：{field_name} → {field_value}")
            return f"✅ 衣着词条已修改：{field_name} → {field_value}"

@dataclass
class DeleteOutfitTool(FunctionTool):
    name: str = "delete_avatar_outfit"
    description: str = "删除衣着方案（不能删除当前使用的）"
    _avatar_plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "衣着名称"}},
        "required": ["outfit_name"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        if not self._avatar_plugin_instance.config.get(
            "allow_llm_modify_outfit", DEFAULT_ALLOW_LLM_MODIFY_OUTFIT
        ):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self._avatar_plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.warning(f"[{conversation_id}] 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            if avatar.current_outfit == outfit_name:
                logger.warning(f"[{conversation_id}] 不能删除当前衣着 [{outfit_name}]")
                return f"❌ 不能删除当前衣着，请先切换"
            del avatar.outfits[outfit_name]
            await self._avatar_plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"[{conversation_id}] 衣着 [{outfit_name}] 已删除")
            return f"✅ 衣着 [{outfit_name}] 已删除"

# ===================== 插件主类 =====================
class BotAvatarManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 启动锁管理器清理任务
        _lock_manager.set_data_dir(self.data_dir)
        _lock_manager.start()

        # 记录当前注册的工具名称，用于动态更新
        self._registered_tool_names: set = set()

        # 注册LLM工具（根据配置动态添加）
        self._sync_llm_tools()

    def _build_tool_list(self) -> list:
        """根据当前配置构建工具列表"""
        tools = []
        if self.config.get("allow_llm_modify_body", DEFAULT_ALLOW_LLM_MODIFY_BODY):
            tools.extend([CreateBodySchemaTool(_avatar_plugin_instance=self),
                          ModifyBodyFieldTool(_avatar_plugin_instance=self),
                          DeleteBodySchemaTool(_avatar_plugin_instance=self)])
        if self.config.get("allow_llm_switch_body", DEFAULT_ALLOW_LLM_SWITCH_BODY):
            tools.append(SelectBodySchemaTool(_avatar_plugin_instance=self))
        if self.config.get("allow_llm_modify_outfit", DEFAULT_ALLOW_LLM_MODIFY_OUTFIT):
            tools.extend([CreateOutfitTool(_avatar_plugin_instance=self),
                          ModifyOutfitFieldTool(_avatar_plugin_instance=self),
                          DeleteOutfitTool(_avatar_plugin_instance=self)])
        if self.config.get("allow_llm_switch_outfit", DEFAULT_ALLOW_LLM_SWITCH_OUTFIT):
            tools.append(SelectOutfitTool(_avatar_plugin_instance=self))
        return tools

    def _sync_llm_tools(self):
        """同步LLM工具列表，确保工具列表与配置完全一致"""
        new_tools = self._build_tool_list()
        new_tool_names = {t.name for t in new_tools}

        # 确定需要移除和添加的工具
        tools_to_remove = self._registered_tool_names - new_tool_names
        tools_to_add = [t for t in new_tools if t.name not in self._registered_tool_names]

        # 尝试动态移除工具
        remove_supported = hasattr(self.context, 'remove_llm_tool')
        removal_success = True
        
        if remove_supported and tools_to_remove:
            for tool_name in tools_to_remove:
                try:
                    self.context.remove_llm_tool(tool_name)
                    logger.info(f"[{self.name}] 已移除LLM工具: {tool_name}")
                except Exception as e:
                    logger.warning(f"[{self.name}] 移除工具 {tool_name} 失败: {e}")
                    removal_success = False
        
        # 只有当所有待移除工具均成功移除时，才更新内部记录
        if tools_to_remove:
            if not removal_success:
                # 部分移除失败，保持内部记录与实际一致
                logger.warning(
                    f"[{self.name}] 部分工具移除失败，内部记录保持不变。"
                    f"实际注册工具可能包含: {self._registered_tool_names}"
                )
            elif not remove_supported:
                # 框架不支持动态移除工具API
                logger.warning(
                    f"[{self.name}] 警告：当前AstrBot版本不支持动态移除LLM工具API（remove_llm_tool）。"
                    f"禁用的工具({tools_to_remove})将在下次插件重载后生效，"
                    f"当前会话中这些工具仍可能被LLM调用。"
                )
            else:
                # 所有移除成功，更新内部记录
                self._registered_tool_names = new_tool_names
        else:
            # 无需移除，直接更新记录
            self._registered_tool_names = new_tool_names

        # 添加新工具
        if tools_to_add:
            self.context.add_llm_tools(*tools_to_add)
            logger.info(f"[{self.name}] 已添加LLM工具: {[t.name for t in tools_to_add]}")
            # 添加成功后更新记录
            self._registered_tool_names.update({t.name for t in tools_to_add})

        # 输出同步状态日志
        if tools_to_remove and not removal_success:
            logger.warning(
                f"[{self.name}] 工具同步完成但存在不一致，期望工具: {new_tool_names}, "
                f"内部记录: {self._registered_tool_names}"
            )
        elif tools_to_remove and not remove_supported:
            logger.warning(
                f"[{self.name}] 工具同步未完成：{len(tools_to_remove)}个禁用工具因框架限制无法移除，"
                f"请重载插件以完全生效。期望工具: {sorted(new_tool_names)}"
            )
        else:
            logger.info(
                f"[{self.name}] 工具同步完成，当前工具数量: {len(self._registered_tool_names)}"
            )

    def update_config(self, new_config: AstrBotConfig):
        """配置热更新入口（由框架 on_config_changed 钩子调用，若框架不支持则此方法为预留接口）"""
        self.config = new_config
        self._sync_llm_tools()

    # --------------------- 事件监听：自动插入形象 ---------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        conversation_id = _get_conversation_id(event)
        
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            try:
                avatar = await self.load_conversation_avatar(conversation_id)
                if avatar is None:
                    # 首次对话，文件不存在，创建默认形象
                    logger.info(f"[{conversation_id}] 首次对话，创建默认形象")
                    avatar = self._create_default_avatar(conversation_id)
                    await self.save_conversation_avatar(avatar)

                context_text = ""
                # 身体部分
                if avatar.current_body in avatar.bodies:
                    body = avatar.bodies[avatar.current_body]
                    body_prefix = self.config.get(
                        "body_prefix_prompt",
                        DEFAULT_BODY_PREFIX_PROMPT
                    )
                    body_suffix = self.config.get(
                        "body_suffix_prompt",
                        DEFAULT_BODY_SUFFIX_PROMPT
                    )
                    context_text += f"\n{body_prefix}\n"
                    context_text += f"当前身体方案：{avatar.current_body}\n简介：{body.description}\n"
                    for k, v in body.fields.items():
                        context_text += f"- {k}：{v}\n"
                    context_text += f"{body_suffix}\n"

                    # 可选身体方案列表
                    if len(avatar.bodies) > 1:
                        body_list_prefix = self.config.get(
                            "available_body_list_prefix",
                            DEFAULT_AVAILABLE_BODY_LIST_PREFIX
                        )
                        body_list_suffix = self.config.get(
                            "available_body_list_suffix",
                            DEFAULT_AVAILABLE_BODY_LIST_SUFFIX
                        )
                        context_text += f"{body_list_prefix}\n"
                        for name, b in avatar.bodies.items():
                            if name != avatar.current_body:
                                context_text += f"- {name}：{b.description}\n"
                        context_text += f"{body_list_suffix}\n"

                # 衣着部分
                if avatar.current_outfit in avatar.outfits:
                    outfit = avatar.outfits[avatar.current_outfit]
                    outfit_prefix = self.config.get(
                        "outfit_prefix_prompt",
                        DEFAULT_OUTFIT_PREFIX_PROMPT
                    )
                    outfit_suffix = self.config.get(
                        "outfit_suffix_prompt",
                        DEFAULT_OUTFIT_SUFFIX_PROMPT
                    )
                    context_text += f"\n{outfit_prefix}\n"
                    context_text += f"当前衣着：{avatar.current_outfit}\n简介：{outfit.description}\n"
                    for k, v in outfit.fields.items():
                        context_text += f"- {k}：{v}\n"
                    context_text += f"{outfit_suffix}\n"

                    # 可用衣着列表
                    if len(avatar.outfits) > 1:
                        list_prefix = self.config.get(
                            "available_outfit_list_prefix",
                            DEFAULT_AVAILABLE_OUTFIT_LIST_PREFIX
                        )
                        list_suffix = self.config.get(
                            "available_outfit_list_suffix",
                            DEFAULT_AVAILABLE_OUTFIT_LIST_SUFFIX
                        )
                        context_text += f"{list_prefix}\n"
                        for name, o in avatar.outfits.items():
                            if name != avatar.current_outfit:
                                context_text += f"- {name}：{o.description}\n"
                        context_text += f"{list_suffix}\n"

                # 插入到指定位置
                if context_text:
                    insert_pos = self.config.get("llm_insert_position", DEFAULT_LLM_INSERT_POSITION)
                    if insert_pos == "system_prompt_start":
                        req.system_prompt = context_text + (req.system_prompt or "")
                    elif insert_pos == "system_prompt_end":
                        req.system_prompt = (req.system_prompt or "") + context_text
                    elif insert_pos == "user_prompt_start":
                        req.prompt = context_text + (req.prompt or "")
                    elif insert_pos == "user_prompt_end":
                        req.prompt = (req.prompt or "") + context_text
                    else:
                        req.system_prompt = (req.system_prompt or "") + context_text

                    logger.info(f"[{conversation_id}] 形象已注入")
                    logger.debug(f"[{conversation_id}] 形象字段：\n{context_text}")

            except Exception as e:
                logger.error(f"[{conversation_id}] 形象注入失败: {e}")

    def _create_default_avatar(self, conversation_id: str) -> ConversationAvatar:
        """创建包含默认身体和两套衣着的形象"""
        # 默认身体（蓝长发、金瞳、星星发饰）
        default_body = BodySchema(
            description="标准体型，蓝色长发，金色瞳孔，佩戴星星发饰",
            fields={
                "发色": "蓝色长发",
                "瞳色": "金色",
                "发饰": "星星发饰",
                "身高": "165cm",
                "胸围": "B cup"
            }
        )
        # 默认衣着：常服
        normal_outfit = Outfit(
            description="日常校园通勤穿搭，正式得体",
            fields={
                "上衣": "白色衬衫+灰色马甲",
                "下着": "黑色百褶裙",
                "袜子": "白色裤袜",
                "鞋子": "棕色小皮鞋",
                "内衣": "蓝白条内衣",
                "内裤": "蓝白条内裤"
            }
        )
        # 居家服
        home_outfit = Outfit(
            description="舒适居家休闲穿搭，柔软亲肤",
            fields={
                "上衣": "白色纱质连衣裙",
                "内衣": "黑色蕾丝内衣",
                "内裤": "黑色蕾丝内裤"
            }
        )
        return ConversationAvatar(
            conversation_id=conversation_id,
            current_body=DEFAULT_BODY_NAME,
            bodies={DEFAULT_BODY_NAME: default_body},
            current_outfit=DEFAULT_OUTFIT_NAME,
            outfits={DEFAULT_OUTFIT_NAME: normal_outfit, "居家服": home_outfit}
        )

    def get_conversation_file_path(self, conversation_id: str) -> Path:
        return self.data_dir / f"{conversation_id}.json"

    async def load_conversation_avatar(self, conversation_id: str) -> Optional[ConversationAvatar]:
        file_path = self.get_conversation_file_path(conversation_id)
        if not file_path.exists():
            return None
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)

            # 旧数据迁移
            if "bodies" not in data:
                logger.info(f"[{conversation_id}] 迁移旧数据")
                default_body = BodySchema(
                    description="标准体型，蓝色长发，金色瞳孔，佩戴星星发饰",
                    fields={"发色": "蓝色长发", "瞳色": "金色", "发饰": "星星发饰",
                            "身高": "165cm", "胸围": "B cup"}
                )
                if isinstance(data.get("outfits"), list):
                    outfits_dict = {}
                    for o in data.get("outfits", []):
                        name = o.get("outfit_name", "未知")
                        desc = o.get("description", DEFAULT_DESCRIPTION)
                        fields = o.get("fields", {})
                        outfits_dict[name] = Outfit(description=desc, fields=fields)
                    current_outfit = data.get("current_outfit", DEFAULT_OUTFIT_NAME)
                else:
                    outfits_dict = {}
                    for name, od in data.get("outfits", {}).items():
                        outfits_dict[name] = Outfit(
                            description=od.get("description", DEFAULT_DESCRIPTION),
                            fields=od.get("fields", {})
                        )
                    current_outfit = data.get("current_outfit", DEFAULT_OUTFIT_NAME)
                # 校验当前衣着指针有效性
                if current_outfit not in outfits_dict:
                    if outfits_dict:
                        current_outfit = next(iter(outfits_dict.keys()))
                        logger.warning(
                            f"[{conversation_id}] 迁移时当前衣着无效，已修正为 {current_outfit}"
                        )
                    else:
                        current_outfit = DEFAULT_OUTFIT_NAME
                new_avatar = ConversationAvatar(
                    conversation_id=conversation_id,
                    current_body=DEFAULT_BODY_NAME,
                    bodies={DEFAULT_BODY_NAME: default_body},
                    current_outfit=current_outfit,
                    outfits=outfits_dict
                )
                await self.save_conversation_avatar(new_avatar)
                return new_avatar

            # 新结构加载
            bodies = {}
            for name, bd in data.get("bodies", {}).items():
                bodies[name] = BodySchema(
                    description=bd.get("description", DEFAULT_DESCRIPTION),
                    fields=bd.get("fields", {})
                )
            outfits = {}
            for name, od in data.get("outfits", {}).items():
                outfits[name] = Outfit(
                    description=od.get("description", DEFAULT_DESCRIPTION),
                    fields=od.get("fields", {})
                )
            avatar = ConversationAvatar(
                conversation_id=data.get("conversation_id", conversation_id),
                current_body=data.get("current_body", DEFAULT_BODY_NAME),
                bodies=bodies,
                current_outfit=data.get("current_outfit", DEFAULT_OUTFIT_NAME),
                outfits=outfits
            )

            # 自动修复数据完整性：若身体或衣着为空，补全默认数据
            need_repair = not avatar.bodies or not avatar.outfits
            if need_repair:
                logger.warning(f"[{conversation_id}] 形象数据不完整，自动修复")
                default = self._create_default_avatar(conversation_id)
                if avatar.bodies:
                    default.bodies = avatar.bodies
                    default.current_body = (
                        avatar.current_body
                        if avatar.current_body in avatar.bodies
                        else next(iter(avatar.bodies.keys()))
                    )
                if avatar.outfits:
                    default.outfits = avatar.outfits
                    default.current_outfit = (
                        avatar.current_outfit
                        if avatar.current_outfit in avatar.outfits
                        else next(iter(avatar.outfits.keys()))
                    )
                avatar = default
                await self.save_conversation_avatar(avatar)

            # 再次确保指针有效（修复后也检查）
            if avatar.current_body not in avatar.bodies:
                if avatar.bodies:
                    avatar.current_body = next(iter(avatar.bodies.keys()))
                else:
                    # 极端情况：bodies 被手动清空，重建默认
                    avatar = self._create_default_avatar(conversation_id)
                await self.save_conversation_avatar(avatar)
            if avatar.current_outfit not in avatar.outfits:
                if avatar.outfits:
                    avatar.current_outfit = next(iter(avatar.outfits.keys()))
                else:
                    avatar = self._create_default_avatar(conversation_id)
                await self.save_conversation_avatar(avatar)

            return avatar

        except Exception as e:
            logger.error(f"[{conversation_id}] 加载失败: {e}")
            try:
                # 使用时间戳+进程ID+随机数避免并发碰撞
                pid = os.getpid() if hasattr(os, 'getpid') else 0
                backup_path = file_path.with_suffix(f".json.bak.{int(time.time())}.{pid}.{os.urandom(4).hex()}")
                file_path.rename(backup_path)
                logger.warning(f"[{conversation_id}] 损坏文件已备份至 {backup_path}")
            except Exception as be:
                logger.error(f"[{conversation_id}] 备份损坏文件失败: {be}")
            return None

    async def save_conversation_avatar(self, avatar: ConversationAvatar):
        file_path = self.get_conversation_file_path(avatar.conversation_id)
        try:
            data = {
                "conversation_id": avatar.conversation_id,
                "current_body": avatar.current_body,
                "bodies": {name: asdict(b) for name, b in avatar.bodies.items()},
                "current_outfit": avatar.current_outfit,
                "outfits": {name: asdict(o) for name, o in avatar.outfits.items()}
            }
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[{avatar.conversation_id}] 保存形象数据失败: {e}")

    # --------------------- 管理员指令 ---------------------
    @filter.command("查看bot形象")
    async def view_avatar(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self.load_conversation_avatar(conversation_id)
            if not avatar:
                yield event.plain_result("❌ 当前会话无形象数据")
                return
            # 加载函数已保证数据完整，直接使用
            result = f"📝 会话ID：{conversation_id}\n\n"
            # 身体部分
            result += "🧬 当前身体方案：\n"
            body = avatar.bodies[avatar.current_body]
            result += f"  名称：{avatar.current_body}\n  简介：{body.description}\n  属性：\n"
            for k, v in body.fields.items():
                result += f"    - {k}：{v}\n"
            # 可用身体方案
            if len(avatar.bodies) > 1:
                result += "\n📋 其他身体方案：\n"
                for name, b in avatar.bodies.items():
                    if name != avatar.current_body:
                        result += f"  • {name}：{b.description}\n"
            # 衣着部分
            result += "\n👗 当前衣着方案：\n"
            outfit = avatar.outfits[avatar.current_outfit]
            result += f"  名称：{avatar.current_outfit}\n  简介：{outfit.description}\n  属性：\n"
            for k, v in outfit.fields.items():
                result += f"    - {k}：{v}\n"
            if len(avatar.outfits) > 1:
                result += "\n📋 其他衣着方案：\n"
                for name, o in avatar.outfits.items():
                    if name != avatar.current_outfit:
                        result += f"  • {name}：{o.description}\n"
            yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换身体方案")
    async def switch_body_admin(self, event: AstrMessageEvent, body_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self.load_conversation_avatar(conversation_id)
            if not avatar:
                yield event.plain_result("❌ 当前会话无形象数据")
                return
            if body_name not in avatar.bodies:
                yield event.plain_result(f"❌ 身体方案 [{body_name}] 不存在")
                return
            avatar.current_body = body_name
            await self.save_conversation_avatar(avatar)
            yield event.plain_result(f"✅ 已切换身体方案为 【{body_name}】")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换bot形象")
    async def switch_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = await self.load_conversation_avatar(conversation_id)
            if not avatar:
                yield event.plain_result("❌ 当前会话无形象数据")
                return
            if outfit_name not in avatar.outfits:
                yield event.plain_result(f"❌ 衣着方案 [{outfit_name}] 不存在")
                return
            avatar.current_outfit = outfit_name
            await self.save_conversation_avatar(avatar)
            yield event.plain_result(f"✅ 已切换衣着为 【{outfit_name}】")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清空当前对话形象")
    async def clear_conversation(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        file_path = self.get_conversation_file_path(conversation_id)
        if file_path.exists():
            file_path.unlink()
            await _lock_manager.cleanup(conversation_id)
            yield event.plain_result(f"✅ 已清空会话 [{conversation_id}] 的所有形象数据")
        else:
            yield event.plain_result("❌ 当前会话无形象数据")

    async def terminate(self):
        await _lock_manager.close()
        logger.info("BotAvatarManager 已卸载")