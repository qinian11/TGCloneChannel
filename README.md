# Telegram 频道消息转发机器人

一个功能强大的 Telegram 机器人，用于将消息从一个频道转发到其他频道或用户。它支持文本处理、媒体组和广告过滤。支持禁止转发的频道等等！

## 功能

- 从公共或私有频道转发消息。
- 转发到新频道或用户。
- 批量转发历史消息。
- 文本处理：
    - 使用正则表达式删除内容。
    - 替换文本。
    - 追加文本。
- 基于关键字的广告过滤。
- 处理媒体组。
- 通过环境变量或机器人命令进行动态配置。
- 用户特定的消息删除。

## 安装

1.  克隆此存储库。
2.  安装所需的 Python 包。并运行 `pip install -r requirements.txt`。所需的包是：
    - `telethon`
    - `python-telegram-bot`
    - `python-dotenv`

## 配置

1.  在项目根目录中创建一个 `.env` 文件。
2.  将以下环境变量添加到 `.env` 文件中：
    - `TG_API_ID`: 您的 Telegram API ID。
    - `TG_API_HASH`: 您的 Telegram API Hash。
    - `TG_BOT_TOKEN`: 您的 Telegram 机器人令牌。
    - `REPLACE_RULES` (可选): 文本替换规则 (例如, `old1:new1|old2:new2`)。
    - `DELETE_PATTERNS` (可选): 用于文本删除的正则表达式模式，用 `|` 分隔。
    - `APPEND_TEXT` (可选): 要附加到消息的文本。
    - `AD_MEDIA_KEYWORDS` (可选): 用于识别广告媒体组的关键字，用 `|` 分隔。

## 使用方法

1.  运行机器人：
    ```bash
    python main.py
    ```
2.  在 Telegram 上与机器人互动：
    - `/start`: 启动机器人并查看欢迎消息。
    - `/help`: 显示包含可用命令的帮助消息。
    - `/config`: 查看或修改当前配置。
    - `/forward <source_channel_link> <target_channel_link> <start_message_id> [end_message_id]`: 批量转发消息。
    - 发送消息链接给机器人以转发单个消息。
    - `/clear`: 删除机器人发送的消息。
    - `/stop`: 停止正在进行的批量转发任务。
## 示例图
<img width="639" height="911" alt="image" src="https://github.com/user-attachments/assets/089728a0-abfb-42f8-b65c-2aec4ef1757a" />
<img width="634" height="725" alt="image" src="https://github.com/user-attachments/assets/fa22bb47-7a9f-4fc0-8a28-456d55fd3288" />

## 许可证

个人娱乐用，切勿用于非法用途， https://t.me/d2_22
