"""Stable character estimates for the complete provider-independent request."""

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ContextConfig:
    budget_chars: int = 50_000
    target_ratio: float = 0.8
    batch_preview_chars: int = 12_000
    archive_trigger: int = 50
    archive_target: int = 30
    recent_results: int = 3
    protected_exchanges: int = 2
    summary_input_chars: int = 24_000
    summary_output_tokens: int = 2000
    max_summaries: int = 4
    history_index_chars: int = 4000

    def __post_init__(self):
        if type(self.budget_chars) is not int or self.budget_chars < 1:
            raise ValueError('Context character budget must be a positive integer')
        if type(self.history_index_chars) is not int or self.history_index_chars < 0:
            raise ValueError('History index character budget must be a nonnegative integer')


def chars(system, tools, messages):
    return len(json.dumps({'system': system, 'tools': [tool.to_dict() for tool in tools],
                           'messages': [{'role': msg.role.value, 'content': [b.to_dict() for b in msg.content]}
                                        for msg in messages]}, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
