import os
import re
import logging
import random
import asyncio
from typing import Any
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon import errors
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
# 禁用 httpx 的日志输出
logging.getLogger("httpx").setLevel(logging.WARNING)
API_ID_STR = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')
BOT_TOKEN = os.environ.get('TG_BOT_TOKEN')

if not API_ID_STR or not API_HASH or not BOT_TOKEN:
    raise RuntimeError('请在环境变量中设置 TG_API_ID、TG_API_HASH 和 TG_BOT_TOKEN')

try:
    API_ID = int(API_ID_STR)
except ValueError:
    raise RuntimeError('环境变量 TG_API_ID 必须为整数')

# 代理配置
#proxy = ('http', '127.0.0.1', 7890)
# 创建 Telethon 客户端
client = TelegramClient('message_forwarder_session', API_ID, API_HASH)

# 新增：用于用户账号的 Telethon 客户端（用于历史消息收集）
user_client = TelegramClient('user', API_ID, API_HASH)

# 用户客户端可用标志
USER_CLIENT_READY = False

# 消息链接正则表达式模式
# 匹配格式：https://t.me/channel_name/message_id 或 https://t.me/c/channel_id/message_id
MESSAGE_LINK_PATTERN = r'https?://t\.me/(?:c/(\d+)|([^/]+))/(\d+)'

# 存储每个用户最近发送的消息ID，用于批量删除
user_sent_messages = {}
# 存储用户发送的指令消息ID
user_command_messages = {}

# 新增：用户停止批量转发的标志
user_stop_flags = {}

# 文本处理规则（支持动态修改）
REPLACE_RULES = os.environ.get('REPLACE_RULES', '')  # 格式：old1:new1|old2:new2
DELETE_PATTERNS = os.environ.get('DELETE_PATTERNS', '')  # 正则，|分隔
APPEND_TEXT = os.environ.get('APPEND_TEXT', '')  # 直接追加
AD_MEDIA_KEYWORDS = os.environ.get('AD_MEDIA_KEYWORDS', '')  # 广告媒体组关键词，|分隔

# 动态配置存储（运行时修改）
dynamic_config = {
    'replace_rules': REPLACE_RULES,
    'delete_patterns': DELETE_PATTERNS,
    'append_text': APPEND_TEXT,
    'ad_keywords': AD_MEDIA_KEYWORDS,
    'delay_seconds': 1.0  # 默认每条消息间隔1秒
}

LINKS_DIR = 'links'
if not os.path.exists(LINKS_DIR):
    os.makedirs(LINKS_DIR)

def get_links_file(channel_name: str) -> str:
    return os.path.join(LINKS_DIR, f"{channel_name}_links.txt")

def process_text(text: str) -> str:
    """处理文本：删除、替换、追加（支持动态配置）"""
    # 使用动态配置
    delete_patterns = dynamic_config['delete_patterns']
    replace_rules = dynamic_config['replace_rules']
    append_text = dynamic_config['append_text']
    
    # 删除内容
    if delete_patterns:
        for pat in delete_patterns.split('|'):
            if pat.strip():
                try:
                    text = re.sub(pat.strip(), '', text)
                except re.error as e:
                    logger.error(f"无效的删除正则: {pat}，错误: {e}")
    # 替换内容
    if replace_rules:
        for rule in replace_rules.split('|'):
            if ':' in rule:
                old, new = rule.split(':', 1)
                text = text.replace(old, new)
    # 追加内容
    if append_text:
        text = text.rstrip() + '\n' + append_text
    return text.strip()


async def track_bot_message(user_id, message):
    """跟踪机器人发送的消息，用于后续删除"""
    if user_id not in user_sent_messages:
        user_sent_messages[user_id] = []
    user_sent_messages[user_id].append(message.message_id)
    return message

async def track_user_message(update):
    """跟踪用户发送的消息，用于后续删除"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if user_id not in user_command_messages:
        user_command_messages[user_id] = []
    user_command_messages[user_id].append(update.message.message_id)

async def should_respond_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查在群聊中是否应该响应消息"""
    # 检查 update.message 是否存在
    if not update.message:
        return False
    
    # 私聊中总是响应
    if update.message.chat.type == 'private':
        return True
    
    # 群聊中检查是否@了机器人
    message_text = update.message.text or ""
    bot_username = context.bot.username
    
    # 检查消息中是否包含@机器人
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == 'mention':
                mention = message_text[entity.offset:entity.offset + entity.length]
                if mention == f"@{bot_username}":
                    return True
    
    # 也检查回复消息是否是回复给机器人的
    if update.message.reply_to_message:
        if update.message.reply_to_message.from_user.username == bot_username:
            return True
    
    return False

def parse_link(link):
    """解析消息链接，返回entity和message_id"""
    matches = re.search(MESSAGE_LINK_PATTERN, link)
    logger.info(f"解析链接: {matches}")
    if not matches:
        return None, None
    
    channel_id, channel_username, message_id = matches.groups()
    message_id = int(message_id)
    
    if channel_id:  # 私有频道
        channel_id = int(channel_id)
        entity = -1000000000000 - channel_id
    else:  # 公开频道
        entity = channel_username
    
    return entity, message_id

def build_link(entity, message_id):
    """构建消息链接"""
    if isinstance(entity, str):  # 公开频道
        return f"https://t.me/{entity}/{message_id}"
    else:  # 私有频道
        original_channel_id = str(abs(entity + 1000000000000))
        return f"https://t.me/c/{original_channel_id}/{message_id}"

async def send_message_to_user(entity, message_id, user_id, add_link=True):
    """发送单个消息给用户"""
    try:
        # 获取消息组, 前后10条
        message_ids = list(range(message_id - 10, message_id + 10))
        messages = await client.get_messages(entity, ids=message_ids)
        
        # 找到目标消息和同组消息
        target_msg = next((msg for msg in messages if msg and msg.id == message_id), None)
        if not target_msg:
            return False
        
        # 获取同组消息
        if target_msg.grouped_id:
            valid_messages = [msg for msg in messages if msg and msg.grouped_id == target_msg.grouped_id]
        else:
            valid_messages = [target_msg]
        
        valid_messages.sort(key=lambda x: x.id)
        sent_message_ids = []
        
        # 收集媒体文件
        media_list = [msg.media for msg in valid_messages if msg.media]
        
        # 准备文本内容和格式化信息 - 收集所有消息的文本内容
        text_content = ""
        formatting_entities = []
        text_offset = 0
        
        for msg in valid_messages:
            if msg.text and msg.text.strip():
                msg_text = msg.text.strip()
                if text_content:
                    text_content += "\n\n" + msg_text
                    text_offset += 2  # 添加换行符的长度
                else:
                    text_content = msg_text
                
                # 收集格式化信息，调整偏移量
                if msg.entities:
                    for entity in msg.entities:
                        # 创建新的实体，调整偏移量
                        new_entity = entity.__class__(
                            offset=entity.offset + text_offset,
                            length=entity.length
                        )
                        formatting_entities.append(new_entity)
                
                text_offset += len(msg_text)
        
        # 应用文本处理规则（在清理 ** 符号之后）
        if text_content:
            original_text = text_content
            text_content = process_text(text_content)
        #if add_link:
        if media_list:
            # 发送媒体组
            caption = text_content[:1024] if len(text_content) > 1024 else text_content
            
            # 过滤适合caption长度的格式化信息
            caption_entities = []
            if formatting_entities and len(text_content) <= 1024:
                caption_entities = formatting_entities
            elif formatting_entities and len(text_content) > 1024:
                # 只保留在caption范围内的格式化信息
                for entity in formatting_entities:
                    if entity.offset < 1024:
                        caption_entities.append(entity)
            
            sent_messages = await client.send_file(
                user_id, 
                file=media_list, 
                caption=caption,
                formatting_entities=caption_entities if caption_entities else None
            )
            
            # 记录消息ID
            if isinstance(sent_messages, list):
                sent_message_ids.extend([msg.id for msg in sent_messages])
            else:
                sent_message_ids.append(sent_messages.id)
            
            # 如果文本太长，单独发送
            if len(text_content) > 1024:
                remaining_text = text_content[1024:]
                remaining_entities = []
                if formatting_entities:
                    for entity in formatting_entities:
                        if entity.offset >= 1024:
                            # 调整偏移量
                            new_entity = entity.__class__(
                                offset=entity.offset - 1024,
                                length=entity.length
                            )
                            remaining_entities.append(new_entity)
                
                text_msg = await client.send_message(
                    user_id, 
                    f"完整内容：\n{remaining_text}",
                    formatting_entities=remaining_entities if remaining_entities else None
                )
                sent_message_ids.append(text_msg.id)
        
        elif text_content:
            # 只发送文本
            text_msg = await client.send_message(
                user_id, 
                text_content,
                formatting_entities=formatting_entities if formatting_entities else None
            )
            sent_message_ids.append(text_msg.id)
        
        # 记录发送的消息
        if user_id not in user_sent_messages:
            user_sent_messages[user_id] = []
        user_sent_messages[user_id].extend(sent_message_ids)
        
        return True
    
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        return False

def is_ad_media_group(valid_messages: list) -> bool:
    ad_keywords = dynamic_config['ad_keywords']
    if not ad_keywords:
        return False
    keywords = [k.strip() for k in ad_keywords.split('|') if k.strip()]
    for msg in valid_messages:
        if msg.text:
            for kw in keywords:
                if kw in msg.text:
                    return True
    return False

async def send_message_to_channel(entity: Any, message_id: int, channel_entity: Any, add_link: bool = True) -> bool:
    try:
        # 获取消息组, 前后10条
        start_id = max(1, message_id - 10)
        end_id = message_id + 10
        message_ids = list(range(start_id, end_id))
        messages = await client.get_messages(entity, ids=message_ids)
        target_msg = next((msg for msg in messages if msg and msg.id == message_id), None)
        if not target_msg:
            logger.warning(f"未找到消息 ID {message_id}")
            return False
        if target_msg.grouped_id:
            valid_messages = [msg for msg in messages if msg and msg.grouped_id == target_msg.grouped_id]
        else:
            valid_messages = [target_msg]
        
        valid_messages.sort(key=lambda x: x.id)
        if is_ad_media_group(valid_messages):
            logger.info(f"检测到广告内容，已跳过（message_id={message_id}）")
            return False
        media_list = [msg.media for msg in valid_messages if msg.media]
        text_content = ""
        formatting_entities = []
        text_offset = 0
        
        for i, msg in enumerate(valid_messages):
            if msg.text and msg.text.strip():
                msg_text = msg.text.strip()
                if msg.entities and '**' in msg_text:
                    clean_text = msg_text.replace('**', '')
                    if msg.entities:
                        offset_adjustment = 0
                        adjusted_entities = []
                        for entity in msg.entities:
                            text_before_entity = msg_text[:entity.offset]
                            stars_before = text_before_entity.count('**') * 2  
                            entity_text = msg_text[entity.offset:entity.offset + entity.length]
                            stars_in_entity = entity_text.count('**') * 2
                            new_offset = entity.offset - stars_before
                            new_length = entity.length - stars_in_entity
                            if new_length > 0:  
                                new_entity = entity.__class__(offset=new_offset, length=new_length)
                                adjusted_entities.append(new_entity)
                        msg.entities = adjusted_entities
                    msg_text = clean_text
                if text_content:
                    text_content += "\n\n" + msg_text
                    text_offset += 2  
                else:
                    text_content = msg_text
                if msg.entities:
                    for entity in msg.entities:
                        new_entity = entity.__class__(
                            offset=entity.offset + text_offset,
                            length=entity.length
                        )
                        formatting_entities.append(new_entity)
                text_offset += len(msg_text)
        if text_content:
            original_text = text_content
            text_content = process_text(text_content)
        if media_list:
            # 媒体消息的caption限制1024字符
            caption = text_content[:1024] if len(text_content) > 1024 else text_content
            # 过滤适合caption长度的格式化信息
            caption_entities = []
            if formatting_entities and len(text_content) <= 1024:
                caption_entities = formatting_entities
            elif formatting_entities and len(text_content) > 1024:
                # 只保留在caption范围内的格式化信息
                for entity in formatting_entities:
                    if entity.offset < 1024:
                        caption_entities.append(entity)
            try:
                sent_messages = await client.send_file(
                    channel_entity,
                    file=media_list,
                    caption=caption,
                    formatting_entities=caption_entities if caption_entities else None
                )
                print("✅ 发送成功")
            except Exception as e:
                html_caption = convert_to_html(caption, caption_entities)
                
                sent_messages = await client.send_file(
                    channel_entity,
                    file=media_list,
                    caption=html_caption,
                    parse_mode='html'
                )
                print("✅ 发送成功")
            # 如果文本过长，剩余部分单独发送
            if len(text_content) > 1024:
                remaining_text = text_content[1024:]
                remaining_entities = []
                if formatting_entities:
                    for entity in formatting_entities:
                        if entity.offset >= 1024:
                            new_entity = entity.__class__(
                                offset=entity.offset - 1024,
                                length=entity.length
                            )
                            remaining_entities.append(new_entity)
                
                await client.send_message(
                    channel_entity, 
                    f"完整内容：\n{remaining_text}",
                    formatting_entities=remaining_entities if remaining_entities else None
                )
        elif text_content:
            # 尝试使用 formatting_entities
            try:
                await client.send_message(
                    channel_entity, 
                    text_content, 
                    formatting_entities=formatting_entities if formatting_entities else None
                )
                print("✅ 发送成功")
            except Exception as e:
                html_text = convert_to_html(text_content, formatting_entities)
                
                await client.send_message(
                    channel_entity, 
                    html_text, 
                    parse_mode='html'
                )
                print("✅ 发送成功")
        
        return True
    except errors.ChatWriteForbiddenError:
        logger.error(f"发送到频道消息失败: 机器人没有权限向 '{channel_entity}' 频道发送消息")
        logger.error("请确保机器人已加入目标频道并具有发送消息的权限")
        return False
    except errors.ChatAdminRequiredError:
        logger.error(f"发送到频道消息失败: 机器人需要管理员权限才能向 '{channel_entity}' 频道发送消息")
        return False
    except errors.PeerIdInvalidError:
        logger.error(f"发送到频道消息失败: 频道 '{channel_entity}' 不存在或无法访问")
        return False
    except errors.FloodWaitError as e:
        raise e
    except Exception as e:
        logger.error(f"发送到频道消息失败: {e}")
        return False
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """当用户发送 /start 命令时的处理函数"""
    if not update.message:
        return
    await track_user_message(update)
    user = update.effective_user
    message = await update.message.reply_text(f'你好，{user.first_name}！\n'
                                   f'请发送 Telegram 消息链接，我会将消息转发给你。\n\n'
                                   f'💡 在群聊中使用时，请@我或回复我的消息。\n\n'
                                   f'🔗 需要开TG会员找 @HY499\n'
                                   f'🔍 资源搜索 @souba8')
    await track_bot_message(user.id, message)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """当用户发送 /help 命令时的处理函数"""
    if not update.message:
        return
    await track_user_message(update)
    help_text = '将 Telegram 消息链接发送给我，我会尝试获取并转发该消息给你。\n'
    help_text += '支持的链接格式：\n'
    help_text += '- https://t.me/channel_name/message_id\n'
    help_text += '- https://t.me/c/channel_id/message_id\n\n'
    help_text += '另外，你也可以使用以下命令：\n'
    help_text += '/random https://t.me/channel_name/message_id     # 随机发送10条消息\n'
    help_text += '/random https://t.me/channel_name/message_id 5   # 随机发送5条消息\n'
    help_text += '/clear                                           # 删除最近发送的消息\n'
    help_text += '/collectlinks @yourchannel                       # 收集频道历史消息链接\n'
    help_text += '/listlinks                                       # 查看已收集的频道数据\n'
    help_text += '/sendto yourchannel_links.txt @targetchannel     # 克隆频道到目标频道\n'
    help_text += '/stop                                            # 停止批量转发任务\n\n'
    help_text += '📝 文本处理配置命令：\n'
    help_text += '/config                                          # 查看当前配置\n'
    help_text += '/config replace 原文本:新文本                    # 添加替换规则\n'
    help_text += '/config delete 正则表达式                        # 添加删除规则\n'
    help_text += '/config append 追加文本                         # 设置追加文本\n'
    help_text += '/config ad 广告关键词                           # 添加广告关键词\n'
    help_text += '/config clear 类型                               # 清除指定类型规则\n'
    help_text += '/config reset                                    # 重置所有配置\n'
    help_text += '/config save                                     # 保存配置到文件\n'
    help_text += '/config load                                     # 从文件加载配置\n'
    help_text += '/testconfig 测试文本                             # 测试文本处理效果\n\n'
    help_text += '📌 群聊使用提示：\n'
    help_text += '• 在群聊中需要@我才会响应\n'
    help_text += '• 也可以回复我的消息来触发\n'
    help_text += '• 命令始终有效，无需@我\n\n'
    help_text += '🔗 相关服务：\n'
    help_text += '• 需要开TG会员找 @HY499\n'
    help_text += '• 资源搜索 @souba8'
    message = await update.message.reply_text(help_text)
    await track_bot_message(update.effective_user.id, message)
async def process_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户发送的消息链接"""
    # 检查 update.message 是否存在
    if not update.message:
        return
    # 检查是否应该响应（群聊中需要@机器人）
    if not await should_respond_in_group(update, context):
        return
    await track_user_message(update)
    entity, message_id = parse_link(update.message.text)
    if not entity:
        message = await update.message.reply_text('请发送有效的 Telegram 消息链接。')
        await track_bot_message(update.effective_user.id, message)
        return
    success = await send_message_to_user(entity, message_id, update.effective_user.id)
    if not success:
        message = await update.message.reply_text('无法获取该消息，请检查链接或权限。')
        await track_bot_message(update.effective_user.id, message)
async def random_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """根据提供的消息链接随机发送指定数量的消息"""
    if not update.message:
        return
    await track_user_message(update)
    try:
        args = context.args if hasattr(context, 'args') else []
        if not args:
            message = await update.message.reply_text('请提供消息链接。\n用法: /random https://t.me/channel_name/message_id [数量]')
            await track_bot_message(update.effective_user.id, message)
            return
        entity, max_message_id = parse_link(args[0])
        if not entity:
            message = await update.message.reply_text('请发送有效的 Telegram 消息链接。')
            await track_bot_message(update.effective_user.id, message)
            return
        # 解析发送数量，默认为10条
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
        max_attempts = send_count * 5  # 最多尝试次数为目标数量的5倍
        while sent_count < send_count and attempts < max_attempts:
            rand_id = random.randint(1, max_message_id)
            attempts += 1
            max_retries = 3
            retry_count = 0
            success = False
            while retry_count < max_retries and not success:
                try:
                    success = await send_message_to_user(entity, rand_id, update.effective_user.id)
                    if success:
                        sent_count += 1
                    success = True  # 标记为已处理
                except errors.FloodWaitError as e:
                    retry_count += 1
                    wait_time = e.seconds
                    logger.warning(f"随机消息遇到限流，等待 {wait_time} 秒后重试")
                    await asyncio.sleep(wait_time + 1)
                except Exception as e:
                    logger.error(f"发送随机消息时出错: {e}")
                    success = True  # 标记为已处理
        if sent_count > 0:
            message = await update.message.reply_text(f'已成功发送 {sent_count} 条随机消息！\n使用 /clear 可以删除这些消息。')
            await track_bot_message(update.effective_user.id, message)
        else:
            message = await update.message.reply_text('未能找到有效消息，请检查链接或稍后重试。')
            await track_bot_message(update.effective_user.id, message)

    except Exception as e:
        logger.error(f'随机消息处理错误: {e}')
        message = await update.message.reply_text(f'获取随机消息时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)

async def clear_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """删除最近发送给用户的消息以及用户的指令消息"""
    if not update.message:
        return
    try:
        user_id = update.effective_user.id
        
        # 获取要删除的消息列表
        bot_messages = user_sent_messages.get(user_id, [])
        user_messages = user_command_messages.get(user_id, [])
        all_messages = bot_messages + user_messages
        
        if not all_messages:
            message = await update.message.reply_text('没有可删除的消息。')
            await track_bot_message(user_id, message)
            return
        
        # 添加当前清理命令消息到删除列表
        all_messages.append(update.message.message_id)
        
        deleted_count = 0
        status_message = await update.message.reply_text(f'正在删除 {len(all_messages)} 条消息...')
        
        # 批量删除消息
        for msg_id in all_messages:
            try:
                await client.delete_messages(user_id, msg_id)
                deleted_count += 1
            except Exception as e:
                logger.error(f"删除消息 {msg_id} 失败: {e}")
        
        # 清空记录
        user_sent_messages[user_id] = []
        user_command_messages[user_id] = []
        
        # 删除状态消息
        try:
            await client.delete_messages(user_id, status_message.message_id)
        except:
            pass
        
        if deleted_count > 0:
            result_message = await update.message.reply_text(f'已成功删除 {deleted_count} 条消息！')
            # 延迟删除结果消息
            import asyncio
            await asyncio.sleep(3)
            try:
                await client.delete_messages(user_id, result_message.message_id)
            except:
                pass
        else:
            message = await update.message.reply_text('删除失败，可能消息已被删除或超过48小时。')
            await track_bot_message(user_id, message)
    
    except Exception as e:
        logger.error(f'删除消息时出错: {e}')
        message = await update.message.reply_text(f'删除消息时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理非链接消息"""
    # 检查 update.message 是否存在
    if not update.message:
        return
    
    # 检查是否应该响应（群聊中需要@机器人）
    if not await should_respond_in_group(update, context):
        return
    
    await track_user_message(update)
    message = await update.message.reply_text('请发送 Telegram 消息链接。如需帮助，请使用 /help 命令。')
    await track_bot_message(update.effective_user.id, message)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """停止批量转发任务"""
    if not update.message:
        return
    user_id = update.effective_user.id
    user_stop_flags[user_id] = True
    await update.message.reply_text("已收到停止指令，正在尝试中断批量转发。")

# ==================== 动态配置管理命令 ====================

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """动态配置管理命令"""
    if not update.message:
        return
    await track_user_message(update)
    if not context.args:
        # 显示当前配置
        config_text = "📋 当前文本处理配置：\n\n"
        config_text += f"🔄 替换规则：\n{dynamic_config['replace_rules'] or '无'}\n\n"
        config_text += f"🗑️ 删除规则：\n{dynamic_config['delete_patterns'] or '无'}\n\n"
        config_text += f"➕ 追加文本：\n{dynamic_config['append_text'] or '无'}\n\n"
        config_text += f"🚫 广告关键词：\n{dynamic_config['ad_keywords'] or '无'}\n\n"
        delay = dynamic_config.get('delay_seconds', 1.0)
        config_text += f"⏱️ 发送延迟：{delay} 秒（克隆发送时每条消息的间隔时间）\n\n"
        config_text += "📝 使用方法：\n"
        config_text += "• /config replace 原文本:新文本\n"
        config_text += "• /config delete 正则表达式\n"
        config_text += "• /config append 追加的文本\n"
        config_text += "• /config ad 广告关键词\n"
        config_text += "• /config clear 类型 - 清除指定类型的所有规则\n"
        config_text += "• /config remove 类型 规则 - 删除特定规则\n"
        config_text += "• /config reset - 重置所有配置\n"
        config_text += "• /config save - 保存配置到文件\n"
        config_text += "• /config load - 从文件加载配置\n"
        config_text += "• /config reload - 重新加载配置文件（手动修改后使用）\n\n"
        config_text += "💡 提示：延迟时间（delay_seconds）需要在 config.json 文件中手动修改，然后使用 /config reload 重新加载"
        
        message = await update.message.reply_text(config_text)
        await track_bot_message(update.effective_user.id, message)
        return
    
    command = context.args[0].lower()
    
    if command == "replace":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 用法：/config replace 原文本:新文本")
            return
        
        rule = ' '.join(context.args[1:])
        if ':' not in rule:
            await update.message.reply_text("❌ 替换规则格式错误！请使用：原文本:新文本")
            return
        
        # 添加到现有规则
        if dynamic_config['replace_rules']:
            dynamic_config['replace_rules'] += '|' + rule
        else:
            dynamic_config['replace_rules'] = rule
        
        await update.message.reply_text(f"✅ 已添加替换规则：{rule}")
    
    elif command == "delete":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 用法：/config delete 正则表达式")
            return
        
        pattern = ' '.join(context.args[1:])
        
        # 测试正则表达式
        try:
            re.compile(pattern)
        except re.error as e:
            await update.message.reply_text(f"❌ 正则表达式错误：{e}")
            return
        
        # 添加到现有规则
        if dynamic_config['delete_patterns']:
            dynamic_config['delete_patterns'] += '|' + pattern
        else:
            dynamic_config['delete_patterns'] = pattern
        
        await update.message.reply_text(f"✅ 已添加删除规则：{pattern}")
    
    elif command == "append":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 用法：/config append 要追加的文本")
            return
        
        text = ' '.join(context.args[1:])
        dynamic_config['append_text'] = text
        await update.message.reply_text(f"✅ 已设置追加文本：{text}")
    
    elif command == "ad":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 用法：/config ad 广告关键词")
            return
        
        keyword = ' '.join(context.args[1:])
        
        # 添加到现有规则
        if dynamic_config['ad_keywords']:
            dynamic_config['ad_keywords'] += '|' + keyword
        else:
            dynamic_config['ad_keywords'] = keyword
        
        await update.message.reply_text(f"✅ 已添加广告关键词：{keyword}")
    
    elif command == "clear":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 用法：/config clear <类型>\n支持的类型：replace, delete, append, ad")
            return
        
        clear_type = context.args[1].lower()
        
        # 映射类型到实际的配置键名
        type_mapping = {
            'replace': 'replace_rules',
            'delete': 'delete_patterns',
            'append': 'append_text',
            'ad': 'ad_keywords'
        }
        
        if clear_type not in type_mapping:
            await update.message.reply_text("❌ 无效的类型！支持：replace, delete, append, ad")
            return
        
        config_key = type_mapping[clear_type]
        dynamic_config[config_key] = ""
        await update.message.reply_text(f"✅ 已清除 {clear_type} 规则")
    
    elif command == "remove":
        if len(context.args) < 3:
            await update.message.reply_text("❌ 用法：/config remove <类型> <要删除的规则>\n支持的类型：replace, delete, ad")
            return
        
        remove_type = context.args[1].lower()
        rule_to_remove = ' '.join(context.args[2:])
        
        # 映射类型到实际的配置键名
        type_mapping = {
            'replace': 'replace_rules',
            'delete': 'delete_patterns', 
            'ad': 'ad_keywords'
        }
        
        if remove_type not in type_mapping:
            await update.message.reply_text("❌ 无效的类型！支持：replace, delete, ad")
            return
        
        config_key = type_mapping[remove_type]
        current_rules = dynamic_config[config_key]
        if not current_rules:
            await update.message.reply_text(f"❌ {remove_type} 规则为空，无需删除")
            return
        
        # 分割规则并查找要删除的规则
        rules_list = current_rules.split('|')
        original_count = len(rules_list)
        # 移除匹配的规则
        rules_list = [rule for rule in rules_list if rule.strip() != rule_to_remove.strip()]
        
        if len(rules_list) == original_count:
            await update.message.reply_text(f"❌ 未找到规则：{rule_to_remove}")
            return
        # 更新配置
        dynamic_config[config_key] = '|'.join(rules_list)
        removed_count = original_count - len(rules_list)
        await update.message.reply_text(f"✅ 已删除 {removed_count} 条 {remove_type} 规则")
    elif command == "reset":
        dynamic_config['replace_rules'] = ""
        dynamic_config['delete_patterns'] = ""
        dynamic_config['append_text'] = ""
        dynamic_config['ad_keywords'] = ""
        await update.message.reply_text("✅ 已重置所有配置")
    elif command == "save":
        try:
            import json
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(dynamic_config, f, ensure_ascii=False, indent=2)
            await update.message.reply_text("✅ 配置已保存到 config.json")
        except Exception as e:
            await update.message.reply_text(f"❌ 保存失败：{e}")
    
    elif command == "load":
        try:
            import json
            if os.path.exists('config.json'):
                with open('config.json', 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    dynamic_config.update(loaded_config)
                await update.message.reply_text("✅ 配置已从 config.json 重新加载")
            else:
                await update.message.reply_text("❌ config.json 文件不存在")
        except Exception as e:
            await update.message.reply_text(f"❌ 加载失败：{e}")
    
    elif command == "reload":
        """重新加载配置文件（与 load 相同，但更明确的命名）"""
        try:
            import json
            if os.path.exists('config.json'):
                with open('config.json', 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    dynamic_config.update(loaded_config)
                
                # 显示加载的配置摘要
                summary = "✅ 配置已重新加载：\n"
                if loaded_config.get('replace_rules'):
                    summary += f"🔄 替换规则: {len(loaded_config['replace_rules'].split('|'))} 条\n"
                if loaded_config.get('delete_patterns'):
                    summary += f"🗑️ 删除规则: {len(loaded_config['delete_patterns'].split('|'))} 条\n"
                if loaded_config.get('append_text'):
                    summary += f"➕ 追加文本: 已设置\n"
                if loaded_config.get('ad_keywords'):
                    summary += f"🚫 广告关键词: {len(loaded_config['ad_keywords'].split('|'))} 个\n"
                if loaded_config.get('delay_seconds'):
                    summary += f"⏱️ 发送延迟: {loaded_config['delay_seconds']} 秒\n"
                
                await update.message.reply_text(summary)
            else:
                await update.message.reply_text("❌ config.json 文件不存在")
        except Exception as e:
            await update.message.reply_text(f"❌ 重新加载失败：{e}")
    
    else:
        await update.message.reply_text("❌ 未知命令！使用 /config 查看帮助")

async def test_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """测试文本处理配置"""
    if not update.message:
        return
    await track_user_message(update)
    
    if not context.args:
        await update.message.reply_text("❌ 用法：/testconfig 测试文本")
        return
    
    test_text = ' '.join(context.args)
    processed_text = process_text(test_text)
    
    result_text = f"🧪 文本处理测试：\n\n"
    result_text += f"📝 原始文本：\n{test_text}\n\n"
    result_text += f"🔄 处理后文本：\n{processed_text}\n\n"
    
    if test_text == processed_text:
        result_text += "ℹ️ 文本未发生变化"
    else:
        result_text += "✅ 文本已处理"
    
    message = await update.message.reply_text(result_text)
    await track_bot_message(update.effective_user.id, message)

async def collect_channel_history_links(entity: Any, save_path: str) -> None:
    """收集整个频道历史消息的链接并保存，媒体组只保存一次。"""
    from telethon.tl.types import Message
    
    if not USER_CLIENT_READY:
        raise RuntimeError("用户客户端未启动，无法收集频道历史消息。请检查两步验证设置。")
    
    # 首先获取总消息数
    total_count = (await user_client.get_messages(entity, limit=0)).total
    links = []
    grouped_ids = set()
    
    print(f"开始收集 {total_count} 条消息...")
    processed_count = 0
    
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
        
        processed_count += 1
        # 每处理10条消息显示一次进度
        if processed_count % 10 == 0 or processed_count == total_count:
            progress = (processed_count / total_count) * 100
            bar_length = 30
            filled_length = int(bar_length * processed_count // total_count)
            bar = '█' * filled_length + '-' * (bar_length - filled_length)
            print(f'\r进度: |{bar}| {progress:.1f}% ({processed_count}/{total_count})', end='', flush=True)
        
        await asyncio.sleep(0.01)  # 每收集一条消息间隔0.01秒，防止被限流
    
    print()  # 换行
    
    # 保存到文件
    with open(save_path, 'w', encoding='utf-8') as f:
        for link in links:
            f.write(link + '\n')
    print(f"已保存 {len(links)} 条数据到 {save_path}")

def safe_channel_name(channel: str) -> str:
    """提取用户名或ID，并去除特殊字符"""
    if channel.startswith('https://t.me/'):
        channel = channel.replace('https://t.me/', '')
    channel = channel.lstrip('@')
    channel = re.sub(r'[^a-zA-Z0-9_\-]', '', channel)
    return channel

def convert_to_html(text: str, entities: list) -> str:
    """将格式化实体转换为 HTML 格式"""
    if not entities:
        return text
    
    # 按偏移量排序
    entities = sorted(entities, key=lambda x: x.offset)
    
    # 从后往前处理，避免偏移量变化
    result = text
    for entity in reversed(entities):
        start = entity.offset
        end = entity.offset + entity.length
        
        if start >= len(result) or end > len(result):
            continue
            
        entity_text = result[start:end]
        
        if hasattr(entity, '__class__'):
            entity_type = entity.__class__.__name__
            
            if entity_type == 'MessageEntityBold':
                html_text = f"<b>{entity_text}</b>"
            elif entity_type == 'MessageEntityItalic':
                html_text = f"<i>{entity_text}</i>"
            elif entity_type == 'MessageEntityCode':
                html_text = f"<code>{entity_text}</code>"
            elif entity_type == 'MessageEntityPre':
                html_text = f"<pre>{entity_text}</pre>"
            elif entity_type == 'MessageEntityTextUrl':
                html_text = f'<a href="{entity.url}">{entity_text}</a>'
            elif entity_type == 'MessageEntityMention':
                html_text = f"<a href=\"https://t.me/{entity_text.lstrip('@')}\">{entity_text}</a>"
            else:
                html_text = entity_text
            
            result = result[:start] + html_text + result[end:]
    
    return result

async def collectlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """收集频道历史消息链接并保存为 txt 文件，媒体组只保存一次。"""
    if not update.message:
        return
    await track_user_message(update)

    # 检查用户客户端是否启动
    if not USER_CLIENT_READY:
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
        channel_input = args[0]
        channel_name = safe_channel_name(channel_input)
        save_file = get_links_file(channel_name)
        
        # 解析频道实体
        try:
            if channel_input.startswith('https://t.me/'):
                # 如果是链接，提取频道名
                channel_entity = channel_input.replace('https://t.me/', '').lstrip('@')
            else:
                # 直接使用频道名或ID
                channel_entity = channel_input.lstrip('@')
            
            message = await update.message.reply_text(f'正在收集 {channel_input} 的数据，请稍候...')
            await track_bot_message(update.effective_user.id, message)
            await collect_channel_history_links(channel_entity, save_file)
        except Exception as e:
            logger.error(f"解析频道实体失败: {e}")
            message = await update.message.reply_text(f'无法解析频道 {channel_input}，请检查频道名或链接是否正确。')
            await track_bot_message(update.effective_user.id, message)
            return
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
    if not update.message:
        return
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

async def sendto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """从保存的频道消息链接文件中读取所有链接，依次转发到指定频道。支持直接输入频道名、@频道名、频道链接或txt文件名。"""
    if not update.message:
        return
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
        for i, link in enumerate(links):
            # 检查是否收到停止指令
            if user_stop_flags.get(user_id):
                await update.message.reply_text("批量转发已被手动停止。")
                user_stop_flags[user_id] = False  # 重置
                break
            
            entity, message_id = parse_link(link)
            if not entity:
                fail_count += 1
                continue
            
            # 尝试发送消息，自动处理限流
            max_retries = 3
            retry_count = 0
            success = False
            
            while retry_count < max_retries and not success:
                try:
                    result = await send_message_to_channel(entity, message_id, target_channel)
                    if result:
                        success_count += 1
                        success = True
                    else:
                        fail_count += 1
                        success = True  # 标记为已处理，避免重试
                except errors.FloodWaitError as e:
                    retry_count += 1
                    wait_time = e.seconds
                    logger.warning(f"遇到限流，等待 {wait_time} 秒后重试 (第 {retry_count}/{max_retries} 次)")
                    
                    # 更新进度消息
                    progress_msg = await update.message.reply_text(
                        f'⏳ 遇到限流，等待 {wait_time} 秒后继续...\n'
                        f'进度: {i+1}/{len(links)} | 成功: {success_count} | 失败: {fail_count}'
                    )
                    
                    await asyncio.sleep(wait_time + 1)  # 等待限流时间 + 1秒缓冲
                    
                    # 删除进度消息
                    try:
                        await client.delete_messages(update.effective_user.id, progress_msg.message_id)
                    except:
                        pass
                    
                except Exception as e:
                    logger.error(f"发送消息时出错: {e}")
                    fail_count += 1
                    success = True  # 标记为已处理
            
            if not success:
                fail_count += 1
                logger.error(f"消息发送失败，已达到最大重试次数: {link}")
            
            # 正常间隔（从配置读取，默认1秒）
            delay = float(dynamic_config.get('delay_seconds', 1.0))
            if delay > 0:
                await asyncio.sleep(delay)  # 每条消息间隔，防止转发过快
        message2 = await update.message.reply_text(f'转发完成！成功: {success_count} 条，失败: {fail_count} 条。')
        await track_bot_message(update.effective_user.id, message2)
    except Exception as e:
        logger.error(f'/sendto 批量转发命令处理错误: {e}')
        message = await update.message.reply_text(f'批量转发消息时出错: {str(e)}')
        await track_bot_message(update.effective_user.id, message)

async def post_init(app: Application) -> None:
    """在 PTB 应用启动后初始化 Telethon 客户端"""
    global USER_CLIENT_READY
    
    # 启动 bot 客户端
    await client.start(bot_token=BOT_TOKEN)
    print("Bot 客户端已启动")
    
    # 注册机器人命令菜单
    try:
        from telegram import BotCommand
        commands = [
            BotCommand("start", "开始使用机器人"),
            BotCommand("help", "获取帮助信息"),
            BotCommand("clear", "删除最近发送的消息"),
            BotCommand("random", "随机发送消息"),
            BotCommand("collectlinks", "收集频道历史消息链接"),
            BotCommand("listlinks", "查看已收集的频道数据"),
            BotCommand("sendto", "克隆频道到目标频道"),
            BotCommand("stop", "停止批量转发任务"),
            BotCommand("config", "管理文本处理配置"),
            BotCommand("testconfig", "测试文本处理效果")
        ]
        await app.bot.set_my_commands(commands)
        print("✅ 机器人命令菜单已注册")
    except Exception as e:
        print(f"⚠️  注册命令菜单失败: {e}")
    
    # 尝试加载保存的配置
    try:
        import json
        if os.path.exists('config.json'):
            with open('config.json', 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
                dynamic_config.update(loaded_config)
            print("✅ 已加载保存的配置")
        else:
            print("ℹ️  未找到 config.json，使用默认配置")
    except Exception as e:
        print(f"⚠️  加载配置失败: {e}")
    
    # 启动用户客户端
    print("正在启动用户客户端...")
    try:
        await user_client.start()
        print("✅ 用户客户端启动成功")
        USER_CLIENT_READY = True
    except errors.SessionPasswordNeededError:
        print("⚠️  检测到两步验证，请输入你的两步验证密码：")
        password = input("请输入两步验证密码: ")
        try:
            await user_client.sign_in(password=password)
            USER_CLIENT_READY = True
            print("✅ 两步验证成功！")
        except Exception as e2:
            print(f"❌ 两步验证失败: {e2}")
            print("机器人将无法使用 /collectlinks 功能")
            USER_CLIENT_READY = False
    except Exception as e:
        print(f"❌ 用户客户端启动失败: {e}")
        print("机器人将无法使用 /collectlinks 功能")
        USER_CLIENT_READY = False

async def post_stop(app: Application) -> None:
    """在 PTB 应用停止后清理 Telethon 客户端"""
    await client.disconnect()
    if USER_CLIENT_READY:
        await user_client.disconnect()

def main() -> None:
    # 创建应用程序
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_stop(post_stop).build()

    # 添加命令处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_messages))
    
    # 添加消息处理器，处理消息链接
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(MESSAGE_LINK_PATTERN) & ~filters.COMMAND, 
        process_message_link
    ))
    
    # 处理其他消息
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # 添加随机消息处理器
    application.add_handler(CommandHandler("random", random_message))
    
    # 添加收集和克隆频道命令处理器
    application.add_handler(CommandHandler("collectlinks", collectlinks_command))
    application.add_handler(CommandHandler("listlinks", listlinks_command))
    application.add_handler(CommandHandler("sendto", sendto_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # 添加动态配置管理命令处理器
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("testconfig", test_config_command))
    
    print("机器人已启动")
    
    # 运行机器人直到按下 Ctrl-C
    application.run_polling()

if __name__ == '__main__':
    main()
