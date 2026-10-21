"""程序从已核验邮件候选建立选择快照；公开展示不包含内部业务引用。"""
import json
from uuid import UUID,uuid5
from pydantic import BaseModel,ConfigDict,Field,model_validator
from aegis_agent_worker.agent.contracts.decision import ResourceReference


class InputChoice(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    id: str=Field(min_length=1,max_length=128)
    label: str=Field(min_length=1,max_length=200)
    reference: ResourceReference

    @model_validator(mode="after")
    def email_only(self):
        if self.reference.resource_type!="email" or not self.reference.source_version or self.reference.version is not None:
            raise ValueError("选择项必须关联有版本的邮件")
        return self


def choice_id(question_id,reference):
    ref=ResourceReference.model_validate(reference)
    return str(uuid5(UUID(str(question_id)),ref.model_dump_json()))


def validate_options(input_type,options):
    items=[InputChoice.model_validate(item).model_dump(mode="json") for item in options]
    if (input_type not in {"text","single_choice"} or (input_type=="text" and items)
        or (input_type=="single_choice" and not 1<=len(items)<=20)
        or len({item["id"] for item in items})!=len(items)
        or len(json.dumps(items,ensure_ascii=False).encode())>16000):
        raise ValueError("补充问题选项无效")
    return items


def public_options(options):
    return [{"id":item["id"],"label":item["label"]} for item in options]
