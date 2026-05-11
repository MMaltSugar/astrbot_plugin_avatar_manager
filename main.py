import json
import os
import asyncio
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Any
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api import FunctionTool

# ===================== 新数据模型 =====================
@dataclass
class BodySchema:
    """身体方案（如：正常体型、Q版）"""
    description: str = field(default="无简介")
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."

@dataclass
class Outfit:
    """衣着方案（如：常服、泳装）"""
    description: str = field(default="无简介")
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."

@dataclass
class ConversationAvatar:
    """对话级完整形象数据"""
    conversation_id: str
    current_body: str = "默认身体"
    bodies: Dict[str, BodySchema] = field(default_factory=dict)
    current_outfit: str = "常服"
    outfits: Dict[str, Outfit] = field(default_factory=dict)

# ===================== 会话ID获取 =====================
def _get_conversation_id(event: AstrMessageEvent) -> str:
    """获取稳定会话ID，失败时使用固定兜底"""
    def _sid_from_event(ev: AstrMessageEvent) -> Optional[str]:
        if ev is None:
            return None
        if hasattr(ev, "session_id"):
            sid = getattr(ev, "session_id", None)
            if sid:
                return str(sid)
        if hasattr(ev, "get_session_id"):
            sid = ev.get_session_id()
            if sid:
                return str(sid)
        if hasattr(ev, "message_obj") and hasattr(ev.message_obj, "session_id"):
            return str(ev.message_obj.session_id)
        return None

    sid = _sid_from_event(event)
    if sid:
        safe_sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)
        safe_sid = safe_sid.replace(":", "_")
        logger.debug(f"会话ID: {safe_sid}")
        return safe_sid
    logger.warning("无法获取会话ID，使用固定兜底: default_conversation")
    return "default_conversation"

# ===================== 会话级锁管理器 =====================
class ConversationLockManager:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def get_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._global_lock:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = asyncio.Lock()
            return self._locks[conversation_id]

    async def cleanup(self, conversation_id: str):
        async with self._global_lock:
            self._locks.pop(conversation_id, None)

_lock_manager = ConversationLockManager()

# ===================== 辅助函数：字段过滤 =====================
def filter_fields(fields: Dict[str, str], plugin, is_outfit: bool = True) -> Dict[str, str]:
    """根据配置过滤不允许的字段"""
    allow_custom = plugin.config.get("allow_custom_fields", True)
    if allow_custom:
        return fields
    allowed_str = plugin.config.get("allowed_fields", "")
    allowed = [f.strip() for f in allowed_str.split(",") if f.strip()]
    if not allowed:
        return fields
    # 衣着和身体共用一个词条白名单
    return {k: v for k, v in fields.items() if k in allowed}

# ===================== LLM工具：身体方案操作 =====================
@dataclass
class CreateBodySchemaTool(FunctionTool):
    name: str = "create_body_schema"
    description: str = "创建/覆盖身体方案（发色、瞳色、身高、胸围等）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "schema_name": {"type": "string", "description": "身体方案名称，如「正常体型」"},
            "description": {"type": "string", "description": "50字内简介"},
            "fields": {"type": "object", "description": "身体词条，如：{\"发色\":\"蓝色长发\",\"瞳色\":\"金色\"}"}
        },
        "required": ["schema_name", "fields"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str, fields: Dict[str, str], description: str = "无简介"):
        if not self.plugin_instance.config.get("allow_llm_modify_body", False):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            fields = filter_fields(fields, self.plugin_instance, is_outfit=False)
            body_schema = BodySchema(description=description, fields=fields)
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar:
                avatar = ConversationAvatar(conversation_id=conversation_id)
            avatar.bodies[schema_name] = body_schema
            if len(avatar.bodies) == 1:
                avatar.current_body = schema_name
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 身体方案 [{schema_name}] 已保存\n简介：{description}\n词条：{fields}")
            return f"✅ 身体方案 [{schema_name}] 已保存\n简介：{description}\n词条：{fields}"

@dataclass
class SelectBodySchemaTool(FunctionTool):
    name: str = "select_body_schema"
    description: str = "切换当前使用的身体方案"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"schema_name": {"type": "string", "description": "身体方案名称"}},
        "required": ["schema_name"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str):
        if not self.plugin_instance.config.get("allow_llm_switch_body", True):
            return "❌ 管理员已禁止LLM切换身体方案"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.debug(f"❌ 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            avatar.current_body = schema_name
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 当前身体方案已切换为 [{schema_name}]")
            return f"✅ 当前身体方案已切换为 [{schema_name}]"

@dataclass
class ModifyBodyFieldTool(FunctionTool):
    name: str = "modify_body_field"
    description: str = "修改身体方案的单个词条或简介（仅1-3条修改）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "schema_name": {"type": "string", "description": "身体方案名称"},
            "field_name": {"type": "string", "description": "词条名或description"},
            "field_value": {"type": "string", "description": "新值"}
        },
        "required": ["schema_name", "field_name", "field_value"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str, field_name: str, field_value: str):
        if not self.plugin_instance.config.get("allow_llm_modify_body", False):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.debug(f"❌ 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            if field_name == "description":
                if len(field_value) > 50:
                    field_value = field_value[:47] + "..."
                avatar.bodies[schema_name].description = field_value
                self.plugin_instance.save_conversation_avatar(avatar)
                logger.info(f"✅ 身体方案 [{schema_name}] 简介已更新：{field_value}")
                return f"✅ 身体方案 [{schema_name}] 简介已更新：{field_value}"
            # 字段过滤
            filtered = {field_name: field_value}
            filtered = filter_fields(filtered, self.plugin_instance, is_outfit=False)
            if field_name not in filtered:
                logger.debug(f"❌ 词条 [{field_name}] 不在允许列表中，请联系管理员")
                return f"❌ 词条 [{field_name}] 不在允许列表中，请联系管理员"
            avatar.bodies[schema_name].fields[field_name] = field_value
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 身体词条已修改：{field_name} → {field_value}")
            return f"✅ 身体词条已修改：{field_name} → {field_value}"

@dataclass
class DeleteBodySchemaTool(FunctionTool):
    name: str = "delete_body_schema"
    description: str = "删除身体方案（不能删除当前使用的）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"schema_name": {"type": "string", "description": "身体方案名称"}},
        "required": ["schema_name"]
    })

    async def run(self, event: AstrMessageEvent, schema_name: str):
        if not self.plugin_instance.config.get("allow_llm_modify_body", False):
            return "❌ 管理员已禁止LLM修改身体数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or schema_name not in avatar.bodies:
                logger.debug(f"❌ 身体方案 [{schema_name}] 不存在")
                return f"❌ 身体方案 [{schema_name}] 不存在"
            if avatar.current_body == schema_name:
                logger.debug(f"❌ 不能删除当前正在使用的身体方案，请先切换")
                return f"❌ 不能删除当前正在使用的身体方案，请先切换"
            del avatar.bodies[schema_name]
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 身体方案 [{schema_name}] 已删除")
            return f"✅ 身体方案 [{schema_name}] 已删除"

# ===================== LLM工具：衣着方案操作（沿用原逻辑但适配新模型） =====================
@dataclass
class CreateOutfitTool(FunctionTool):
    name: str = "create_avatar_outfit"
    description: str = "创建/覆盖衣着方案（上衣、下着、鞋子等）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "衣着名称"},
            "description": {"type": "string", "description": "50字内简介"},
            "fields": {"type": "object", "description": "衣着词条"}
        },
        "required": ["outfit_name", "fields"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, fields: Dict[str, str], description: str = "无简介"):
        if not self.plugin_instance.config.get("allow_llm_modify_outfit", True):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            fields = filter_fields(fields, self.plugin_instance, is_outfit=True)
            outfit = Outfit(description=description, fields=fields)
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar:
                avatar = ConversationAvatar(conversation_id=conversation_id)
            avatar.outfits[outfit_name] = outfit
            if len(avatar.outfits) == 1:
                avatar.current_outfit = outfit_name
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 衣着方案 [{outfit_name}] 已保存\n简介：{description}\n词条：{fields}")
            return f"✅ 衣着方案 [{outfit_name}] 已保存\n简介：{description}\n词条：{fields}"

@dataclass
class SelectOutfitTool(FunctionTool):
    name: str = "select_avatar_outfit"
    description: str = "切换当前衣着方案"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "衣着名称"}},
        "required": ["outfit_name"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        if not self.plugin_instance.config.get("allow_llm_switch_outfit", True):
            return "❌ 管理员已禁止LLM切换衣着方案"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.debug(f"❌ 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            avatar.current_outfit = outfit_name
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 当前衣着已切换为 [{outfit_name}]")
            return f"✅ 当前衣着已切换为 [{outfit_name}]"

@dataclass
class ModifyOutfitFieldTool(FunctionTool):
    name: str = "modify_avatar_field"
    description: str = "修改衣着方案的单个词条或简介（1-3条）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "衣着名称"},
            "field_name": {"type": "string", "description": "词条名或description"},
            "field_value": {"type": "string", "description": "新值"}
        },
        "required": ["outfit_name", "field_name", "field_value"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, field_name: str, field_value: str):
        if not self.plugin_instance.config.get("allow_llm_modify_outfit", True):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.debug(f"❌ 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            if field_name == "description":
                if len(field_value) > 50:
                    field_value = field_value[:47] + "..."
                avatar.outfits[outfit_name].description = field_value
                self.plugin_instance.save_conversation_avatar(avatar)
                logger.info(f"✅ 衣着 [{outfit_name}] 简介已更新：{field_value}")
                return f"✅ 衣着 [{outfit_name}] 简介已更新：{field_value}"
            filtered = {field_name: field_value}
            filtered = filter_fields(filtered, self.plugin_instance, is_outfit=True)
            if field_name not in filtered:
                logger.debug(f"❌ 词条 [{field_name}] 不在允许列表中")
                return f"❌ 词条 [{field_name}] 不在允许列表中"
            avatar.outfits[outfit_name].fields[field_name] = field_value
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 衣着词条已修改：{field_name} → {field_value}")
            return f"✅ 衣着词条已修改：{field_name} → {field_value}"

@dataclass
class DeleteOutfitTool(FunctionTool):
    name: str = "delete_avatar_outfit"
    description: str = "删除衣着方案（不能删除当前使用的）"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "衣着名称"}},
        "required": ["outfit_name"]
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        if not self.plugin_instance.config.get("allow_llm_modify_outfit", True):
            return "❌ 管理员已禁止LLM修改衣着数据"
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                logger.debug(f"❌ 衣着方案 [{outfit_name}] 不存在")
                return f"❌ 衣着方案 [{outfit_name}] 不存在"
            if avatar.current_outfit == outfit_name:
                logger.debug(f"❌ 不能删除当前衣着，请先切换")
                return f"❌ 不能删除当前衣着，请先切换"
            del avatar.outfits[outfit_name]
            self.plugin_instance.save_conversation_avatar(avatar)
            logger.info(f"✅ 衣着 [{outfit_name}] 已删除")
            return f"✅ 衣着 [{outfit_name}] 已删除"

# ===================== 插件主类 =====================
class BotAvatarManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 注册LLM工具（根据配置动态添加）
        tools = []
        # 身体相关工具
        if self.config.get("allow_llm_modify_body", False):
            tools.extend([CreateBodySchemaTool(plugin_instance=self),
                          ModifyBodyFieldTool(plugin_instance=self),
                          DeleteBodySchemaTool(plugin_instance=self)])
        if self.config.get("allow_llm_switch_body", True):
            tools.append(SelectBodySchemaTool(plugin_instance=self))
        # 衣着相关工具
        if self.config.get("allow_llm_modify_outfit", True):
            tools.extend([CreateOutfitTool(plugin_instance=self),
                          ModifyOutfitFieldTool(plugin_instance=self),
                          DeleteOutfitTool(plugin_instance=self)])
        if self.config.get("allow_llm_switch_outfit", True):
            tools.append(SelectOutfitTool(plugin_instance=self))

        self.context.add_llm_tools(*tools)
        logger.info("BotAvatarManager 初始化完成，工具注册数量: %d", len(tools))

    # --------------------- 事件监听：自动插入形象 ---------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.load_conversation_avatar(conversation_id)

            # 无数据则创建默认
            if not avatar or not avatar.bodies:
                avatar = self._create_default_avatar(conversation_id)
                self.save_conversation_avatar(avatar)

            # 修复无效指针
            if avatar.current_body not in avatar.bodies and avatar.bodies:
                avatar.current_body = next(iter(avatar.bodies.keys()))
                self.save_conversation_avatar(avatar)
            if avatar.current_outfit not in avatar.outfits and avatar.outfits:
                avatar.current_outfit = next(iter(avatar.outfits.keys()))
                self.save_conversation_avatar(avatar)

            # 构建上下文文本
            context_text = ""
            # 身体部分
            body = avatar.bodies[avatar.current_body]
            body_prefix = self.config.get("body_prefix_prompt", "【你当前的身体形象（必须严格遵守）】")
            body_suffix = self.config.get("body_suffix_prompt", "【身体形象设定结束】")
            context_text += f"\n{body_prefix}\n"
            context_text += f"当前身体方案：{avatar.current_body}\n简介：{body.description}\n"
            for k, v in body.fields.items():
                context_text += f"- {k}：{v}\n"
            context_text += f"{body_suffix}\n"

            # 可选身体方案列表（简单格式）
            if len(avatar.bodies) > 1:
                context_text += "\n【可选身体方案】\n"
                for name, b in avatar.bodies.items():
                    if name != avatar.current_body:
                        context_text += f"- {name}：{b.description}\n"
                context_text += "【可选身体方案结束】\n"

            # 衣着部分
            outfit = avatar.outfits[avatar.current_outfit]
            outfit_prefix = self.config.get("outfit_prefix_prompt", "【你当前的衣着形象（必须严格遵守）】")
            outfit_suffix = self.config.get("outfit_suffix_prompt", "【衣着形象设定结束】")
            context_text += f"\n{outfit_prefix}\n"
            context_text += f"当前衣着：{avatar.current_outfit}\n简介：{outfit.description}\n"
            for k, v in outfit.fields.items():
                context_text += f"- {k}：{v}\n"
            context_text += f"{outfit_suffix}\n"

            # 可用衣着列表（支持前后缀）
            if len(avatar.outfits) > 1:
                list_prefix = self.config.get("available_outfit_list_prefix", "\n【可用衣着列表（可自主切换）】")
                list_suffix = self.config.get("available_outfit_list_suffix", "【可用衣着列表结束】")
                context_text += f"{list_prefix}\n"
                for name, o in avatar.outfits.items():
                    if name != avatar.current_outfit:
                        context_text += f"- {name}：{o.description}\n"
                context_text += f"{list_suffix}\n"

            # 插入到指定位置
            insert_pos = self.config.get("llm_insert_position", "system_prompt_end")
            if insert_pos == "system_prompt_start":
                req.system_prompt = context_text + req.system_prompt
            elif insert_pos == "system_prompt_end":
                req.system_prompt += context_text
            elif insert_pos == "user_prompt_start":
                req.prompt = context_text + req.prompt
            elif insert_pos == "user_prompt_end":
                req.prompt += context_text
            else:
                req.system_prompt += context_text

            logger.debug(f"会话 {conversation_id} 形象已注入")

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
            current_body="默认身体",
            bodies={"默认身体": default_body},
            current_outfit="常服",
            outfits={"常服": normal_outfit, "居家服": home_outfit}
        )

    # --------------------- 数据读写（兼容旧版迁移） ---------------------
    def get_conversation_file_path(self, conversation_id: str) -> str:
        return str(self.data_dir / f"{conversation_id}.json")

    def load_conversation_avatar(self, conversation_id: str) -> Optional[ConversationAvatar]:
        file_path = self.get_conversation_file_path(conversation_id)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 旧数据迁移（无bodies字段）
            if "bodies" not in data:
                logger.info(f"迁移旧数据：{conversation_id}")
                # 旧outfits可能是list或dict
                if isinstance(data.get("outfits"), list):
                    outfits_dict = {}
                    for o in data.get("outfits", []):
                        name = o.get("outfit_name", "未知")
                        desc = o.get("description", "无简介")
                        fields = o.get("fields", {})
                        outfits_dict[name] = Outfit(description=desc, fields=fields)
                    current_outfit = data.get("current_outfit", "常服")
                else:
                    outfits_dict = {}
                    for name, od in data.get("outfits", {}).items():
                        outfits_dict[name] = Outfit(
                            description=od.get("description", "无简介"),
                            fields=od.get("fields", {})
                        )
                    current_outfit = data.get("current_outfit", "常服")
                # 创建默认身体
                default_body = BodySchema(
                    description="标准体型，蓝色长发，金色瞳孔，佩戴星星发饰",
                    fields={"发色": "蓝色长发", "瞳色": "金色", "发饰": "星星发饰", "身高": "165cm", "胸围": "B cup"}
                )
                new_avatar = ConversationAvatar(
                    conversation_id=conversation_id,
                    current_body="默认身体",
                    bodies={"默认身体": default_body},
                    current_outfit=current_outfit,
                    outfits=outfits_dict
                )
                self.save_conversation_avatar(new_avatar)
                return new_avatar

            # 新结构加载
            bodies = {}
            for name, bd in data.get("bodies", {}).items():
                bodies[name] = BodySchema(
                    description=bd.get("description", "无简介"),
                    fields=bd.get("fields", {})
                )
            outfits = {}
            for name, od in data.get("outfits", {}).items():
                outfits[name] = Outfit(
                    description=od.get("description", "无简介"),
                    fields=od.get("fields", {})
                )
            return ConversationAvatar(
                conversation_id=data.get("conversation_id", conversation_id),
                current_body=data.get("current_body", "默认身体"),
                bodies=bodies,
                current_outfit=data.get("current_outfit", "常服"),
                outfits=outfits
            )
        except Exception as e:
            logger.error(f"加载会话 {conversation_id} 失败: {e}")
            try:
                backup = f"{file_path}.bak.{os.urandom(4).hex()}"
                os.rename(file_path, backup)
                logger.warning(f"损坏文件已备份至 {backup}")
            except Exception:
                pass
            return None

    def save_conversation_avatar(self, avatar: ConversationAvatar):
        file_path = self.get_conversation_file_path(avatar.conversation_id)
        try:
            data = {
                "conversation_id": avatar.conversation_id,
                "current_body": avatar.current_body,
                "bodies": {name: asdict(b) for name, b in avatar.bodies.items()},
                "current_outfit": avatar.current_outfit,
                "outfits": {name: asdict(o) for name, o in avatar.outfits.items()}
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存失败: {e}")

    # --------------------- 管理员指令（部分保留并适配） ---------------------
    @filter.command("查看bot形象")
    async def view_avatar(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.load_conversation_avatar(conversation_id)
            if not avatar:
                yield event.plain_result("❌ 当前会话无形象数据")
                return
            # 确保指针有效
            if avatar.current_body not in avatar.bodies and avatar.bodies:
                avatar.current_body = next(iter(avatar.bodies.keys()))
                self.save_conversation_avatar(avatar)
            if avatar.current_outfit not in avatar.outfits and avatar.outfits:
                avatar.current_outfit = next(iter(avatar.outfits.keys()))
                self.save_conversation_avatar(avatar)

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

    @filter.command("切换身体方案")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_body_admin(self, event: AstrMessageEvent, body_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.load_conversation_avatar(conversation_id)
            if not avatar or body_name not in avatar.bodies:
                yield event.plain_result(f"❌ 身体方案 [{body_name}] 不存在")
                return
            avatar.current_body = body_name
            self.save_conversation_avatar(avatar)
            yield event.plain_result(f"✅ 已切换身体方案为 【{body_name}】")

    @filter.command("切换bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar = self.load_conversation_avatar(conversation_id)
            if not avatar or outfit_name not in avatar.outfits:
                yield event.plain_result(f"❌ 衣着方案 [{outfit_name}] 不存在")
                return
            avatar.current_outfit = outfit_name
            self.save_conversation_avatar(avatar)
            yield event.plain_result(f"✅ 已切换衣着为 【{outfit_name}】")

    @filter.command("清空当前对话形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_conversation(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        file_path = self.get_conversation_file_path(conversation_id)
        if os.path.exists(file_path):
            os.remove(file_path)
            await _lock_manager.cleanup(conversation_id)
            yield event.plain_result(f"✅ 已清空会话 [{conversation_id}] 的所有形象数据")
        else:
            yield event.plain_result("❌ 当前会话无形象数据")

    async def terminate(self):
        logger.info("BotAvatarManager 已卸载")