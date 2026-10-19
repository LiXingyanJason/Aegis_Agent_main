"""日历时间语义的纯代码解析；无SQL、不决定空闲或审批。"""
import json
import re
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from aegis_agent_worker.agent.contracts.memory import TimezoneEntity


def explicit_calendar_timezone(text):
    """显式IANA/常用时区名称优先；ISO偏移仍由原时间解析器保留瞬时值。"""
    zones=set()
    for name in re.findall(r'\b(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)/[A-Za-z_0-9+-]+(?:/[A-Za-z_]+)?\b',text):
        try:
            zones.add(ZoneInfo(name).key)
        except (ZoneInfoNotFoundError,ValueError):
            raise ValueError('CALENDAR_EXPLICIT_TIMEZONE_INVALID') from None
    for pattern,name in ((r'北京时间|上海时间|中国时间','Asia/Shanghai'),
                         (r'东京时间','Asia/Tokyo'),(r'纽约时间','America/New_York'),
                         (r'伦敦时间','Europe/London'),(r'\bUTC\b(?![+-])|协调世界时','UTC')):
        if re.search(pattern,text,re.I): zones.add(name)
    for sign,hours,minutes in re.findall(r'\bUTC([+-])(\d{1,2})(?::(\d{2}))?',text,re.I):
        if (minutes and int(minutes)!=0) or int(hours)>14:
            raise ValueError('CALENDAR_EXPLICIT_TIMEZONE_INVALID')
        name='UTC' if int(hours)==0 else f"Etc/GMT{'-' if sign=='+' else '+'}{int(hours)}"
        try:
            zones.add(ZoneInfo(name).key)
        except ZoneInfoNotFoundError:
            raise ValueError('CALENDAR_EXPLICIT_TIMEZONE_INVALID') from None
    if len(zones)>1:
        raise ValueError('CALENDAR_EXPLICIT_TIMEZONE_AMBIGUOUS')
    return ZoneInfo(next(iter(zones))) if zones else None


def calendar_timezone(text,bundle,fallback):
    explicit=explicit_calendar_timezone(text)
    if explicit is not None:
        return explicit
    if bundle is not None:
        for item in bundle.items:
            if item.memory_type!='entity': continue
            value=json.loads(item.excerpt)
            if value.get('key')=='timezone':
                return ZoneInfo(TimezoneEntity.model_validate(value).value)
    return fallback
