from dataclasses import dataclass


@dataclass(frozen=True)
class RerankPolicy:
    enabled: bool = False
    version: str = 'qwen-rerank-v1'
    max_candidates: int = 20
    max_query_chars: int = 1000
    max_document_chars: int = 800
    max_total_chars: int = 17000
    max_response_bytes: int = 64000
    timeout_seconds: int = 15
    allow_fallback: bool = False

    def __post_init__(self):
        if not (1<=self.max_candidates<=40 and 1<=self.max_query_chars<=1000 and
                1<=self.max_document_chars<=800 and 1<=self.max_total_chars<=33000 and
                1024<=self.max_response_bytes<=128000 and 1<=self.timeout_seconds<=30):
            raise ValueError('Invalid rerank policy')
