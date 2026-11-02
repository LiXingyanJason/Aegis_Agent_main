from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationRetrievalPolicy:
    enabled: bool = False
    max_filter_ids: int = 4096
    max_snapshot_chunks: int = 512
    max_parts_per_source: int = 2
    max_context_chars: int = 3500
    max_result_bytes: int = 12000

    def __post_init__(self):
        for name,maximum in (('max_filter_ids',4096),('max_snapshot_chunks',512),('max_parts_per_source',2),
                              ('max_context_chars',3500),('max_result_bytes',12000)):
            if type(getattr(self,name)) is not int or not 1<=getattr(self,name)<=maximum:
                raise ValueError('代次检索额度无效')
