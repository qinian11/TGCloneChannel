import os
import re
import logging
import random
import asyncio
from typing import Any, Tuple, Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient, events

# 可选：自动加载.env文件（需安装python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram API 凭证（建议用环境变量管理）
API_ID = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')
BOT_TOKEN = os.environ.get('TG_BOT_TOKEN')

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError('请在环境变量中设置 TG_API_ID、TG_API_HASH 和 TG_BOT_TOKEN，或在 .env 文件中配置。')

# 代理配置
# proxy = ('http', '127.0.0.1', 7890)
# 创建 Telethon 客户端
client = TelegramClient('message_forwarder_session', API_ID, API_HASH)

# 新增：用于用户账号的 Telethon 客户端（用于历史消息收集）
user_client = TelegramClient('user_session', API_ID, API_HASH)

# 添加两步验证处理函数
def handle_2fa():
    """处理两步验证密码输入"""
    try:
        user_client.start()
        return True
    except Exception as e:
        if "2FA" in str(e) or "password" in str(e).lower():
            print("⚠️  检测到两步验证，请输入你的两步验证密码：")
            password = input("请输入两步验证密码: ")
            try:
                user_client.sign_in(password=password)
                print("✅ 两步验证成功！")
                return True
            except Exception as e2:
                print(f"❌ 两步验证失败: {e2}")
                return False
        else:
            print(f"❌ 登录失败: {e}")
            return False

# 消息链接正则表达式模式
MESSAGE_LINK_PATTERN = r'https?://t\.me/(?:c/(\d+)|([^/]+))/(\d+)'

# 存储每个用户最近发送的消息ID，用于批量删除
# TODO: 可替换为数据库或文件持久化
user_sent_messages: dict[int, list[int]] = {}
# 存储用户发送的指令消息ID
user_command_messages: dict[int, list[int]] = {}

# 新增：用户停止批量转发的标志
user_stop_flags = {}

# 文本处理规则从环境变量读取
REPLACE_RULES = os.environ.get('REPLACE_RULES', '')  # 格式：old1:new1|old2:new2
DELETE_PATTERNS = os.environ.get('DELETE_PATTERNS', '')  # 正则，|分隔
APPEND_TEXT = os.environ.get('APPEND_TEXT', '')  # 直接追加
AD_MEDIA_KEYWORDS = os.environ.get('AD_MEDIA_KEYWORDS', '')  # 广告媒体组关键词，|分隔

LINKS_DIR = 'links'
if not os.path.exists(LINKS_DIR):
    os.makedirs(LINKS_DIR)

def get_links_file(channel_name: str) -> str:
    return os.path.join(LINKS_DIR, f"{channel_name}_links.txt")

def process_text(text: str) -> str:
    # 删除内容
    if DELETE_PATTERNS:
        for pat in DELETE_PATTERNS.split('|'):
            if pat.strip():
                try:
                    text = re.sub(pat.strip(), '', text)
                except re.error as e:
                    logger.error(f"无效的删除正则: {pat}，错误: {e}")
    # 替换内容
    if REPLACE_RULES:
        for rule in REPLACE_RULES.split('|'):
            if ':' in rule:
                old, new = rule.split(':', 1)
                text = text.replace(old, new)
    # 追加内容
    if APPEND_TEXT:
        text = text.rstrip() + '\n' + APPEND_TEXT
    return text.strip()

async def track_bot_message(user_id: int, message: Any) -> Any:
    """跟踪机器人发送的消息，用于后续删除"""
    if user_id not in user_sent_messages:
        user_sent_messages[user_id] = []
    user_sent_messages[user_id].append(message.message_id)
    return message


async def track_user_message(update: Update) -> None:
    """跟踪用户发送的消息，用于后续删除"""
    user_id = update.effective_user.id
    if user_id not in user_command_messages:
        user_command_messages[user_id] = []
    user_command_messages[user_id].append(update.message.message_id)


async def should_respond_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查在群聊中是否应该响应消息"""
    if update.message.chat.type == 'private':
        return True
    message_text = update.message.text or ""
    bot_username = context.bot.username
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == 'mention':
                mention = message_text[entity.offset:entity.offset + entity.length]
                if mention == f"@{bot_username}":
                    return True
    if update.message.reply_to_message:
        if update.message.reply_to_message.from_user.username == bot_username:
            return True
    return False


def parse_link(link: str) -> Tuple[Optional[Any], Optional[int]]:
    """解析Telegram消息链接，提取频道实体和消息ID

    Args:
        link: 要解析的Telegram消息链接字符串

    Returns:
        Tuple[Optional[Any], Optional[int]]:
            第一个元素是频道实体(可能是频道ID或用户名)，
            第二个元素是消息ID。如果解析失败则返回(None, None)

    支持的链接格式示例：
        https://t.me/channel_name/message_id
        https://t.me/c/channel_id/message_id
    """
    # 使用正则表达式匹配消息链接
    matches = re.search(MESSAGE_LINK_PATTERN, link)
    if not matches:
        return None, None

    # 提取正则匹配的分组：频道ID、频道用户名和消息ID
    channel_id, channel_username, message_id = matches.groups()
    message_id = int(message_id)  # 将消息ID转为整数

    # 处理频道标识：如果是数字ID格式(c/12345)，则转换为Telethon需要的负实体ID
    if channel_id:
        channel_id = int(channel_id)
        entity = -1000000000000 - channel_id  # Telethon的特殊处理方式
    else:
        entity = channel_username  # 如果是用户名格式，直接使用用户名

    return entity, message_id


def build_link(entity: Any, message_id: int) -> str:
    """构建消息链接

    Args:
        entity: 频道实体，可以是频道ID(整数)或频道用户名(字符串)
        message_id: 消息ID

    Returns:
        str: 构造完成的Telegram消息链接

    功能说明:
        根据频道实体类型(用户名或ID)构造不同格式的消息链接:
        1. 当entity是字符串(频道用户名)时，构造格式为: https://t.me/{username}/{message_id}
        2. 当entity是整数(频道ID)时，需要特殊处理:
           - 原始频道ID = abs(entity + 1000000000000)
           - 构造格式为: https://t.me/c/{channel_id}/{message_id}
    """
    if isinstance(entity, str):
        return f"https://t.me/{entity}/{message_id}"
    else:
        original_channel_id = str(abs(entity + 1000000000000))
        return f"https://t.me/c/{original_channel_id}/{message_id}"


async def send_message_to_user(entity: Any, message_id: int, user_id: int, add_link: bool = True) -> bool:
    """发送单个消息给用户

    Args:
        entity: 消息来源实体，可以是频道ID或用户名
        message_id: 要转发的消息ID
        user_id: 目标用户ID
        add_link: 是否在转发消息中添加原始消息链接，默认为True

    Returns:
        bool: 转发是否成功
    """
    try:
        # 获取消息ID前后10条消息范围，提高获取成功率
        message_ids = list(range(message_id - 10, message_id + 10))
        messages = await client.get_messages(entity, ids=message_ids)

        # 查找目标消息
        target_msg = next((msg for msg in messages if msg and msg.id == message_id), None)
        if not target_msg:
            return False

        # 处理媒体组消息（图片组等）
        if target_msg.grouped_id:
            valid_messages = [msg for msg in messages if msg and msg.grouped_id == target_msg.grouped_id]
        else:
            valid_messages = [target_msg]
        valid_messages.sort(key=lambda x: x.id)  # 按消息ID排序

        sent_message_ids = []
        media_list = [msg.media for msg in valid_messages if msg.media]  # 收集所有媒体内容

        # 提取并处理文本内容
        text_content = ""
        for msg in valid_messages:
            if msg.text:
                text_content = process_text(msg.text)  # 应用文本处理规则
                break

        # 可选添加原始消息链接
        if add_link:
            text_content += f"\n\n🔗 原始消息: {build_link(entity, message_id)}"

        # 处理带媒体的消息
        if media_list:
            # 媒体消息的caption限制1024字符
            caption = text_content[:1024] if len(text_content) > 1024 else text_content
            sent_messages = await client.send_file(
                user_id,
                file=media_list,
                caption=caption
            )
            # 记录发送的消息ID（媒体组可能返回多个消息）
            if isinstance(sent_messages, list):
                sent_message_ids.extend([msg.id for msg in sent_messages])
            else:
                sent_message_ids.append(sent_messages.id)
            # 如果文本过长，剩余部分单独发送
            if len(text_content) > 1024:
                text_msg = await client.send_message(user_id, f"完整内容：\n{text_content}")
                sent_message_ids.append(text_msg.id)

        # 处理纯文本消息
        elif text_content:
            text_msg = await client.send_message(user_id, text_content)
            sent_message_ids.append(text_msg.id)

        # 记录用户已发送的消息ID，用于后续管理
        if user_id not in user_sent_messages:
            user_sent_messages[user_id] = []
        user_sent_messages[user_id].extend(sent_message_ids)

        return True
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        return False


def is_ad_media_group(valid_messages: list) -> bool:
    if not AD_MEDIA_KEYWORDS:
        return False
    keywords = [k.strip() for k in AD_MEDIA_KEYWORDS.split('|') if k.strip()]
    for msg in valid_messages:
        if msg.text:
            for kw in keywords:
                if kw in msg.text:
                    return True
    return False


async def send_message_to_channel(entity: Any, message_id: int, channel_entity: Any, add_link: bool = True) -> bool:
    """发送单个消息到频道，支持文本处理和广告内容过滤"""
    try:
        message_ids = list(range(message_id - 10, message_id + 10))
        messages = await client.get_messages(entity, ids=message_ids)
        target_msg = next((msg for msg in messages if msg and msg.id == message_id), None)
        if not target_msg:
            return False
        if target_msg.grouped_id:
            valid_messages = [msg for msg in messages if msg and msg.grouped_id == target_msg.grouped_id]
        else:
            valid_messages = [target_msg]
        valid_messages.sort(key=lambda x: x.id)
        # 广告内容过滤（无论媒体组还是单条文本）
        if is_ad_media_group(valid_messages):
            logger.info(f"检测到广告内容，已跳过（message_id={message_id}）")
            return False
        media_list = [msg.media for msg in valid_messages if msg.media]
        text_content = ""
        for msg in valid_messages:
            if msg.text:
                text_content = process_text(msg.text)
                break
        # 频道转发时不再自动添加原始消息链接
        # if add_link:
        #     text_content += f"\n\n🔗 原始消息: {build_link(entity, message_id)}"
        # 统一处理文本
        if media_list:
            caption = text_content[:1024] if len(text_content) > 1024 else text_content
            sent_messages = await client.send_file(
                channel_entity,
                file=media_list,
                caption=caption
            )
            if len(text_content) > 1024:
                await client.send_message(channel_entity, f"完整内容：\n{text_content}")
        elif text_content:
            await client.send_message(channel_entity, text_content)
        return True
    except Exception as e:
        logger.error(f"发送到频道消息失败: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user_message(update)
    user = update.effective_user
    try:
        message = await update.message.reply_text(
            f'👋 <b>你好，{user.first_name}！</b>\n\n'
            f'我是<b>小卡拉米专属机器人</b>，\n请发送 <b>Telegram 消息链接</b>，我会将消息转发给你。\n'
            f'💡 在群聊中使用时，请@我或回复我的消息。\n\n'
            f'📖 克隆频道使用说明：/help',
            parse_mode='HTML'
        )
        await track_bot_message(user.id, message)
    except Exception as e:
        logger.error(f"/start 响应失败: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user_message(update)
    help_text = (
        '🤖 <b>小卡拉米专属机器人使用说明</b>\n\n'
        '📥 <b>消息转发</b>\n'
        '  只需发送 Telegram 消息链接，我会将消息转发给你。\n'
        '  支持的链接格式：\n'
        '    • https://t.me/channel_name/message_id\n'
        '    • https://t.me/c/channel_id/message_id\n\n'
        '🛠 <b>常用命令</b>\n'
        '  /start   - 启动机器人，获取欢迎信息\n'
        '  /help    - 查看本帮助说明\n'
        '  /clear   - 删除最近发送的消息\n'
        '  /random  [消息链接] [数量] - 随机发送指定频道的消息（最多50条）\n'
        '      例：/random https://t.me/channel_name/123456 5\n'
        '  /collectlinks [频道用户名或ID或链接] - 收集频道数据并保存\n'
        '      例：/collectlinks @yourchannel\n'
        '  /listlinks - 查看所有已收集的频道及其消息数量\n'
        '  /sendto [链接文件名或频道名或频道链接或@频道名] [目标频道]\n'
        '      例：/sendto yourchannel_links.txt @targetchannel\n'
        '  /stop    - 停止当前批量转发任务\n\n'
        '📌 <b>群聊使用提示</b>\n'
        '  • 在群聊中需要@我才会响应\n'
        '  • 也可以回复我的消息来触发\n'
        '  • 命令始终有效，无需@我\n\n'
        '⚙️ <b>文本转发处理支持</b>\n'
        '  • 支持通过 .env 文件配置 REPLACE_RULES/DELETE_PATTERNS/APPEND_TEXT 实现批量替换、删除、追加内容\n'
        '  • 留空则不做处理，详见 .env 示例\n'
    )
    try:
        message = await update.message.reply_text(help_text, parse_mode='HTML')
        await track_bot_message(update.effective_user.id, message)
    except Exception as e:
        logger.error(f"/help 响应失败: {e}")


async def process_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await should_respond_in_group(update, context):
        return
    await track_user_message(update)
    entity, message_id = parse_link(update.message.text)
    if not entity:
        try:
            message = await update.message.reply_text('请发送有效的 Telegram 消息链接。')
            await track_bot_message(update.effective_user.id, message)
        except Exception as e:
            logger.error(f"无效链接响应失败: {e}")
        return
    success = await send_message_to_user(entity, message_id, update.effective_user.id)
    if not success:
        try:
            message = await update.message.reply_text('无法获取该消息，请检查链接或权限。')
            await track_bot_message(update.effective_user.id, message)
        except Exception as e:
            logger.error(f"消息获取失败响应失败: {e}")


async def random_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await track_user_message(update)
    try:
        args = context.args if hasattr(context, 'args') else []
        if not args:
            message = await update.message.reply_text(
                '请提供消息链接。\n用法: /random https://t.me/channel_name/message_id [数量]')
            await track_bot_message(update.effective_user.id, message)
            return
        entity, max_message_id = parse_link(args[0])
        if not entity:
            message = await update.message.reply_text('请发送有效的 Telegram 消息链接。')
            await track_bot_message(update.effective_user.id, message)
            return
        send_count = 10
        if len(args) > 1:
            try:
                send_count = int(args[1])
                if send_count <= 0:
                    message = await update.message.reply_text('发送数量必须大于0。')
                    await track_bot_message(update.effective_user.id, message)
                    return
                if send_count > 50:
                    message = await update.message.reply_text('发送数量不能超过50条。')
                    await track_bot_message(update.effective_user.id, message)
                    return
            except ValueError:
                message = await update.message.reply_text('请输入有效的数字作为发送数量。')
                await track_bot_message(update.effective_user.id, message)
                return
        user_sent_messages[update.effective_user.id] = []
        user_command_messages[update.effective_user.id] = []
        sent_count = 0
        attempts = 0
        max_attempts = send_count * 5
        while sent_count < send_count and attempts < max_attempts:
            rand_id = random.randint(1, max_message_id)
            attempts += 1
            success = await send_message_to_user(entity, rand_id, update.effective_user.id)
            if success:
                sent_count += 1
            await asyncio.sleep(2)  # 每条消息间隔2秒，防止转发过快
        if sent_count > 0:
            message = await update.message.reply_text(
                f'已成功发送 {sent_count} 条随机消息！\n使用 /clear 可以删除这些消息。')
            await track_bot_message(update.effective_user.id, message)
        else:
            message = await update.message.reply_text('未能找到有效消息，请检查链接或稍后重试。')
            await track_bot_message(update.effective_user.id, message)
    except Exception as e:
        logger.error(f'随机消息处理错误: {e}')
        try:
            message = await update.message.reply_text(f'获取随机消息时出错: {str(e)}')
            await track_bot_message(update.effective_user.id, message)
        except Exception as e2:
            logger.error(f"随机消息异常响应失败: {e2}")


async def clear_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """删除用户与机器人交互的所有消息（包括用户命令消息和机器人回复消息）

    Args:
        update: 包含更新信息的对象
        context: 上下文对象
    """
    try:
        # 获取用户ID和待删除消息列表
        user_id = update.effective_user.id
        bot_messages = user_sent_messages.get(user_id, [])  # 机器人发送的消息
        user_messages = user_command_messages.get(user_id, [])  # 用户发送的命令消息
        all_messages = bot_messages + user_messages

        # 检查是否有消息需要删除
        if not all_messages:
            message = await update.message.reply_text('⚠️ 没有可删除的消息。', parse_mode='HTML')
            await track_bot_message(user_id, message)
            return

        # 添加当前命令消息到删除列表
        all_messages.append(update.message.message_id)
        deleted_count = 0

        # 发送删除状态通知
        status_message = await update.message.reply_text(f'🗑️ 正在删除 <b>{len(all_messages)}</b> 条消息...', parse_mode='HTML')

        # 批量删除消息
        for msg_id in all_messages:
            try:
                await client.delete_messages(user_id, msg_id)
                deleted_count += 1
            except Exception as e:
                logger.error(f"删除消息 {msg_id} 失败: {e}")

        # 清空消息记录
        user_sent_messages[user_id] = []
        user_command_messages[user_id] = []

        # 删除状态消息
        try:
            await client.delete_messages(user_id, status_message.message_id)
        except Exception as e:
            logger.error(f"删除状态消息失败: {e}")

        # 根据删除结果发送反馈
        if deleted_count > 0:
            try:
                result_message = await update.message.reply_text(f'✅ 已成功删除 <b>{deleted_count}</b> 条消息！', parse_mode='HTML')
                await asyncio.sleep(3)  # 延迟3秒后删除结果消息
                await client.delete_messages(user_id, result_message.message_id)
            except Exception as e:
                logger.error(f"删除结果消息失败: {e}")
        else:
            message = await update.message.reply_text('⚠️ 删除失败，可能消息已被删除或超过48小时。', parse_mode='HTML')
            await track_bot_message(user_id, message)

    except Exception as e:
        logger.error(f'删除消息时出错: {e}')
        try:
            message = await update.message.reply_text(f'❌ 删除消息时出错: {str(e)}', parse_mode='HTML')
            await track_bot_message(update.effective_user.id, message)
        except Exception as e2:
            logger.error(f"删除消息异常响应失败: {e2}")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await should_respond_in_group(update, context):
        return
    await track_user_message(update)
    try:
        message = await update.message.reply_text(
            '🤖 <b>我是小卡拉米专属机器人</b>\n请发送 <b>Telegram 消息链接</b>。\n如需帮助，请使用 /help 命令。',
            parse_mode='HTML'
        )
        await track_bot_message(update.effective_user.id, message)
    except Exception as e:
        logger.error(f"echo 响应失败: {e}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_stop_flags[user_id] = True
    await update.message.reply_text("已收到停止指令，正在尝试中断批量转发。")


async def sendto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """从保存的频道消息链接文件中读取所有链接，依次转发到指定频道。支持直接输入频道名、@频道名、频道链接或txt文件名。"""
    await track_user_message(update)
    try:
        args = context.args if hasattr(context, 'args') else []
        if len(args) < 2:
            message = await update.message.reply_text(
                '用法: /sendto <链接文件名或频道名或频道链接或@频道名> <目标频道>\n'
                '例如: /sendto yourchannel_links.txt @targetchannel\n'
                '或: /sendto @yourchannel @targetchannel\n'
                '或: /sendto https://t.me/yourchannel @targetchannel')
            await track_bot_message(update.effective_user.id, message)
            return
        file_or_channel = args[0]
        target_channel = args[1]
        # 判断是否为txt文件，否则自动转为xxx_links.txt
        if file_or_channel.endswith('.txt'):
            file_name = os.path.join(LINKS_DIR, file_or_channel)
        else:
            channel_name = safe_channel_name(file_or_channel)
            file_name = get_links_file(channel_name)
        if not os.path.isfile(file_name):
            message = await update.message.reply_text(f'文件 {file_name} 不存在，请先用 /collectlinks 命令生成。')
            await track_bot_message(update.effective_user.id, message)
            return
        # 读取所有链接
        with open(file_name, 'r', encoding='utf-8') as f:
            links = [line.strip() for line in f if line.strip()]
        if not links:
            message = await update.message.reply_text(f'文件 {file_name} 没有可用的频道数据。')
            await track_bot_message(update.effective_user.id, message)
            return
        message = await update.message.reply_text(f'开始向 {target_channel} 转发 {len(links)} 条消息，请耐心等待...\n如需中断，请发送 /stop')
        await track_bot_message(update.effective_user.id, message)
        success_count = 0
        fail_count = 0
        user_id = update.effective_user.id
        user_stop_flags[user_id] = False  # 开始前重置
        for link in links:
            # 检查是否收到停止指令
            if user_stop_flags.get(user_id):
                await update.message.reply_text("批量转发已被手动停止。")
                user_stop_flags[user_id] = False  # 重置
                break
            entity, message_id = parse_link(link)
            if not entity:
                fail_count += 1
                continue
            result = await send_message_to_channel(entity, message_id, target_channel)
            if result:
                success_count += 1
            else:
                fail_count += 1
            await asyncio.sleep(3)  # 每条消息间隔2秒，防止转发过快
        message2 = await update.message.reply_text(f'转发完成！成功: {success_count} 条，失败: {fail_count} 条。')
        await track_bot_message(update.effective_user.id, message2)
    except Exception as e:
        logger.error(f'/sendto 批量转发命令处理错误: {e}')
        message = await update.message.reply_text(f'批量转发消息时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)


async def collect_channel_history_links(entity: Any, save_path: str) -> None:
    """收集整个频道历史消息的链接并保存，媒体组只保存一次。"""
    from telethon.tl.types import Message
    
    if user_client is None:
        raise RuntimeError("用户客户端未启动，无法收集频道历史消息。请检查两步验证设置。")
    
    links = []
    grouped_ids = set()
    async for msg in user_client.iter_messages(entity, reverse=True):
        if not isinstance(msg, Message):
            continue
        # 媒体组去重
        if msg.grouped_id:
            if msg.grouped_id in grouped_ids:
                continue
            grouped_ids.add(msg.grouped_id)
        link = build_link(entity, msg.id)
        links.append(link)
        await asyncio.sleep(0.5)  # 每收集一条消息间隔0.5秒，防止被限流
    # 保存到文件
    with open(save_path, 'w', encoding='utf-8') as f:
        for link in links:
            f.write(link + '\n')
    print(f"已保存 {len(links)} 条数据到 {save_path}")


def safe_channel_name(channel: str) -> str:
    # 提取用户名或ID，并去除特殊字符
    if channel.startswith('https://t.me/'):
        channel = channel.replace('https://t.me/', '')
    channel = channel.lstrip('@')
    channel = re.sub(r'[^a-zA-Z0-9_\-]', '', channel)
    return channel


async def collectlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """收集频道历史消息链接并保存为 txt 文件，媒体组只保存一次。"""
    await track_user_message(update)
    
    # 检查用户客户端是否启动
    if user_client is None:
        message = await update.message.reply_text(
            '❌ 用户客户端未启动，无法收集频道历史消息。\n'
            '请重启机器人并正确输入两步验证密码。')
        await track_bot_message(update.effective_user.id, message)
        return
    
    try:
        args = context.args if hasattr(context, 'args') else []
        if not args:
            message = await update.message.reply_text(
                '用法: /collectlinks <频道用户名或ID>\n例如: /collectlinks @yourchannel 或 /collectlinks https://t.me/yourchannel')
            await track_bot_message(update.effective_user.id, message)
            return
        channel = args[0]
        channel_name = safe_channel_name(channel)
        save_file = get_links_file(channel_name)
        message = await update.message.reply_text(f'正在收集 {channel} 的数据，请稍候...')
        await track_bot_message(update.effective_user.id, message)
        await collect_channel_history_links(channel, save_file)
        # 统计收集到的条数
        count = 0
        if os.path.isfile(save_file):
            with open(save_file, 'r', encoding='utf-8') as f:
                count = sum(1 for _ in f if _.strip())
        message2 = await update.message.reply_text(f'收集完成，收集了 {count} 条数据，已保存到 {save_file}。')
        await track_bot_message(update.effective_user.id, message2)
    except Exception as e:
        logger.error(f'/collectlinks 命令处理错误: {e}')
        message = await update.message.reply_text(f'收集历史数据时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)


async def listlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """列出所有已收集的频道链接文件及其消息数量"""
    try:
        files = [f for f in os.listdir(LINKS_DIR) if f.endswith('_links.txt')]
        if not files:
            message = await update.message.reply_text('还没有收集任何频道数据。')
            await track_bot_message(update.effective_user.id, message)
            return
        lines = []
        for fname in files:
            fpath = os.path.join(LINKS_DIR, fname)
            count = 0
            with open(fpath, 'r', encoding='utf-8') as f:
                count = sum(1 for _ in f if _.strip())
            # 提取频道名并加@
            channel_name = fname.replace('_links.txt', '')
            lines.append(f"@{channel_name} : {count} 条")
        msg = '已收集的频道数据：\n' + '\n'.join(lines)
        message = await update.message.reply_text(msg)
        await track_bot_message(update.effective_user.id, message)
    except Exception as e:
        logger.error(f'/listlinks 命令处理错误: {e}')
        message = await update.message.reply_text(f'列出数据文件时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)


def main() -> None:
    # 创建应用程序
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_messages))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(MESSAGE_LINK_PATTERN) & ~filters.COMMAND,
        process_message_link
    ))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.add_handler(CommandHandler("random", random_message))
    application.add_handler(CommandHandler("sendto", sendto_command))
    application.add_handler(CommandHandler("collectlinks", collectlinks_command))
    application.add_handler(CommandHandler("listlinks", listlinks_command))
    application.add_handler(CommandHandler("stop", stop_command))
    # 启动 Telethon 客户端（同步）
    client.start(bot_token=BOT_TOKEN)
    
    # 处理用户客户端登录，包括两步验证
    print("正在启动用户客户端...")
    if not handle_2fa():
        print("❌ 用户客户端启动失败，机器人将无法使用 /collectlinks 功能")
        user_client = None
    else:
        print("✅ 用户客户端启动成功")
    
    print("机器人已启动")
    # 运行机器人直到按下 Ctrl-C
    application.run_polling()
    # 关闭 Telethon 客户端
    client.disconnect()
    if user_client:
        user_client.disconnect()


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == 'collect':
        # 用法: python 新建文本文档.py collect <频道用户名或ID>
        channel = sys.argv[2]
        save_file = f"{channel}_links.txt"
        with client:
            client.loop.run_until_complete(collect_channel_history_links(channel, save_file))
    else:
        main()