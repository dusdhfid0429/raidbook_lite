from datetime import datetime

import discord


_LOOT_PROCESSING_KEYS = set()
_LOOT_REVISIONS = {}


def loot_busy_message():
    return "⏳ 다른 사용자가 획득 아이템을 저장하고 정산 메시지를 갱신 중입니다. 잠시 후 다시 시도해 주세요."


def _loot_key(source_message_id):
    return str(source_message_id or "").strip()


def is_loot_processing(source_message_id):
    key = _loot_key(source_message_id)
    return bool(key and key in _LOOT_PROCESSING_KEYS)


def begin_loot_processing(source_message_id):
    key = _loot_key(source_message_id)
    if not key or key in _LOOT_PROCESSING_KEYS:
        return False
    _LOOT_PROCESSING_KEYS.add(key)
    return True


def end_loot_processing(source_message_id):
    key = _loot_key(source_message_id)
    if key:
        _LOOT_PROCESSING_KEYS.discard(key)


def get_loot_revision(source_message_id):
    key = _loot_key(source_message_id)
    return _LOOT_REVISIONS.get(key, 0)


def bump_loot_revision(source_message_id):
    key = _loot_key(source_message_id)
    if not key:
        return 0
    next_revision = _LOOT_REVISIONS.get(key, 0) + 1
    _LOOT_REVISIONS[key] = next_revision
    return next_revision


def build_loot_announcement_embed(
    subject_name,
    subject_time,
    loot_text,
    editor,
    guild_id,
    source_channel_id,
    source_message_id,
    *,
    acquirers="",
    body_line="용사들이 전리품을 획득했습니다!",
    jump_label="알림 메시지로 이동",
):
    """Build the shared loot-channel announcement embed."""
    jump_url = f"https://discord.com/channels/{guild_id}/{source_channel_id}/{source_message_id}"
    time_label = subject_time or "시간 미입력"
    acquirer_line = f"\n👤 **획득자**: {acquirers}" if acquirers else ""
    embed = discord.Embed(color=0xEF9F27)
    embed.description = (
        f"⚔️ **전리품 획득** 🎉\n\n"
        f"🎁 **{subject_name} ({time_label})**\n"
        f"{body_line}\n\n"
        f"```\n{loot_text if loot_text else '(기록 없음)'}\n```"
        f"{acquirer_line}"
        f"\n[🔗 {jump_label}]({jump_url})"
    )
    embed.set_footer(text=f"기록자: {editor} · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return embed


def loot_announcement_matches(message, source_message_id):
    """Return True if a loot-channel message points to the source alert/support message."""
    target = str(source_message_id)
    for embed in getattr(message, "embeds", []) or []:
        texts = [
            getattr(embed, "description", "") or "",
            getattr(embed, "title", "") or "",
            getattr(embed, "url", "") or "",
        ]
        footer = getattr(embed, "footer", None)
        if footer and getattr(footer, "text", None):
            texts.append(footer.text)
        for field in getattr(embed, "fields", []) or []:
            texts.append(getattr(field, "name", "") or "")
            texts.append(getattr(field, "value", "") or "")
        if any(target in text and "discord.com/channels" in text for text in texts):
            return True
    return False


async def find_loot_announcement_message(
    channel,
    source_message_id,
    existing_announcement_id=None,
    *,
    bot_user=None,
    logger=None,
    log_prefix="득템 공지",
    source_label="알림",
    history_limit=300,
):
    """Find an existing loot announcement by stored ID or source-message jump link."""
    if existing_announcement_id:
        try:
            return await channel.fetch_message(int(existing_announcement_id))
        except discord.NotFound:
            if logger:
                logger.info(f"[{log_prefix}] 저장된 공지 {existing_announcement_id} 를 찾을 수 없음")
        except Exception as e:
            if logger:
                logger.info(f"[{log_prefix} 조회 실패] {existing_announcement_id}: {e}")

    try:
        async for msg in channel.history(limit=history_limit):
            if bot_user and getattr(msg.author, "id", None) != bot_user.id:
                continue
            if loot_announcement_matches(msg, source_message_id):
                if logger:
                    logger.info(f"[{log_prefix} 검색] {source_label} {source_message_id} → 기존 공지 {msg.id}")
                return msg
    except Exception as e:
        if logger:
            logger.info(f"[{log_prefix} 검색 실패] {source_label} {source_message_id}: {e}")
    return None


async def post_or_update_loot_announcement(
    *,
    bot,
    loot_channel_id,
    source_channel_id,
    source_message_id,
    subject_name,
    subject_time,
    loot_text,
    editor,
    guild_id,
    acquirers="",
    existing_announcement_id=None,
    create_if_missing=True,
    logger=None,
    log_prefix="득템 공지",
    source_label="알림",
    body_line="용사들이 전리품을 획득했습니다!",
    jump_label="알림 메시지로 이동",
    view=None,
):
    """Post or edit the loot-channel announcement.

    view: 공지 메시지에 붙일 버튼 View (예: 보스봇 경매시작). None이면 버튼 없이 기존과 동일.

    Existing announcements are found by stored message ID first, then by the
    source message jump link. When create_if_missing is False, this function
    avoids creating duplicates and returns None if no existing announcement is found.
    """
    channel = bot.get_channel(loot_channel_id)
    if channel is None:
        if logger:
            logger.info(f"[{log_prefix}] 채널 {loot_channel_id} 를 찾을 수 없음")
        return None

    embed = build_loot_announcement_embed(
        subject_name,
        subject_time,
        loot_text,
        editor,
        guild_id,
        source_channel_id,
        source_message_id,
        acquirers=acquirers,
        body_line=body_line,
        jump_label=jump_label,
    )

    old_msg = await find_loot_announcement_message(
        channel,
        source_message_id,
        existing_announcement_id,
        bot_user=bot.user,
        logger=logger,
        log_prefix=log_prefix,
        source_label=source_label,
    )
    if old_msg:
        try:
            if view is not None:
                await old_msg.edit(embed=embed, view=view)
            else:
                await old_msg.edit(embed=embed)
            if logger:
                logger.info(
                    f"[{log_prefix} 수정] {subject_name} {subject_time} | "
                    f"{source_label} {source_message_id} ↔ 공지 {old_msg.id}"
                )
            return old_msg.id
        except Exception as e:
            if logger:
                logger.info(f"[{log_prefix} 수정 실패] {e}")
            if not create_if_missing:
                return None

    if not create_if_missing:
        if logger:
            logger.info(f"[{log_prefix} 수정 보류] {subject_name} {subject_time} | 기존 공지 없음, 새 게시 생략")
        return None

    try:
        if view is not None:
            new_msg = await channel.send(embed=embed, view=view)
        else:
            new_msg = await channel.send(embed=embed)
        if logger:
            logger.info(f"[{log_prefix} 게시] {subject_name} {subject_time} | {source_label} {source_message_id} → 공지 {new_msg.id}")
        return new_msg.id
    except Exception as e:
        if logger:
            logger.info(f"[{log_prefix} 게시 실패] {e}")
        return None


async def delete_loot_announcement(
    *,
    bot,
    loot_channel_id,
    source_message_id,
    existing_announcement_id=None,
    subject_name="",
    subject_time="",
    logger=None,
    log_prefix="득템 공지",
    source_label="알림",
):
    """Delete an existing loot announcement if it can be found."""
    channel = bot.get_channel(loot_channel_id)
    if channel is None:
        return False
    try:
        old_msg = await find_loot_announcement_message(
            channel,
            source_message_id,
            existing_announcement_id,
            bot_user=bot.user,
            logger=logger,
            log_prefix=log_prefix,
            source_label=source_label,
        )
        if not old_msg:
            return False
        await old_msg.delete()
        if logger:
            logger.info(f"[{log_prefix} 삭제] {subject_name} {subject_time} | 공지 {old_msg.id}")
        return True
    except discord.NotFound:
        return False
    except Exception as e:
        if logger:
            logger.info(f"[{log_prefix} 삭제 실패] {e}")
        return False


async def fire_settlement_trigger(
    send_acct_ipc,
    source_message_id,
    settlement_channel_id,
    *,
    logger=None,
    source_label="알림",
    subject_name="",
    subject_time="",
):
    """Call Accountant IPC _settle_trigger and log a shared result shape."""
    msg_id = str(source_message_id)
    if logger:
        logger.info(f"[정산 트리거] {source_label} {subject_name} {subject_time} → msg_id {msg_id}")
    r = await send_acct_ipc("_settle_trigger", [msg_id, str(settlement_channel_id)])
    if logger:
        if r.get("ok"):
            logger.info(f"[정산 트리거 완료] {r.get('data', {}).get('boss_name', '')}")
        else:
            logger.info(f"[정산 트리거 실패] {r.get('error', '알 수 없는 오류')}")
    return r


def schedule_settlement_trigger(
    asyncio_module,
    send_acct_ipc,
    source_message_id,
    settlement_channel_id,
    *,
    logger=None,
    source_label="알림",
    subject_name="",
    subject_time="",
):
    """Schedule a shared Accountant settlement trigger task."""
    try:
        task = fire_settlement_trigger(
            send_acct_ipc,
            source_message_id,
            settlement_channel_id,
            logger=logger,
            source_label=source_label,
            subject_name=subject_name,
            subject_time=subject_time,
        )
        return asyncio_module.ensure_future(task)
    except Exception as e:
        if logger:
            logger.info(f"[정산 트리거 실패] {e}")
        return None
