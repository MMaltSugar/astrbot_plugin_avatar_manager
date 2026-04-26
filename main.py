import json
import os
import asyncio
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Any, cast
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api import FunctionTool

# ===================== 数据模型 =====================
@dataclass
class AvatarOutfit:
    description: str = field(default="无简介")
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."

@dataclass
class ConversationAvatar:
    conversation_id: str
    current_outfit: str = "常服"
    outfits: Dict[str, AvatarOutfit] = field(default_factory=dict)

# ===================== 会话ID获取（稳定兜底） =====================
def _get_conversation_id(event: AstrMessageEvent) -> str:
    """获取会话唯一ID，取不到时使用固定兜底值，避免数据漂移"""
    def _sid_from_event(ev: AstrMessageEvent) -> Optional[str]:
        if ev is None:
            return None
        if hasattr(ev, "session_id"):
            sid = getattr(ev, "session_id", None)
            if sid is not None:
                return str(sid)
        if hasattr(ev, "get_session_id"):
            sid = ev.get_session_id()
            if sid is not None:
                return str(sid)
        if hasattr(ev, "message_obj"):
            mobj = getattr(ev, "message_obj", None)
            if mobj and hasattr(mobj, "session_id"):
                return str(getattr(mobj, "session_id"))
        if hasattr(ev, "unified_msg_origin"):
            umo = getattr(ev, "unified_msg_origin", None)
            if umo:
                return str(umo)
        return None

    sid = _sid_from_event(event)
    if sid:
        # 清理非法文件名字符，Windows下不允许 : \ / * ? " < > |
        safe_sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)
        # 额外处理冒号（常见于某些会话ID）
        safe_sid = safe_sid.replace(":", "_")
        logger.debug(f"获取到会话ID: {safe_sid}")
        return safe_sid

    logger.warning("无法获取会话ID，使用固定兜底ID: default_conversation")
    return "default_conversation"

# ===================== 会话级异步锁管理 =====================
class ConversationLockManager:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def get_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._lock:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = asyncio.Lock()
            return self._locks[conversation_id]

    async def cleanup(self, conversation_id: str):
        async with self._lock:
            self._locks.pop(conversation_id, None)

# 全局锁管理器实例（在插件中持有）
_lock_manager = ConversationLockManager()

# ===================== LLM工具定义 =====================
@dataclass
class CreateAvatarOutfitTool(FunctionTool):
    name: str = "create_avatar_outfit"
    description: str = "创建/覆盖形象列表中的指定着装，支持自定义词条和简介。【规则】：修改4条及以上词条，直接调用本工具覆写对应着装"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "着装名称"},
            "description": {"type": "string", "description": "50字内的着装简介"},
            "fields": {"type": "object", "description": "形象词条键值对"},
        },
        "required": ["outfit_name", "fields"],
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, fields: Dict[str, str], description: str = "无简介"):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            # 安全读取配置
            avatar_fields_str = self.plugin_instance.config.get("avatar_fields", "")
            config_fields = [f.strip() for f in avatar_fields_str.split(",") if f.strip()]
            allow_custom = self.plugin_instance.config.get("allow_custom_fields", True)
            if not allow_custom and config_fields:
                fields = {k: v for k, v in fields.items() if k in config_fields}

            outfit = AvatarOutfit(description=description, fields=fields)
            self.plugin_instance.save_outfit_to_list(conversation_id, outfit_name, outfit)

            # 首次创建自动设为当前形象（当只有这一套时）
            avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
            if avatar_data and len(avatar_data.outfits) == 1:
                avatar_data.current_outfit = outfit_name
                self.plugin_instance.save_conversation_avatar(avatar_data)

            return f"✅ 成功创建/覆盖[{outfit_name}]\n简介：{outfit.description}\n形象词条：{fields}"

@dataclass
class SelectAvatarOutfitTool(FunctionTool):
    name: str = "select_avatar_outfit"
    description: str = "切换当前形象"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "要切换的着装名称"}},
        "required": ["outfit_name"],
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar_data:
                return f"❌ 当前会话无形象数据"
            if outfit_name not in avatar_data.outfits:
                return f"❌ 形象列表中无[{outfit_name}]，可用：{list(avatar_data.outfits.keys())}"

            avatar_data.current_outfit = outfit_name
            self.plugin_instance.save_conversation_avatar(avatar_data)
            current = avatar_data.outfits[outfit_name]
            return f"✅ 切换当前形象为[{outfit_name}]\n简介：{current.description}\n词条：{current.fields}"

@dataclass
class ModifyAvatarFieldTool(FunctionTool):
    name: str = "modify_avatar_field"
    description: str = "修改指定着装的单个词条或简介。仅用于1-3条修改，批量请用create_avatar_outfit"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "outfit_name": {"type": "string", "description": "着装名称"},
            "field_name": {"type": "string", "description": "词条名，修改简介填「description」"},
            "field_value": {"type": "string", "description": "新值（简介限50字）"},
        },
        "required": ["outfit_name", "field_name", "field_value"],
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str, field_name: str, field_value: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar_data or outfit_name not in avatar_data.outfits:
                return f"❌ 形象[{outfit_name}]不存在"

            # 修改简介
            if field_name == "description":
                if len(field_value) > 50:
                    field_value = field_value[:47] + "..."
                avatar_data.outfits[outfit_name].description = field_value
                self.plugin_instance.save_conversation_avatar(avatar_data)
                return f"✅ 修改简介成功：{field_value}"

            # 修改词条：检查自定义字段权限
            allow_custom = self.plugin_instance.config.get("allow_custom_fields", True)
            if not allow_custom:
                avatar_fields_str = self.plugin_instance.config.get("avatar_fields", "")
                allowed_fields = [f.strip() for f in avatar_fields_str.split(",") if f.strip()]
                if field_name not in allowed_fields:
                    return f"❌ 不允许创建自定义词条「{field_name}」，请在配置中开启或使用已有词条：{allowed_fields}"

            avatar_data.outfits[outfit_name].fields[field_name] = field_value
            self.plugin_instance.save_conversation_avatar(avatar_data)
            return f"✅ 修改词条成功：{field_name} → {field_value}"

@dataclass
class DeleteAvatarOutfitTool(FunctionTool):
    name: str = "delete_avatar_outfit"
    description: str = "从形象列表中删除指定着装，不能删除当前使用的形象"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"outfit_name": {"type": "string", "description": "要删除的着装名称"}},
        "required": ["outfit_name"],
    })

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
            if not avatar_data or outfit_name not in avatar_data.outfits:
                return f"❌ 形象[{outfit_name}]不存在"
            if avatar_data.current_outfit == outfit_name:
                return f"❌ 无法删除当前使用的形象[{outfit_name}]，请先切换"

            del avatar_data.outfits[outfit_name]
            self.plugin_instance.save_conversation_avatar(avatar_data)
            return f"✅ 删除[{outfit_name}]成功，剩余形象：{list(avatar_data.outfits.keys())}"

# ===================== 插件主类 =====================
class BotAvatarManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.context.add_llm_tools(
            CreateAvatarOutfitTool(plugin_instance=self),
            SelectAvatarOutfitTool(plugin_instance=self),
            ModifyAvatarFieldTool(plugin_instance=self),
            DeleteAvatarOutfitTool(plugin_instance=self),
        )
        logger.info("=====[BotAvatarManager] initialized =====")

    # --------------------- 事件监听：自动插入形象 ---------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        logger.info("监听到LLM请求，准备注入形象数据")
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.load_conversation_avatar(conversation_id)

            # 无形象则创建默认两套
            if not avatar_data or len(avatar_data.outfits) == 0:
                normal = AvatarOutfit(
                    description="日常校园通勤穿搭，正式得体",
                    fields={
                        "上衣": "白色衬衫+灰色马甲",
                        "下着": "黑色百褶裙",
                        "袜子": "白色裤袜",
                        "鞋子": "棕色小皮鞋",
                        "内衣": "蓝白条内衣",
                        "内裤": "蓝白条内裤",
                    }
                )
                home = AvatarOutfit(
                    description="舒适居家休闲穿搭，柔软亲肤",
                    fields={
                        "上衣": "白色纱质连衣裙",
                        "内衣": "黑色蕾丝内衣",
                        "内裤": "黑色蕾丝内裤",
                    }
                )
                # 直接保存到outfits
                if not avatar_data:
                    avatar_data = ConversationAvatar(conversation_id=conversation_id)
                avatar_data.outfits["常服"] = normal
                avatar_data.outfits["居家服"] = home
                avatar_data.current_outfit = "常服"
                self.save_conversation_avatar(avatar_data)
                logger.info(f"会话[{conversation_id}]创建默认形象：常服+居家服")

            # 若当前形象指针失效，自动修复
            if avatar_data.current_outfit not in avatar_data.outfits:
                if avatar_data.outfits:
                    new_current = next(iter(avatar_data.outfits.keys()))
                    logger.warning(f"当前形象指针失效，自动切换到 {new_current}")
                    avatar_data.current_outfit = new_current
                    self.save_conversation_avatar(avatar_data)
                else:
                    logger.warning(f"会话[{conversation_id}]无任何形象，跳过注入")
                    return

            # 构建形象文本
            current = avatar_data.outfits[avatar_data.current_outfit]
            avatar_text = f"\n【你当前的形象设定（必须严格遵守）】\n会话ID：{conversation_id}\n当前形象：{avatar_data.current_outfit}\n简介：{current.description}\n形象属性：\n"
            for f, v in current.fields.items():
                avatar_text += f"- {f}：{v}\n"
            avatar_text += "【当前形象设定结束】\n\n【可用形象列表（可根据场景自主切换）】\n"
            for name, outfit in avatar_data.outfits.items():
                avatar_text += f"- {name}：{outfit.description}\n"
            avatar_text += "【可用形象列表结束】\n"

            # 按配置插入
            insert_pos = self.config.get("llm_insert_position", "system_prompt_end")
            if insert_pos == "system_prompt_start":
                req.system_prompt = avatar_text + req.system_prompt
            elif insert_pos == "system_prompt_end":
                req.system_prompt += avatar_text
            elif insert_pos == "user_prompt_start":
                req.prompt = avatar_text + req.prompt
            elif insert_pos == "user_prompt_end":
                req.prompt += avatar_text
            else:
                req.system_prompt += avatar_text  # 默认结尾

            logger.info(f"会话[{conversation_id}]形象数据已注入LLM上下文")

    # --------------------- 管理员指令 ---------------------
    @filter.command("查看bot形象")
    async def view_avatar(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.load_conversation_avatar(conversation_id)
            if not avatar_data or not avatar_data.outfits:
                yield event.plain_result("❌ 当前会话无形象数据")
                return

            # 确保 current_outfit 有效
            if avatar_data.current_outfit not in avatar_data.outfits:
                if avatar_data.outfits:
                    avatar_data.current_outfit = next(iter(avatar_data.outfits.keys()))
                    self.save_conversation_avatar(avatar_data)
                else:
                    yield event.plain_result("❌ 形象列表为空")
                    return

            reply = f"📝 会话 Bot 形象信息\n会话ID：{conversation_id}\n\n▶️ 当前形象：{avatar_data.current_outfit}\n"
            cur = avatar_data.outfits[avatar_data.current_outfit]
            reply += f"简介：{cur.description}\n形象属性：\n"
            for k, v in cur.fields.items():
                reply += f"- {k}：{v}\n"
            reply += f"\n📋 完整形象列表（共{len(avatar_data.outfits)}套）：\n"
            for name, outfit in avatar_data.outfits.items():
                reply += f"\n├─ 【{name}】（简介：{outfit.description}）\n"
                for k, v in outfit.fields.items():
                    reply += f"│  └─ {k}：{v}\n"
            yield event.plain_result(reply)

    @filter.command("创建bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def create_outfit_admin(self, event: AstrMessageEvent, outfit_name: str, description: str = "无简介", *args):
        conversation_id = _get_conversation_id(event)
        # 解析字段 k=v
        fields = {}
        for arg in args:
            if "=" in arg:
                k, v = arg.split("=", 1)
                fields[k.strip()] = v.strip()

        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            # 权限过滤
            allow_custom = self.config.get("allow_custom_fields", True)
            config_fields_str = self.config.get("avatar_fields", "")
            allowed = [f.strip() for f in config_fields_str.split(",") if f.strip()]
            if not allow_custom and allowed:
                fields = {k: v for k, v in fields.items() if k in allowed}

            outfit = AvatarOutfit(description=description, fields=fields)
            self.save_outfit_to_list(conversation_id, outfit_name, outfit)
            yield event.plain_result(f"✅ 创建形象 [{outfit_name}] 成功\n简介：{description}\n词条：{fields}")

    @filter.command("切换bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.load_conversation_avatar(conversation_id)
            if not avatar_data or outfit_name not in avatar_data.outfits:
                yield event.plain_result(f"❌ 形象 [{outfit_name}] 不存在")
                return
            avatar_data.current_outfit = outfit_name
            self.save_conversation_avatar(avatar_data)
            yield event.plain_result(f"✅ 已切换当前形象为【{outfit_name}】")

    @filter.command("删除bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            avatar_data = self.load_conversation_avatar(conversation_id)
            if not avatar_data or outfit_name not in avatar_data.outfits:
                yield event.plain_result(f"❌ 形象 [{outfit_name}] 不存在")
                return
            if avatar_data.current_outfit == outfit_name:
                yield event.plain_result("❌ 无法删除当前使用的形象，请先切换")
                return
            del avatar_data.outfits[outfit_name]
            self.save_conversation_avatar(avatar_data)
            yield event.plain_result(f"✅ 删除形象 [{outfit_name}] 成功，剩余：{list(avatar_data.outfits.keys())}")

    @filter.command("清空当前对话形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_conversation_avatar(self, event: AstrMessageEvent):
        conversation_id = _get_conversation_id(event)
        lock = await _lock_manager.get_lock(conversation_id)
        async with lock:
            file_path = self.get_conversation_file_path(conversation_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                yield event.plain_result(f"✅ 已清空会话 [{conversation_id}] 的所有形象数据")
            else:
                yield event.plain_result("❌ 当前会话无形象数据")
            await _lock_manager.cleanup(conversation_id)

    # --------------------- 数据读写方法 ---------------------
    def get_conversation_file_path(self, conversation_id: str) -> str:
        return str(self.data_dir / f"{conversation_id}.json")

    def load_conversation_avatar(self, conversation_id: str) -> Optional[ConversationAvatar]:
        file_path = self.get_conversation_file_path(conversation_id)
        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 旧数据迁移（outfits为list时）
            if isinstance(data.get("outfits"), list):
                logger.info(f"迁移旧数据：{conversation_id}")
                new_outfits = {}
                current = "常服"
                for o in data.get("outfits", []):
                    name = o.get("outfit_name", "未知")
                    desc = o.get("description", "无简介")
                    fields = o.get("fields", {})
                    if name == "当前形象":
                        new_outfits["常服"] = AvatarOutfit(description=desc, fields=fields)
                        current = "常服"
                    else:
                        new_outfits[name] = AvatarOutfit(description=desc, fields=fields)
                new_avatar = ConversationAvatar(
                    conversation_id=conversation_id,
                    current_outfit=current,
                    outfits=new_outfits
                )
                # 立即保存迁移后的新结构
                self.save_conversation_avatar(new_avatar)
                return new_avatar

            # 新结构加载
            outfits = {}
            for name, od in data.get("outfits", {}).items():
                outfits[name] = AvatarOutfit(
                    description=od.get("description", "无简介"),
                    fields=od.get("fields", {})
                )
            return ConversationAvatar(
                conversation_id=data.get("conversation_id", conversation_id),
                current_outfit=data.get("current_outfit", "常服"),
                outfits=outfits
            )
        except Exception as e:
            logger.error(f"加载会话 {conversation_id} 数据失败: {e}")
            try:
                backup_path = f"{file_path}.bak.{os.urandom(4).hex()}"
                os.rename(file_path, backup_path)
                logger.warning(f"损坏文件已备份至 {backup_path}")
            except Exception as e2:
                logger.error(f"备份失败: {e2}")
            return None

    def save_conversation_avatar(self, avatar_data: ConversationAvatar):
        file_path = self.get_conversation_file_path(avatar_data.conversation_id)
        try:
            data = {
                "conversation_id": avatar_data.conversation_id,
                "current_outfit": avatar_data.current_outfit,
                "outfits": {name: asdict(outfit) for name, outfit in avatar_data.outfits.items()}
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"保存会话 {avatar_data.conversation_id} 成功")
        except Exception as e:
            logger.error(f"保存失败: {e}")

    def save_outfit_to_list(self, conversation_id: str, outfit_name: str, outfit: AvatarOutfit):
        avatar_data = self.load_conversation_avatar(conversation_id)
        if not avatar_data:
            avatar_data = ConversationAvatar(conversation_id=conversation_id)
        avatar_data.outfits[outfit_name] = outfit
        self.save_conversation_avatar(avatar_data)

    async def terminate(self):
        logger.info("BotAvatarManager 插件已卸载")