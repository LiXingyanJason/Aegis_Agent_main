"""确定性记忆选择基线：只处理已授权候选，不判断权限，不调用模型。"""
import json
import re
from datetime import datetime,timezone


def terms(value):
    result=set()
    for part in re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}',value):
        if re.fullmatch(r'[\u4e00-\u9fff]+',part):
            result.update(part[index:index+2] for index in range(len(part)-1))
        else:
            result.add(part.lower())
    return result


def recall_hints(purpose,text,policy):
    text=text[:policy.max_task_chars]
    task_terms=terms(text)
    calendar=bool(task_terms & {'日程','会议','安排','空闲','时间','时区','日期','今天','明天'})
    mail=purpose in {'mail_reply_draft','mail_draft_revision'} or bool(task_terms & {'邮件','回复','起草','语气'})
    keys=set()
    if purpose.startswith('calendar_'):
        keys.update(policy.entity_keys_for(purpose))
    else:
        if calendar: keys.update({'timezone','meeting_preferences'})
        if mail: keys.add('reply_style')
        # 联系人仅在原任务明确出现姓名/邮箱时由SQL匹配，不因泛化“回复”带入地址。
        keys.add('contact')
        keys.intersection_update(policy.entity_keys_for(purpose))
    return dict(task=text.lower(),terms=sorted(task_terms,key=lambda item:(-len(item),item))[:policy.max_query_terms],
        affinity=['时区','北京','上午','下午','周一','周二','周三','周四','周五'] if calendar else [],
        entity_keys=sorted(keys),types=sorted(policy.types_for(purpose)))


def summary_topic(content):
    if re.search(r'时区|北京时间|Asia/|UTC',content,re.I): return 'timezone'
    if re.search(r'语气|措辞|风格|正式|简洁|友好|礼貌',content): return 'reply_style'
    if re.search(r'会议|开会|安排|空闲',content): return 'meeting_preferences'
    if re.search(r'联系人|邮箱|email',content,re.I): return 'contact'
    return 'unknown'


def describe_candidate(row,hints,policy):
    """评分只使用确定性字段/词项，更新时间只用来打破平局，不做衰减。"""
    entity=row.get('entity_value') if row.get('memory_type')=='entity' else None
    if isinstance(entity,str): entity=json.loads(entity)
    if entity:
        key=entity['key']
        if key not in hints['entity_keys']: return None
        if key=='contact' and not any(value and value.lower() in hints['task'] for value in (entity.get('name'),entity.get('email'))):
            return None
        return dict(row=row,topic=key,tier=3,matches=0,content=json.dumps(entity,ensure_ascii=False,sort_keys=True))
    content=str(row['content']).strip()
    direct=sum(term in content.lower() for term in hints['terms'])
    if not direct and not any(term in content.lower() for term in hints['affinity']): return None
    return dict(row=row,topic=summary_topic(content),tier=2 if direct>=policy.strong_term_matches else 1,
                matches=direct,content=content)


def rank_and_resolve(candidates):
    """已确认实体优先；同主题多个不同摘要/不可判定摘要保守排除，不合并或猜新值。"""
    entity_topics={item['topic'] for item in candidates if item['row']['memory_type']=='entity'}
    groups={}
    for item in candidates:
        if item['row']['memory_type']=='long_term_summary':
            groups.setdefault(item['topic'],set()).add(item['content'].lower())
    accepted,excluded=[],[]
    for item in candidates:
        row=item['row']; reason=None
        if row['memory_type']=='long_term_summary':
            if item['topic'] in entity_topics or row.get('has_entity_topic'):
                reason='entity_preferred'
            elif len(groups[item['topic']])>1 or row.get('summary_conflicted'):
                reason='summary_conflict'
            elif item['topic']=='unknown' and (entity_topics or row.get('has_any_entity')):
                reason='ambiguous_summary'
        (excluded if reason else accepted).append((item,reason) if reason else item)
    def key(item):
        updated=item['row'].get('updated_at')
        stamp=updated.astimezone(timezone.utc).isoformat() if isinstance(updated,datetime) else str(updated or '')
        return item['tier'],item['matches'],stamp,str(item['row']['id'])
    accepted.sort(key=key,reverse=True)
    seen=set(); unique=[]
    for item in accepted:
        signature=(item['row']['memory_type'],item['content'].lower())
        if signature in seen: excluded.append((item,'duplicate_memory'))
        else: seen.add(signature);unique.append(item)
    return unique,excluded
