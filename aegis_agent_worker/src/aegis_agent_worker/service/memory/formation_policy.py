"""Pre-extraction gate; passing never proves a durable fact or permits a write."""
import re


def formation_gate(message,*,mode,external_allowed,conversation_excluded=False):
    if mode not in ('off','suggest','controlled_auto'):return 'invalid_mode'
    if mode=='off':return 'mode_off'
    if conversation_excluded or message.excluded:return 'excluded_scope'
    if message.role!='user':return 'unsupported_source'
    text=message.content
    if re.search(r'不要记|别记|不许记|忘记|忘掉',text):return 'memory_control'
    if re.search(r'密码|密钥|私钥|API.?KEY|-----BEGIN|\bsk-[\w-]{20,}|身份证|病史|诊断|银行卡',text,re.I):return 'sensitive_or_secret'
    if not external_allowed:return 'external_denied'
    if re.search(r'这次|本次|仅此一次|假设|角色扮演|他说|她说',text):return 'temporary_or_quoted'
    if re.fullmatch(r'[\s你好您好谢谢再见早晚安！!。，,.]+',text):return 'no_information'
    return 'eligible_for_extraction'
