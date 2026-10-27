"""
AstrBot 插件：QQ空间桥接 v2.1（WebUI 管理 + 思维链存档）

功能：
  - LLM 工具 qzone_publish：大模型发 QQ 空间说说（可开关、可先审后发），
    发布时把思维链一并入库——模型显式提交的 thinking 参数 + 自动捕获的
    模型推理（ThinkPart/reasoning_content）+ 触发消息。思维链只存档给
    管理员在 WebUI 查看，绝不发到 QQ 空间。
  - Plugin View「QQ空间说说管理」（views/qzone）：开关配置、审核、编辑、
    拒稿、删除、手动发布、思维链查看
  - 不提供任何聊天指令，管理一律走 WebUI

依赖 NapCat 扩展接口：send_qzone_msg / delete_qzone_msg（登录态由 QQ
客户端自己维护，不走 cookie）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time
import uuid
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = None
    json_response = None
    request = None

PLUGIN_NAME = "astrbot_plugin_qzone_bridge"

UGC_RIGHTS = (1, 4, 16, 64, 128)
UGC_RIGHT_DESC = {
    1: "所有人可见",
    4: "好友可见",
    16: "部分好友可见",
    64: "仅自己可见",
    128: "部分好友不可见",
}
MAX_IMAGES = 9
MAX_RECORDS = 300
MAX_THINK_CHARS = 8000
MAX_TRIGGER_CHARS = 500

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

DATA_DIR = os.path.join("data", "qzone_bridge")

TOOL_NAME = "qzone_publish"

TOOL_DESCRIPTION = (
    "在 QQ空间 发表一条说说。仅当用户明确要求「发说说 / 发空间 / 发个动态」时调用。"
    "配图：可以使用图片生成工具 angel_image_generate 生成一张配图，把工具返回的"
    "图片文件路径填进 images 参数。硬性规定：配图只能一张（可以不配图，"
    "但绝不能多于一张，多给的会被丢弃）。"
    "发布需管理员审批：提交后立刻按工具返回的提示行动，私信管理员请求过稿。"
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "说说正文，纯文本，不要带 markdown 标记",
        },
        "images": {
            "type": "string",
            "description": (
                "可选，配图一张：填图片生成工具 angel_image_generate 返回的"
                "文件路径，或 http 图片地址。硬性规定最多一张"
            ),
        },
        "thinking": {
            "type": "string",
            "description": (
                "发这条说说的思维链：你为什么发、怎么构思正文的、想传达什么。"
                "只会存档给管理员在控制台查看，绝不会出现在 QQ 空间里，"
                "请如实、详细地写"
            ),
        },
    },
    "required": ["content"],
}


# ━━━━━━━━━━━━━━━━━━━━━━━━ 记录存储 ━━━━━━━━━━━━━━━━━━━━━━━━


class PostStore:
    """说说记录的 JSON 持久化存储（线程安全，写临时文件后原子替换）。"""

    def __init__(self, path: str, max_records: int = MAX_RECORDS):
        self.path = path
        self.max_records = max_records
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._records = [r for r in data if isinstance(r, dict)]
        except FileNotFoundError:
            self._records = []
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qzone] 记录文件损坏，已重置: {e}")
            self._records = []

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _prune(self) -> None:
        """超出上限时优先淘汰最旧的已完结记录，pending 永不淘汰。"""
        if len(self._records) <= self.max_records:
            return
        finished = [r for r in self._records if r.get("status") != STATUS_PENDING]
        finished.sort(key=lambda r: r.get("created_at") or 0)
        drop = len(self._records) - self.max_records
        drop_ids = {r["id"] for r in finished[:drop]}
        self._records = [r for r in self._records if r["id"] not in drop_ids]

    def add(self, record: dict) -> None:
        with self._lock:
            self._records.insert(0, record)
            self._prune()
            self._flush()

    def get(self, record_id: str) -> Optional[dict]:
        with self._lock:
            for r in self._records:
                if r.get("id") == record_id:
                    return dict(r)
        return None

    def update(self, record_id: str, **fields: Any) -> Optional[dict]:
        with self._lock:
            for r in self._records:
                if r.get("id") == record_id:
                    r.update(fields)
                    self._flush()
                    return dict(r)
        return None

    def delete(self, record_id: str) -> bool:
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.get("id") != record_id]
            if len(self._records) != before:
                self._flush()
                return True
        return False

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    def counts(self) -> dict:
        out = {
            STATUS_PENDING: 0,
            STATUS_PUBLISHED: 0,
            STATUS_REJECTED: 0,
            STATUS_FAILED: 0,
        }
        with self._lock:
            for r in self._records:
                st = r.get("status")
                if st in out:
                    out[st] += 1
        out["total"] = len(self._records)
        return out


# ━━━━━━━━━━━━━━━━━━━━━━━━ 思维链提取 ━━━━━━━━━━━━━━━━━━━━━━━━


def extract_turn_thinking(messages: Any) -> str:
    """从 agent 本轮消息列表里提取思维链（ThinkPart / reasoning 内容）。

    只做鸭子类型读取，不依赖具体版本的类结构；提取不到返回空串。
    """
    chunks: list[str] = []
    for msg in messages or []:
        content = getattr(msg, "content", None)
        parts = content if isinstance(content, list) else []
        for part in parts:
            think = ""
            if isinstance(part, dict):
                if part.get("type") == "think":
                    think = str(part.get("think") or "")
            elif getattr(part, "type", "") == "think":
                think = str(getattr(part, "think", "") or "")
            think = think.strip()
            if think and (not chunks or chunks[-1] != think):
                chunks.append(think)
    return "\n---\n".join(chunks)[:MAX_THINK_CHARS]


def build_thinking(model_thinking: str, auto_thinking: str) -> str:
    """合并模型显式提交的思维链与自动捕获的推理内容。"""
    sections: list[str] = []
    model_thinking = (model_thinking or "").strip()
    auto_thinking = (auto_thinking or "").strip()
    if model_thinking:
        sections.append(model_thinking)
    if auto_thinking and auto_thinking not in model_thinking:
        sections.append("[自动捕获的模型推理]\n" + auto_thinking)
    return "\n\n".join(sections)[:MAX_THINK_CHARS]


# ━━━━━━━━━━━━━━━━━━━━━━━━ LLM 工具 ━━━━━━━━━━━━━━━━━━━━━━━━


def create_qzone_tool(plugin: "QzoneBridge"):
    """构建 qzone_publish 工具实例。

    用 FunctionTool 子类重写 call(context) 而不是 @filter.llm_tool 装饰器：
    装饰器 handler 只能拿到 event，而 call(context) 能读到
    ContextWrapper.messages 里本轮的 ThinkPart —— 也就是模型真正的
    推理内容，实现「发说说时把思维链一并提交」。
    """
    from astrbot.core.agent.tool import FunctionTool

    class _QzonePublishTool(FunctionTool):
        def __init__(self):
            super().__init__(
                name=TOOL_NAME,
                description=TOOL_DESCRIPTION,
                parameters=TOOL_PARAMETERS,
                handler=None,
                handler_module_path=None,
            )
            self._plugin = plugin

        async def call(self, context, **kwargs):
            event = getattr(getattr(context, "context", None), "event", None)
            auto_thinking = ""
            try:
                auto_thinking = extract_turn_thinking(
                    getattr(context, "messages", None)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qzone] 提取模型推理失败: {e}")
            return await plugin.handle_llm_publish(
                event=event,
                content=str(kwargs.get("content") or ""),
                images=str(kwargs.get("images") or ""),
                thinking=str(kwargs.get("thinking") or ""),
                auto_thinking=auto_thinking,
            )

    return _QzonePublishTool()


# ━━━━━━━━━━━━━━━━━━━━━━━━ 插件主体 ━━━━━━━━━━━━━━━━━━━━━━━━


class QzoneBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self._store = PostStore(os.path.join(DATA_DIR, "posts.json"))
        self._last_publish_at: float = 0.0
        self._web_ready = json_response is not None and hasattr(
            self.context, "register_web_api"
        )
        if self._web_ready:
            self._register_web_apis()
        else:
            logger.warning("[qzone] Web API 不可用，WebUI 将无法工作")
        try:
            self._tool = create_qzone_tool(self)
            self.context.add_llm_tools(self._tool)
        except Exception as e:  # noqa: BLE001
            self._tool = None
            logger.error(f"[qzone] 注册 LLM 工具失败: {e}")

    # ───────────────────────── 配置读写 ─────────────────────────

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:  # noqa: BLE001
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        try:
            return str(self.config.get(key, default) or default).strip() or default
        except Exception:  # noqa: BLE001
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:  # noqa: BLE001
            return default

    def _config_dict(self) -> dict:
        return {
            "llm_enabled": self._cfg_bool("llm_enabled", False),
            "publish_mode": self._cfg_str("publish_mode", "review"),
            "admin_only": self._cfg_bool("admin_only", True),
            "ugc_right": self._norm_right(self.config.get("ugc_right"), 4),
            "min_interval_seconds": self._cfg_int("min_interval_seconds", 60),
        }

    def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qzone] 保存配置失败: {e}")

    # ───────────────────────── 基础工具 ─────────────────────────

    @staticmethod
    def _norm_right(value: Any, default: int = 4) -> int:
        try:
            v = int(value)
        except Exception:  # noqa: BLE001
            return default
        return v if v in UGC_RIGHTS else default

    @staticmethod
    def _split_images(raw: Any) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
        else:
            parts = [str(p).strip() for p in raw]
        return [p for p in parts if p][:MAX_IMAGES]

    @staticmethod
    def _localize_images(images: list[str]) -> list[str]:
        """本地文件路径转成 base64:// 纯 base64 形式。

        NapCat 与 AstrBot 是不同容器，互相看不到对方的文件系统；
        NapCat 解析 base64:// 的方式是「去前缀后按 base64 解码」，
        所以不能带 data:...;base64, 前缀。
        """
        out: list[str] = []
        for img in images:
            s = str(img).strip()
            if not s:
                continue
            if s.startswith(("http://", "https://", "base64://", "file://")):
                out.append(s)
                continue
            if os.path.isfile(s):
                try:
                    with open(s, "rb") as f:
                        out.append("base64://" + base64.b64encode(f.read()).decode())
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[qzone] 图片读取失败 {s}: {e}")
            else:
                out.append(s)  # 既不是 URL 也不是本地文件，交给 NapCat 报错
        return out

    def _admin_ids(self) -> list[str]:
        cfg = getattr(self.context, "astrbot_config", None)
        if not cfg:
            return []
        try:
            return [str(x).strip() for x in cfg.get("admins_id", []) if str(x).strip()]
        except Exception:  # noqa: BLE001
            return []

    def _is_admin(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return True  # 无事件上下文（如 WebUI）视为管理员操作
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        return str(event.get_sender_id()).strip() in self._admin_ids()

    def _find_bot(self):
        pm = getattr(self.context, "platform_manager", None)
        for inst in getattr(pm, "platform_insts", None) or []:
            get_client = getattr(inst, "get_client", None)
            if not callable(get_client):
                continue
            try:
                client = get_client()
            except Exception:  # noqa: BLE001
                continue
            if hasattr(client, "call_action"):
                return client
        return None

    def _get_bot(self, event: AstrMessageEvent | None = None):
        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                return bot
        bot = self._find_bot()
        if bot is None:
            raise RuntimeError("找不到 aiocqhttp/NapCat 客户端，平台未就绪")
        return bot

    def _rate_left(self) -> int:
        gap = self._cfg_int("min_interval_seconds", 60)
        if gap <= 0 or self._last_publish_at <= 0:
            return 0
        return max(gap - int(time.time() - self._last_publish_at), 0)

    # ───────────────────────── 发布核心 ─────────────────────────

    async def _send_qzone(
        self,
        content: str,
        images: list[str],
        ugc_right: int,
        target_uins: list[str],
    ) -> str:
        """调 NapCat 扩展接口发说说，返回 tid。

        注意：只有 content/images/权限会发到空间；
        thinking/trigger 只入库，绝不通过这个接口外发。
        """
        left = self._rate_left()
        if left > 0:
            raise RuntimeError(f"发布过于频繁，请 {left}s 后再试")
        params: dict[str, Any] = {"content": content, "ugc_right": ugc_right}
        if images:
            params["images"] = self._localize_images(list(images))[:MAX_IMAGES]
        if ugc_right in (16, 128):
            t = [str(x).strip() for x in target_uins if str(x).strip()]
            if not t:
                raise RuntimeError("权限为部分好友时必须提供目标 QQ 号")
            params["target_uins"] = t
        bot = self._get_bot()
        logger.info(
            f"[qzone] send_qzone_msg len={len(content)} imgs={len(images)} "
            f"ugc_right={ugc_right}({UGC_RIGHT_DESC.get(ugc_right, '?')})"
        )
        ret = await bot.call_action("send_qzone_msg", **params)
        self._last_publish_at = time.time()
        tid = ""
        if isinstance(ret, dict):
            data = ret.get("data") if isinstance(ret.get("data"), dict) else ret
            tid = str(data.get("tid") or "")
        return tid

    async def _delete_qzone(self, tid: str) -> None:
        bot = self._get_bot()
        await bot.call_action("delete_qzone_msg", tid=tid)

    def _new_record(
        self,
        *,
        content: str,
        images: list[str],
        ugc_right: int,
        target_uins: list[str],
        source: str,
        thinking: str = "",
        trigger: str = "",
        sender_id: str = "",
        sender_name: str = "",
    ) -> dict:
        return {
            "id": uuid.uuid4().hex[:10],
            "content": content,
            "images": images[:MAX_IMAGES],
            "ugc_right": ugc_right,
            "target_uins": target_uins,
            "source": source,
            "thinking": (thinking or "")[:MAX_THINK_CHARS],
            "trigger": (trigger or "")[:MAX_TRIGGER_CHARS],
            "sender_id": sender_id,
            "sender_name": sender_name,
            "status": STATUS_PENDING,
            "tid": "",
            "error": "",
            "reject_reason": "",
            "created_at": time.time(),
            "published_at": None,
        }

    async def _publish_record(self, record_id: str) -> tuple[bool, str]:
        """把一条记录发到空间并落库，返回 (是否成功, 给人看的说明)。"""
        rec = self._store.get(record_id)
        if rec is None:
            return False, "记录不存在"
        if rec.get("status") == STATUS_PUBLISHED:
            return False, "该说说已经发布过了"
        content = str(rec.get("content") or "").strip()
        if not content and not rec.get("images"):
            return False, "内容为空"
        try:
            tid = await self._send_qzone(
                content,
                list(rec.get("images") or []),
                self._norm_right(rec.get("ugc_right"), 4),
                list(rec.get("target_uins") or []),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[qzone] 发布失败: {e}")
            self._store.update(
                record_id,
                status=STATUS_FAILED,
                error=str(e)[:300],
                published_at=None,
            )
            return False, f"发布失败：{e}"
        self._store.update(
            record_id,
            status=STATUS_PUBLISHED,
            tid=tid,
            error="",
            published_at=time.time(),
        )
        return True, f"已发布 tid={tid}" if tid else "已发布（未返回 tid）"

    # ───────────────────── LLM 发布入口（思维链入库） ─────────────────────

    async def handle_llm_publish(
        self,
        *,
        event: AstrMessageEvent | None,
        content: str,
        images: str,
        thinking: str,
        auto_thinking: str,
    ) -> str:
        if not self._cfg_bool("llm_enabled", False):
            return "QQ空间发说说功能当前处于关闭状态（管理员可在 AstrBot 控制台开启），本次不发布。"
        if self._cfg_bool("admin_only", True) and not self._is_admin(event):
            return "权限不足：只有管理员可以触发发说说，本次不发布。"
        content = (content or "").strip()
        if not content:
            return "说说正文为空，未发布。"

        imgs = self._split_images(images)
        img_note = ""
        if len(imgs) > 1:
            imgs = imgs[:1]
            img_note = "（配图超过一张，按硬性规定只保留了第一张）"
        elif imgs:
            img_note = "（配图已收录）"

        thinking_full = build_thinking(thinking, auto_thinking)
        trigger = ""
        sender_id = ""
        sender_name = ""
        if event is not None:
            trigger = str(event.get_message_str() or "")[:MAX_TRIGGER_CHARS]
            sender_id = str(event.get_sender_id())
            sender_name = str(event.get_sender_name())

        rec = self._new_record(
            content=content,
            images=imgs,
            ugc_right=self._norm_right(self.config.get("ugc_right"), 4),
            target_uins=[],
            source="llm",
            thinking=thinking_full,
            trigger=trigger,
            sender_id=sender_id,
            sender_name=sender_name,
        )
        self._store.add(rec)
        logger.info(
            f"[qzone] LLM 说说入库 id={rec['id']} thinking_len={len(thinking_full)} "
            f"trigger_len={len(trigger)} imgs={len(imgs)}"
        )

        if self._cfg_str("publish_mode", "review") == "review":
            admin_str = "、".join(self._admin_ids()) or "（控制台管理员）"
            preview = content if len(content) <= 60 else content[:60] + "…"
            return (
                f"说说草稿已保存（ID {rec['id']}），思维链已一并存档{img_note}。"
                f"请立刻私信管理员 {admin_str} 请求审批：用符合你人设的口吻，"
                f"说说你发这条说说的想法，恳请对方通过审核。"
                f"草稿内容供你在私信里引用：「{preview}」"
            )
        ok, msg = await self._publish_record(rec["id"])
        return f"说说{msg}，思维链已一并存档{img_note}" if ok else msg

    # ───────────────────────── Web API ─────────────────────────

    def _register_web_apis(self) -> None:
        routes = [
            ("GET", "status", self.web_status, "获取配置与统计"),
            ("POST", "config/save", self.web_save_config, "保存开关与配置"),
            ("GET", "posts", self.web_posts, "获取说说记录列表"),
            ("POST", "posts/save", self.web_save_post, "新建或编辑说说记录"),
            ("POST", "posts/approve", self.web_approve_post, "审核通过并发布"),
            ("POST", "posts/reject", self.web_reject_post, "拒绝草稿"),
            ("POST", "posts/delete", self.web_delete_post, "删除记录/说说"),
            ("POST", "posts/publish", self.web_publish_post, "手动立即发布"),
        ]
        for method, suffix, handler, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}", handler, [method], desc
            )

    async def _body(self) -> dict:
        try:
            data = await request.json(default={})
        except Exception:  # noqa: BLE001
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _ok(payload: Any):
        return json_response({"status": "ok", "data": payload})

    @staticmethod
    def _err(message: str, code: int = 400):
        return error_response(message, status_code=code)

    async def web_status(self):
        return self._ok({"config": self._config_dict(), "counts": self._store.counts()})

    async def web_save_config(self):
        payload = await self._body()
        if "llm_enabled" in payload:
            self.config["llm_enabled"] = bool(payload["llm_enabled"])
        if "admin_only" in payload:
            self.config["admin_only"] = bool(payload["admin_only"])
        if "publish_mode" in payload:
            mode = str(payload["publish_mode"]).strip()
            if mode not in ("review", "auto"):
                return self._err("publish_mode 只能是 review 或 auto")
            self.config["publish_mode"] = mode
        if "ugc_right" in payload:
            self.config["ugc_right"] = self._norm_right(payload["ugc_right"], 4)
        if "min_interval_seconds" in payload:
            try:
                self.config["min_interval_seconds"] = max(
                    0, int(payload["min_interval_seconds"])
                )
            except Exception:  # noqa: BLE001
                return self._err("min_interval_seconds 必须是整数")
        self._save_config()
        logger.info(f"[qzone] 配置已更新: {self._config_dict()}")
        return self._ok({"config": self._config_dict()})

    async def web_posts(self):
        return self._ok({"posts": self._store.list()})

    def _upsert_fields(self, payload: dict, existing: dict | None = None) -> dict:
        content = str(
            payload.get("content", (existing or {}).get("content", "")) or ""
        ).strip()
        images = self._split_images(
            payload.get("images", (existing or {}).get("images", []))
        )
        ugc_right = self._norm_right(
            payload.get("ugc_right", (existing or {}).get("ugc_right", 4)), 4
        )
        target_raw = payload.get("target_uins", (existing or {}).get("target_uins", []))
        if isinstance(target_raw, str):
            target_uins = self._split_images(target_raw)
        else:
            target_uins = [str(x).strip() for x in (target_raw or []) if str(x).strip()]
        thinking = str(
            payload.get("thinking", (existing or {}).get("thinking", "")) or ""
        )
        return {
            "content": content,
            "images": images,
            "ugc_right": ugc_right,
            "target_uins": target_uins,
            "thinking": thinking[:MAX_THINK_CHARS],
        }

    async def web_save_post(self):
        """新建（无 id）或编辑（带 id）一条记录，默认保存为待审核草稿。"""
        payload = await self._body()
        record_id = str(payload.get("id") or "").strip()
        fields = self._upsert_fields(
            payload, self._store.get(record_id) if record_id else None
        )
        if not fields["content"] and not fields["images"]:
            return self._err("内容为空")
        if fields["ugc_right"] in (16, 128) and not fields["target_uins"]:
            return self._err("权限为部分好友时必须提供目标 QQ 号")

        if record_id:
            rec = self._store.get(record_id)
            if rec is None:
                return self._err("记录不存在", 404)
            if rec.get("status") == STATUS_PUBLISHED:
                return self._err("已发布的说说不支持编辑，可删除后重发")
            rec = self._store.update(
                record_id, **fields, status=STATUS_PENDING, error=""
            )
        else:
            rec = self._new_record(
                content=fields["content"],
                images=fields["images"],
                ugc_right=fields["ugc_right"],
                target_uins=fields["target_uins"],
                thinking=fields["thinking"],
                source="webui",
            )
            self._store.add(rec)
        return self._ok({"post": rec})

    async def web_approve_post(self):
        payload = await self._body()
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            return self._err("缺少 id")
        ok, msg = await self._publish_record(record_id)
        return self._ok({"ok": ok, "message": msg, "post": self._store.get(record_id)})

    async def web_reject_post(self):
        payload = await self._body()
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            return self._err("缺少 id")
        reason = str(payload.get("reason") or "").strip()[:200]
        rec = self._store.get(record_id)
        if rec is None:
            return self._err("记录不存在", 404)
        if rec.get("status") == STATUS_PUBLISHED:
            return self._err("已发布的说说请用删除功能")
        rec = self._store.update(
            record_id, status=STATUS_REJECTED, reject_reason=reason, error=""
        )
        return self._ok({"post": rec})

    async def web_delete_post(self):
        payload = await self._body()
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            return self._err("缺少 id")
        rec = self._store.get(record_id)
        if rec is None:
            return self._err("记录不存在", 404)
        deleted_in_qzone = False
        tid = str(rec.get("tid") or "")
        if payload.get("delete_qzone") and tid:
            try:
                await self._delete_qzone(tid)
                deleted_in_qzone = True
            except Exception as e:  # noqa: BLE001
                return self._err(f"空间说说删除失败：{e}（记录未删除）")
        self._store.delete(record_id)
        return self._ok({"deleted": True, "deleted_in_qzone": deleted_in_qzone})

    async def web_publish_post(self):
        """手动立即发布：新建一条记录并直接发送。"""
        payload = await self._body()
        fields = self._upsert_fields(payload)
        if not fields["content"] and not fields["images"]:
            return self._err("内容为空")
        if fields["ugc_right"] in (16, 128) and not fields["target_uins"]:
            return self._err("权限为部分好友时必须提供目标 QQ 号")
        rec = self._new_record(
            content=fields["content"],
            images=fields["images"],
            ugc_right=fields["ugc_right"],
            target_uins=fields["target_uins"],
            thinking=fields["thinking"],
            source="webui",
        )
        self._store.add(rec)
        ok, msg = await self._publish_record(rec["id"])
        return self._ok({"ok": ok, "message": msg, "post": self._store.get(rec["id"])})

    async def terminate(self):
        try:
            llm_tools = getattr(
                getattr(self.context, "provider_manager", None), "llm_tools", None
            )
            if llm_tools is not None and hasattr(llm_tools, "remove_func"):
                llm_tools.remove_func(TOOL_NAME)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qzone] 移除 LLM 工具失败: {e}")
        logger.info(f"[{PLUGIN_NAME}] 已卸载")
