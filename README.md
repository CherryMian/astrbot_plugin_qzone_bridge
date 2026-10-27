# astrbot_plugin_qzone_bridge

> 通过 NapCat 扩展接口发/删 QQ 空间说说的 AstrBot 插件。
> LLM 工具可开关、可先审后发；思维链一并存档（不外发）；WebUI 管理说说记录。不走 cookie。

## 功能特性

- **LLM 工具 `qzone_publish`**：让 AI 发 QQ 空间说说，支持配图（硬性规定最多一张）
- **思维链存档**：发说说时把「思考过程」一并入库——
  - 自动捕获模型本轮推理（`ThinkPart` / `reasoning_content`，需推理类模型）
  - 模型显式提交的 `thinking` 参数（为什么发、怎么构思）
  - 触发消息上下文
  - **思维链只存档在记录库和 WebUI，绝不会发到 QQ 空间**
- **先审后发 / 直接发布** 两种模式：审核模式下 AI 只存草稿，并按提示私信管理员请求过稿
- **WebUI 管理页**（Plugin View，无需聊天指令）：
  - 开关：LLM 发说说总开关、仅管理员可触发、发布方式、默认可见权限、发布冷却
  - 说说管理：过稿 / 编辑 / 拒稿 / 失败重试 / 删除记录 / 删除并撤回空间说说
  - 手动发布：正文 + 图片 + 权限（支持部分好友可见），立即发布或存草稿
  - 思维链折叠查看
- **不走 cookie**：登录态由 QQ 客户端自己维护，不解析网页 CGI

## 依赖

| 依赖 | 版本 | 说明 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | >= 4.5.0（实测 4.29.1） | aiocqhttp 平台适配器 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | >= 4.10（实测 4.18.41） | 需包含扩展接口 `send_qzone_msg` / `delete_qzone_msg` |

> `send_qzone_msg` / `delete_qzone_msg` 是 NapCat 的**扩展接口**（非 OneBot 11 标准），
> 不需要给 NapCat 开 HTTP 端口，AstrBot 通过 `call_action` 透传即可。

## 安装

```bash
cd <AstrBot数据目录>/plugins
git clone https://github.com/CherryMian/astrbot_plugin_qzone_bridge
# 重启 AstrBot
docker restart astrbot   # 或在控制台重启
```

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `llm_enabled` | `false` | LLM 发说说总开关（WebUI 可随时切换） |
| `publish_mode` | `review` | `review`=先存草稿人工过稿；`auto`=直接发布 |
| `admin_only` | `true` | 仅管理员可触发 LLM 发说说 |
| `ugc_right` | `4` | 默认可见权限：1 所有人 / 4 好友 / 16 部分好友 / 64 仅自己 / 128 部分好友不可见 |
| `min_interval_seconds` | `60` | 两次发布的最小间隔，0 为不限制 |

## 使用

**WebUI**：AstrBot 控制台左侧「扩展功能 → QQ空间说说管理」。

**LLM 流程**（审核模式）：

1. 用户对 AI 说「发个说说：……」
2. AI 调用 `qzone_publish`（可先用图片生成工具画一张配图，硬限一张），`thinking` 参数写思维链
3. 工具返回提示 AI 私信管理员请求审批（以 AI 自己的人设口吻）
4. 管理员在 WebUI 查看草稿（含思维链）→ 「通过并发布」或「拒绝」

**手动发布 / 管理**：全部在 WebUI 完成，插件不提供聊天指令。

## 记录存储

说说记录（含思维链）持久化在 `<AstrBot数据目录>/qzone_bridge/posts.json`，
上限 300 条（待审核不淘汰），原子写入。思维链单条上限 8000 字符。

## 技术说明

- **思维链为什么不外发**：`send_qzone_msg` 的请求体只构造 `content / images / ugc_right / target_uins` 四个字段，思维链与触发消息只进记录库
- **跨容器图片**：NapCat 与 AstrBot 通常跑在不同容器，本地文件路径互相不可见。
  插件会把本地图片路径自动转成 `base64://` + 纯 base64（NapCat 的解析方式是去前缀后直接 base64 解码，不能带 `data:...;base64,` 前缀）
- **配图硬限一张**：LLM 提交多张时只保留第一张并在返回中提示；WebUI 手动发布最多 9 张
- **思维链自动捕获**：通过 `FunctionTool` 子类重写 `call(context)` 读取本轮 `ThinkPart`（`@filter.llm_tool` 装饰器只能拿到 event，拿不到消息上下文）。非推理模型该通道为空，思维链主要来自 `thinking` 参数

## 注意事项

- 发说说有频控，默认 60 秒间隔，连发可能触发 QQ 侧风控
- `ugc_right` 只接受 1 / 4 / 16 / 64 / 128，为 16/128 时必须提供目标 QQ 号
- NapCat 版本过老（< 4.10）可能没有 `send_qzone_msg` 扩展接口，升级即可

## License

[MIT](LICENSE)
